from django.db import migrations, models
from django.utils.translation import gettext_lazy as _


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0007_attendanceconflictresolution_attendancedailyhours_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="attendance",
            name="work_location",
            field=models.CharField(
                choices=[("onsite", _("On-site")), ("wfh", _("Work From Home"))],
                default="onsite",
                max_length=20,
                verbose_name=_("Work Location"),
            ),
        ),
        migrations.AddField(
            model_name="historicalattendance",
            name="work_location",
            field=models.CharField(
                choices=[("onsite", _("On-site")), ("wfh", _("Work From Home"))],
                default="onsite",
                max_length=20,
                verbose_name=_("Work Location"),
            ),
        ),
    ]
