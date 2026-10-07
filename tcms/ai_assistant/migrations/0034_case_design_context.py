from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_assistant", "0033_saved_case_views")]

    operations = [
        migrations.AddField(
            model_name="aitestcasedraft",
            name="source_context",
            field=models.JSONField(default=dict, blank=True, verbose_name="用例设计依据快照"),
        ),
        migrations.AddField(
            model_name="aitestcasedraft",
            name="dev_tasks",
            field=models.ManyToManyField(
                to="ai_assistant.aidevtask",
                blank=True,
                related_name="case_designs",
                verbose_name="参考开发任务单",
            ),
        ),
    ]
