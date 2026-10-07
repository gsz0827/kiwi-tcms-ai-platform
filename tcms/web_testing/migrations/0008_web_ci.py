from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('web_testing', '0007_bind_legacy_suite_environments')]
    operations = [
        migrations.AddField(model_name='websuite', name='ci_token_hash',
                            field=models.CharField(max_length=64, blank=True, editable=False)),
        migrations.AddField(model_name='websuite', name='ci_token_expires',
                            field=models.DateTimeField(null=True, blank=True, editable=False)),
        migrations.AddField(model_name='webrun', name='trigger',
                            field=models.CharField(max_length=16, default='manual',
                                choices=[('manual', '手动执行'), ('ci', 'CI 执行')])),
    ]
