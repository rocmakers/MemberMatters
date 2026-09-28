"""Match legacy door-lock consumers to profiles and import missing fobs.

Usage:
    python import_legacy_fobs.py ../t_b_Consumer_202607232121.csv
    python import_legacy_fobs.py input.csv --apply --output-dir fob_reports

The default mode is dry-run. Use ``--apply`` to write blank profile.rfid
values. Reports are written for updated rows, already assigned fobs, and rows
that need manual review.
"""

import argparse
import csv
import os
import re
import unicodedata

import django


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "membermatters.settings")
os.environ.setdefault("MM_LOG_LOCATION", "errors.log")
os.environ.setdefault("MM_DB_LOCATION", "taketwo.sqlite3")
django.setup()

from django.db import transaction  # noqa: E402
from profile.models import Profile  # noqa: E402


REPORT_FIELDS = [
    "legacy_consumer_id",
    "legacy_consumer_name",
    "legacy_first_name",
    "legacy_last_name",
    "legacy_fob",
    "user_id",
    "username",
    "db_first_name",
    "db_last_name",
    "db_fob",
    "fob_same",
    "reason",
    "potential_matches",
]


def clean(value):
    return "" if value is None else str(value).strip()


def name_key(value):
    value = unicodedata.normalize("NFKC", clean(value))
    value = value.casefold()
    return re.sub(r"\s+", " ", value)


def parse_consumer_name(value):
    value = clean(value)
    if not value:
        return "", ""

    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) >= 2:
        last_name = parts[0]
        first_name = parts[1]
    else:
        words = value.split()
        while words and words[-1].isdigit():
            words.pop()
        if len(words) < 2:
            return "", ""
        first_name = words[0]
        last_name = words[-1]

    first_name = re.sub(r"\s+\d+$", "", first_name).strip()
    last_name = re.sub(r"\s+\d+$", "", last_name).strip()
    return first_name, last_name


def profile_identity(profile):
    return {
        "user_id": profile.user_id,
        "username": profile.user.email,
        "db_first_name": profile.first_name,
        "db_last_name": profile.last_name,
        "db_fob": clean(profile.rfid),
    }


def potential_matches(profiles_by_last_name, last_name, excluded_ids=()):
    matches = []
    excluded_ids = set(excluded_ids)
    for profile in profiles_by_last_name.get(name_key(last_name), []):
        if profile.user_id in excluded_ids:
            continue
        matches.append(
            f"{profile.user_id}:{profile.user.email}:"
            f"{profile.first_name} {profile.last_name}"
        )
    return "; ".join(matches)


def report_row(legacy, first_name, last_name, legacy_fob, profile=None, **values):
    row = {
        "legacy_consumer_id": clean(legacy.get("f_ConsumerID")),
        "legacy_consumer_name": clean(legacy.get("f_ConsumerName")),
        "legacy_first_name": first_name,
        "legacy_last_name": last_name,
        "legacy_fob": legacy_fob,
        "user_id": "",
        "username": "",
        "db_first_name": "",
        "db_last_name": "",
        "db_fob": "",
        "fob_same": "",
        "reason": "",
        "potential_matches": "",
    }
    if profile is not None:
        row.update(profile_identity(profile))
        row["fob_same"] = str(row["db_fob"] == legacy_fob).lower()
    row.update(values)
    return row


def write_report(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def process(input_path, output_dir, apply_changes):
    profiles = list(Profile.objects.select_related("user").order_by("user_id"))
    profiles_by_name = {}
    profiles_by_last_name = {}
    profiles_by_fob = {}
    for profile in profiles:
        profiles_by_name.setdefault(
            (name_key(profile.first_name), name_key(profile.last_name)), []
        ).append(profile)
        profiles_by_last_name.setdefault(name_key(profile.last_name), []).append(
            profile
        )
        if clean(profile.rfid):
            profiles_by_fob[clean(profile.rfid)] = profile

    updated = []
    already_assigned = []
    unmatched = []

    with open(input_path, newline="", encoding="utf-8-sig") as source:
        for legacy in csv.DictReader(source):
            first_name, last_name = parse_consumer_name(legacy.get("f_ConsumerName"))
            legacy_fob = clean(legacy.get("f_CardNO"))
            matches = profiles_by_name.get((name_key(first_name), name_key(last_name)), [])

            if not first_name or not last_name or not legacy_fob:
                unmatched.append(
                    report_row(
                        legacy,
                        first_name,
                        last_name,
                        legacy_fob,
                        reason="invalid legacy name or fob",
                        potential_matches=potential_matches(
                            profiles_by_last_name, last_name
                        ),
                    )
                )
                continue

            if len(matches) != 1:
                reason = "no exact name match" if not matches else "multiple exact name matches"
                unmatched.append(
                    report_row(
                        legacy,
                        first_name,
                        last_name,
                        legacy_fob,
                        reason=reason,
                        potential_matches=potential_matches(
                            profiles_by_last_name, last_name
                        ),
                    )
                )
                continue

            profile = matches[0]
            if clean(profile.rfid):
                already_assigned.append(
                    report_row(
                        legacy,
                        first_name,
                        last_name,
                        legacy_fob,
                        profile,
                        reason="profile already has fob",
                    )
                )
                continue

            owner = profiles_by_fob.get(legacy_fob)
            if owner is not None and owner.user_id != profile.user_id:
                unmatched.append(
                    report_row(
                        legacy,
                        first_name,
                        last_name,
                        legacy_fob,
                        profile,
                        reason=f"legacy fob already belongs to user {owner.user_id}",
                    )
                )
                continue

            if apply_changes:
                with transaction.atomic():
                    profile.rfid = legacy_fob
                    profile.save(update_fields=["rfid"])
                profiles_by_fob[legacy_fob] = profile

            updated.append(
                report_row(
                    legacy,
                    first_name,
                    last_name,
                    legacy_fob,
                    profile,
                    db_fob=legacy_fob if apply_changes else "",
                    fob_same="true" if apply_changes else "",
                    reason="updated" if apply_changes else "would update",
                )
            )

    os.makedirs(output_dir, exist_ok=True)
    write_report(os.path.join(output_dir, "fobs_updated.csv"), updated)
    write_report(os.path.join(output_dir, "fobs_already_assigned.csv"), already_assigned)
    write_report(os.path.join(output_dir, "fobs_unmatched.csv"), unmatched)
    return len(updated), len(already_assigned), len(unmatched)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", help="Legacy door-lock consumer CSV")
    parser.add_argument(
        "--apply", action="store_true", help="Write missing profile fobs (default: dry-run)"
    )
    parser.add_argument(
        "--output-dir", default="fob_reports", help="Directory for output CSV reports"
    )
    args = parser.parse_args(argv)
    counts = process(args.input_csv, args.output_dir, args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"{mode}: updated={counts[0]} already_assigned={counts[1]} unmatched={counts[2]}")


if __name__ == "__main__":
    main()