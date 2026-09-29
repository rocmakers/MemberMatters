from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("access", "0020_accesscontrolleddevice_post_to_slack"),
    ]

    operations = [
        migrations.CreateModel(
            name="FobTesterDevice",
            fields=[
                (
                    "accesscontrolleddevice_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="access.accesscontrolleddevice",
                    ),
                ),
                (
                    "show_account_status",
                    models.BooleanField(
                        default=False, verbose_name="Display account status on device"
                    ),
                ),
            ],
            options={
                "verbose_name": "Fob Tester Device",
                "verbose_name_plural": "Fob Tester Devices",
            },
            bases=("access.accesscontrolleddevice",),
        ),
    ]
