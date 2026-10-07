import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('ai_assistant', '0037_run_resource_directories'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name='apicase', name='import_review_required',
                            field=models.BooleanField(default=False, editable=False)),
        migrations.CreateModel(name='PostmanImport', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('fingerprint', models.CharField(max_length=64)),
            ('title', models.CharField(max_length=120)),
            ('payload_encrypted', models.TextField(blank=True)),
            ('case_ids', models.JSONField(default=list)),
            ('created', models.DateTimeField(auto_now_add=True)),
            ('expires', models.DateTimeField()),
            ('imported', models.DateTimeField(null=True)),
            ('owner', models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.CASCADE)),
            ('product', models.ForeignKey(to='management.product', on_delete=django.db.models.deletion.CASCADE)),
        ], options={'constraints': [models.UniqueConstraint(fields=('owner','product','fingerprint'),
                                                            name='unique_postman_import_source')]}),
    ]
