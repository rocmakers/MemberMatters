"""
Import legacy households as BillingGroups in MemberMatters.

Run AFTER import_legacy_members.py — all member Users must already exist.

Usage:
    python import_legacy_households.py [--dry-run] [--skip-inactive]

Options:
    --dry-run        Print what would be created without writing anything.
    --skip-inactive  Skip households where no active (state=active) member
                     exists; these groups would have no viable billing primary.

What this script does
---------------------
Each legacy `households` row with 2+ members that have been imported into
MemberMatters becomes a BillingGroup.

Head-of-household heuristic (no explicit HoH column in legacy schema):
  1. Lowest MemberID among members with MembersStatus=6 (Active) in that house
  2. Falling back to: lowest MemberID overall in that house

The resolved member becomes BillingGroup.primary_member.
All other members get profile.billing_group set to the group.

NOTE — billing not wired yet:
  BillingGroup.primary_member does NOT need an active Stripe subscription to
  be created here.  The group record is structural only.  Actual addon billing
  (charging the primary for household members) requires the primary to have a
  Stripe subscription and is tracked separately.  Groups where the primary is
  inactive/noob will be logged so they can be revisited.

Idempotency:
  - Skips any household whose lowest-MemberID member already owns a
    BillingGroup (billing_group_primary_member reverse relation set).
  - Skips members who already have profile.billing_group set.
"""

import os
import sys
import argparse
import django
from collections import defaultdict

# --- Django setup ---
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "membermatters.settings")
os.environ.setdefault("MM_LOG_LOCATION", "errors.log")
os.environ.setdefault("MM_DB_LOCATION", "taketwo.sqlite3")
django.setup()

import pymysql
from django.db import transaction
from django.contrib.auth import get_user_model
from profile.models import Profile, BillingGroup  # noqa: E402

User = get_user_model()

# ---------------------------------------------------------------------------
# Config (mirrors import_legacy_members.py)
# ---------------------------------------------------------------------------

def get_mysql_config():
    """Build legacy MySQL source config from environment variables."""
    required = {
        "host": os.environ.get("MM_LEGACY_MYSQL_HOST"),
        "user": os.environ.get("MM_LEGACY_MYSQL_USER"),
        "password": os.environ.get("MM_LEGACY_MYSQL_PASSWORD"),
        "database": os.environ.get("MM_LEGACY_MYSQL_DB"),
    }
    missing = [f"MM_LEGACY_MYSQL_{k.upper()}" for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing legacy MySQL config env vars: "
            + ", ".join(missing)
            + ". Set them before running import scripts."
        )

    return dict(
        host=required["host"],
        port=int(os.environ.get("MM_LEGACY_MYSQL_PORT", "3306")),
        user=required["user"],
        password=required["password"],
        database=required["database"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=int(os.environ.get("MM_LEGACY_MYSQL_CONNECT_TIMEOUT", "10")),
    )

ACTIVE_STATUS = 6  # MembersStatus = Active in legacy

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_households():
    """
    Returns a list of dicts, one per legacy household member row.
    Only households with 2+ members that have a primary email are included.
    Sorted by MemberID ASC so lowest ID appears first per house.
    """
    conn = pymysql.connect(**get_mysql_config())
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    m.MemberID,
                    m.HouseID,
                    m.MembersStatus,
                    m.FirstName,
                    m.LastName,
                    e.Email
                FROM members m
                JOIN emails e ON m.MemberID = e.MemberID AND e.`Primary` = 1
                WHERE m.HouseID IN (
                    SELECT HouseID
                    FROM members
                    GROUP BY HouseID
                    HAVING COUNT(*) >= 2
                )
                ORDER BY m.HouseID ASC, m.MemberID ASC;
            """)
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Group by HouseID
# ---------------------------------------------------------------------------


def group_by_house(rows):
    """Returns {HouseID: [row, ...]} preserving MemberID ASC order."""
    houses = defaultdict(list)
    for row in rows:
        houses[row["HouseID"]].append(row)
    return houses


def pick_primary(members):
    """
    Pick head-of-household.
    Priority: lowest MemberID among active (status 6) → lowest MemberID overall.
    """
    active = [m for m in members if m["MembersStatus"] == ACTIVE_STATUS]
    pool = active if active else members
    return min(pool, key=lambda m: m["MemberID"])


# ---------------------------------------------------------------------------
# Import one household
# ---------------------------------------------------------------------------


def import_household(house_id, members, dry_run, skip_inactive):
    """
    Returns one of: "created", "skip_no_members", "skip_single",
                    "skip_already_exists", "skip_inactive", "dry"
    """
    # Resolve which rows have a matching imported User
    profiles = []
    for m in members:
        email = (m["Email"] or "").strip().lower()
        try:
            user = User.objects.get(email__iexact=email)
            profiles.append((m, user.profile))
        except User.DoesNotExist:
            pass  # member wasn't imported (no email match)

    if len(profiles) < 2:
        print(
            f"  SKIP HouseID={house_id} — only {len(profiles)} imported member(s) found"
        )
        return "skip_single"

    primary_row = pick_primary([p[0] for p in profiles])
    primary_email = (primary_row["Email"] or "").strip().lower()
    try:
        primary_user = User.objects.get(email__iexact=primary_email)
        primary_profile = primary_user.profile
    except User.DoesNotExist:
        print(
            f"  SKIP HouseID={house_id} — primary MemberID={primary_row['MemberID']} not in system"
        )
        return "skip_no_members"

    # Idempotency: already owns a BillingGroup?
    if (
        hasattr(primary_profile, "billing_group_primary_member")
        and primary_profile.billing_group_primary_member is not None
    ):
        print(
            f"  SKIP HouseID={house_id} — {primary_email} already owns BillingGroup "
            f"#{primary_profile.billing_group_primary_member.id}"
        )
        return "skip_already_exists"

    primary_state = primary_profile.state
    if skip_inactive and primary_state != "active":
        print(
            f"  SKIP HouseID={house_id} — primary {primary_email} state={primary_state} (--skip-inactive)"
        )
        return "skip_inactive"

    group_name = f"Household {house_id}"

    # Collect non-primary members
    other_profiles = [
        prof
        for (row, prof) in profiles
        if prof.pk != primary_profile.pk
        and prof.billing_group is None  # not already in a group
    ]

    warn = ""
    if primary_state != "active":
        warn = f"  ⚠  primary {primary_email} state={primary_state} — billing not functional until active"

    print(
        f"  CREATE BillingGroup '{group_name}': primary={primary_email} ({primary_state}), "
        f"{len(other_profiles)} member(s)"
    )
    if warn:
        print(warn)

    if dry_run:
        return "dry"

    with transaction.atomic():
        group = BillingGroup.objects.create(
            name=group_name,
            primary_member=primary_profile,
        )
        # Wire primary into the group's members relation
        primary_profile.billing_group = group
        primary_profile.save(update_fields=["billing_group"])

        for prof in other_profiles:
            prof.billing_group = group
            prof.save(update_fields=["billing_group"])

    return "created"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Import legacy households as BillingGroups in MemberMatters."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing anything."
    )
    parser.add_argument(
        "--skip-inactive",
        action="store_true",
        help="Skip households where the heuristic primary is not active.",
    )
    # When piped into "python manage.py shell < script.py", sys.argv contains
    # Django shell args. Parse an empty argv in that mode so this script still runs.
    argv = [] if __name__ == "django.core.management.commands.shell" else sys.argv[1:]
    args = parser.parse_args(argv)

    from django.conf import settings

    source_host = os.environ.get("MM_LEGACY_MYSQL_HOST", "<unset>")
    source_db = os.environ.get("MM_LEGACY_MYSQL_DB", "<unset>")
    print(
        f"Source legacy DB: mysql://{source_host}/{source_db} | "
        f"Target Django DB engine: {settings.DATABASES['default']['ENGINE']}",
        flush=True,
    )

    print(
        f"{'DRY RUN — ' if args.dry_run else ''}Fetching household data from legacy DB...",
        flush=True,
    )
    rows = fetch_households()
    print(
        f"Fetched {len(rows)} member rows across multi-member households.\n",
        flush=True,
    )

    houses = group_by_house(rows)
    print(f"Found {len(houses)} households with 2+ legacy members.\n", flush=True)

    counts = {
        "created": 0,
        "dry": 0,
        "skip_single": 0,
        "skip_no_members": 0,
        "skip_already_exists": 0,
        "skip_inactive": 0,
    }

    for house_id, members in sorted(houses.items()):
        result = import_household(house_id, members, args.dry_run, args.skip_inactive)
        counts[result] = counts.get(result, 0) + 1

        print(f"""
Done.
  created             : {counts['created']}
  dry (would create)  : {counts['dry']}
  skip (already exists): {counts['skip_already_exists']}
  skip (<2 imported)  : {counts['skip_single']}
  skip (no members)   : {counts['skip_no_members']}
  skip (inactive prim): {counts['skip_inactive']}
""", flush=True)

    if not args.dry_run and counts["created"] > 0:
        print(
            "NOTE: BillingGroups with an inactive/noob primary member have been created"
        )
        print(
            "      but Stripe billing will not function until the primary subscribes."
        )
        print("      Search output above for '⚠' lines to identify them.\n")


if __name__ == "__main__":
    main()

if __name__ == "django.core.management.commands.shell":
    main()
