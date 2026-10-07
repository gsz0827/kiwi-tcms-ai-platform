from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("web_testing", "0004_ai_drafts"),
        ("testcases", "0024_alter_testcase_extra_link"),
    ]
    operations = [
        migrations.AddField(
            model_name="webcase",
            name="test_case",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="web_configs",
                to="testcases.testcase",
                verbose_name="关联业务用例",
            ),
        )
    ]
