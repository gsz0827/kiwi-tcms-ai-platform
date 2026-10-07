import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_assistant", "0032_common_case_directories"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="SavedCaseView",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=80, verbose_name="视图名称")),
                ("filters", models.JSONField(default=dict, verbose_name="筛选条件")),
                ("revision", models.PositiveIntegerField(default=1, verbose_name="修订号")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saved_case_views",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("name", "pk"),
                "verbose_name": "个人用例筛选视图",
                "verbose_name_plural": "个人用例筛选视图",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("owner", "name"), name="unique_saved_case_view_name"
                    )
                ],
            },
        ),
    ]
