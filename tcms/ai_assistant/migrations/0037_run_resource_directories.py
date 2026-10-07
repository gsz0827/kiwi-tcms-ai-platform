from django.db import migrations, models

RESOURCE_TYPES = [
    ("requirement", "需求"),
    ("case", "测试用例"),
    ("plan", "测试计划"),
    ("run", "执行任务"),
    ("case_group", "共享用例业务目录"),
    ("web_case", "Web 自动化用例"),
    ("api_case", "接口自动化用例"),
]


class Migration(migrations.Migration):
    dependencies = [("ai_assistant", "0036_document_source_review")]
    operations = [
        migrations.AlterField(
            model_name=name,
            name="resource_type",
            field=models.CharField(choices=RESOURCE_TYPES, max_length=20, verbose_name="资源类型"),
        )
        for name in ("projectresourcefolder", "projectresourceassignment")
    ]
