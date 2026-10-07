from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("ai_assistant", "0035_document_sections"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="DocumentSourceReview",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("requirement_version", models.PositiveIntegerField()),
                ("snapshot", models.JSONField(default=dict)),
                ("created", models.DateTimeField(auto_now_add=True)),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_reviews",
                        to="ai_assistant.airequest",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_reviews",
                        to="ai_assistant.aidevtask",
                    ),
                ),
                (
                    "case",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_reviews",
                        to="testcases.testcase",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created", "-pk"),
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(task__isnull=False, case__isnull=True)
                            | models.Q(task__isnull=True, case__isnull=False)
                        ),
                        name="source_review_exactly_one_target",
                    )
                ],
            },
        ),
    ]
