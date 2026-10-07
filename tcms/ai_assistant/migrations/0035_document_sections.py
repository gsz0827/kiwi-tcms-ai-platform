from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("ai_assistant", "0034_case_design_context"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(
            model_name="airequest",
            name="document_sections",
            field=models.JSONField(default=dict, blank=True, verbose_name="需求文档分区"),
        ),
        migrations.AddField(
            model_name="airequest",
            name="target_version",
            field=models.ForeignKey(
                to="management.version",
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ai_target_requirements",
                verbose_name="目标产品版本",
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="assigned_to",
            field=models.ForeignKey(
                to=settings.AUTH_USER_MODEL,
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_ai_requirements",
                verbose_name="需求负责人",
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="priority",
            field=models.CharField(
                max_length=8,
                default="P3",
                choices=[(f"P{i}", f"P{i}") for i in range(1, 6)],
                verbose_name="优先级",
            ),
        ),
        migrations.AddField(
            model_name="airequest",
            name="status",
            field=models.CharField(
                max_length=16,
                default="draft",
                choices=[
                    ("draft", "草稿"),
                    ("confirmed", "已确认"),
                    ("doing", "进行中"),
                    ("done", "已完成"),
                ],
                verbose_name="需求状态",
            ),
        ),
        migrations.AlterField(
            model_name="airequest",
            name="requirement",
            field=models.TextField(verbose_name="功能说明"),
        ),
        migrations.AddField(
            model_name="airequirementversion",
            name="document_sections",
            field=models.JSONField(default=dict, blank=True, verbose_name="需求分区快照"),
        ),
        migrations.AddField(
            model_name="airequirementversion",
            name="attributes",
            field=models.JSONField(default=dict, blank=True, verbose_name="需求属性快照"),
        ),
        migrations.AddField(
            model_name="aidevtask",
            name="document_sections",
            field=models.JSONField(default=dict, blank=True, verbose_name="开发文档分区"),
        ),
        migrations.AddField(
            model_name="aidevtask",
            name="target_version",
            field=models.ForeignKey(
                to="management.version",
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ai_target_dev_tasks",
                verbose_name="目标产品版本",
            ),
        ),
    ]
