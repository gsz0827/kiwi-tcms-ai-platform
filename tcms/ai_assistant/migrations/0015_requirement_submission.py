from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_assistant", "0014_project_resource_folders")]

    operations = [
        migrations.AddField(
            model_name="airequest",
            name="submission_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="airequest",
            name="submission_fingerprint",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="airequest",
            constraint=models.UniqueConstraint(
                fields=("created_by", "submission_token"),
                name="unique_ai_submission_per_owner",
            ),
        ),
    ]
