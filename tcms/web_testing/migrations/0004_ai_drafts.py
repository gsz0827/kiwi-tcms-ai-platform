from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0013_remove_initial_qa_contact"),
        ("web_testing", "0003_environments_datasets"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="WebAIRequest",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200)),
                ("submission_token", models.UUIDField()),
                ("fingerprint", models.CharField(max_length=64)),
                ("input_encrypted", models.TextField()),
                ("generated", models.BooleanField(default=False)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="management.product")),
            ],
            options={"ordering": ("-created", "-pk")},
        ),
        migrations.CreateModel(
            name="WebAIDraft",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveIntegerField()),
                ("name", models.CharField(max_length=200, verbose_name="用例名称")),
                ("description", models.TextField(blank=True, verbose_name="前置条件与预期结果")),
                ("evidence", models.TextField(blank=True, verbose_name="原文依据")),
                ("steps", models.JSONField(default=list, verbose_name="操作与断言步骤")),
                ("questions", models.JSONField(default=list)),
                ("review_notes", models.TextField(blank=True, verbose_name="确认说明")),
                ("reviewed_at", models.DateTimeField(null=True)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("imported_at", models.DateTimeField(null=True)),
                ("web_case", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to="web_testing.webcase")),
                ("request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="drafts", to="web_testing.webairequest")),
            ],
            options={"ordering": ("position", "pk")},
        ),
        migrations.AddConstraint(model_name="webairequest", constraint=models.UniqueConstraint(
            fields=("owner", "submission_token"), name="web_ai_submission_unique")),
        migrations.AddConstraint(model_name="webaidraft", constraint=models.UniqueConstraint(
            fields=("request", "position"), name="web_ai_draft_position_unique")),
    ]
