import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_assistant", "0015_requirement_submission"),
        ("management", "0013_remove_initial_qa_contact"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AIInstructionProfile",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100, verbose_name="规则包名称")),
                ("description", models.CharField(blank=True, max_length=255, verbose_name="说明")),
                (
                    "operation",
                    models.CharField(
                        choices=[
                            ("all", "所有需求任务"),
                            ("requirement_analysis", "需求分析"),
                            ("test_case_generation", "生成测试用例"),
                        ],
                        default="all",
                        max_length=32,
                        verbose_name="适用任务",
                    ),
                ),
                ("instructions", models.TextField(verbose_name="测试规则")),
                ("version", models.PositiveIntegerField(default=1, verbose_name="版本")),
                ("is_active", models.BooleanField(default=True, verbose_name="已启用")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_instruction_profiles",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="所属账号",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_instruction_profiles",
                        to="management.product",
                        verbose_name="适用产品",
                    ),
                ),
            ],
            options={
                "verbose_name": "AI 规则包",
                "verbose_name_plural": "AI 规则包",
                "ordering": ("-is_active", "name", "-version"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("owner", "name"),
                        name="unique_ai_instruction_profile_per_owner",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="airequest",
            name="skill_snapshot",
            field=models.JSONField(
                blank=True,
                default=dict,
                verbose_name="需求任务使用的 AI 规则包快照",
            ),
        ),
    ]
