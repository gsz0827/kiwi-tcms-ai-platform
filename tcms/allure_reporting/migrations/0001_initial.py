import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("ai_assistant", "0037_run_resource_directories"),
        ("web_testing", "0006_formal_execution"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="AllureReport",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "等待生成"),
                            ("generating", "生成中"),
                            ("ready", "已生成"),
                            ("error", "生成失败"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("artifact", models.BinaryField(null=True)),
                ("checksum", models.CharField(blank=True, max_length=64)),
                ("engine_version", models.CharField(blank=True, max_length=32)),
                ("summary", models.JSONField(default=dict)),
                ("error", models.CharField(blank=True, max_length=300)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("heartbeat", models.DateTimeField(null=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("generated", models.DateTimeField(null=True)),
                (
                    "api_run",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="allure_report",
                        to="ai_assistant.apirun",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "web_run",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="allure_report",
                        to="web_testing.webrun",
                    ),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["status", "created"], name="allure_report_queue")],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(web_run__isnull=False, api_run__isnull=True)
                            | models.Q(web_run__isnull=True, api_run__isnull=False)
                        ),
                        name="allure_exactly_one_source",
                    )
                ],
            },
        ),
    ]
