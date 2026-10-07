from django.db import migrations, models

OPERATIONS = [
    ("api_case_generation", "AI 生成接口用例"),
    ("web_case_generation", "AI 生成 Web 用例"),
    ("requirement_analysis", "需求分析"),
    ("test_case_generation", "生成测试用例"),
    ("dev_task_breakdown", "拆分开发任务"),
    ("coverage_analysis", "覆盖率分析"),
    ("coverage_supplement", "补充覆盖缺口"),
    ("test_case_review", "测试用例评审"),
    ("test_run_analysis", "测试运行分析"),
    ("defect_draft_generation", "生成缺陷草稿"),
    ("test_report_generation", "生成测试报告"),
    ("connection_test", "模型连接测试"),
]


class Migration(migrations.Migration):
    dependencies = [("ai_assistant", "0030_automation_archive")]
    operations = [
        migrations.AlterField(model_name="aijob", name="operation",
            field=models.CharField(choices=OPERATIONS, max_length=40, verbose_name="任务类型")),
        migrations.AlterField(model_name="aiusagelog", name="operation",
            field=models.CharField(choices=OPERATIONS + [("other", "其他")], max_length=40, verbose_name="操作类型")),
    ]
