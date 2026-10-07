from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('web_testing', '0005_webcase_business_scenario'), ('testruns', '0020_testexecutiontag')]
    operations = [
        migrations.AddField(model_name='webrun', name='execution_mode', field=models.CharField(
            max_length=16, default='legacy', choices=[('legacy', '历史执行'), ('formal', '正式执行'), ('debug', '调试执行')])),
        migrations.AddField(model_name='webrun', name='test_run', field=models.ForeignKey(
            to='testruns.testrun', null=True, blank=True, related_name='web_runs', on_delete=django.db.models.deletion.PROTECT)),
        migrations.AddField(model_name='webrun', name='environment', field=models.ForeignKey(
            to='web_testing.webenvironment', null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL)),
    ]
