from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def preserve_and_publish(apps, schema_editor):
    Profile = apps.get_model("ai_assistant", "AIInstructionProfile")
    Binding = apps.get_model("ai_assistant", "ProjectAIRuleBinding")
    Revision = apps.get_model("ai_assistant", "AIInstructionRevision")
    alias = schema_editor.connection.alias
    for profile in Profile.objects.using(alias).all().iterator():
        Revision.objects.using(alias).get_or_create(profile_id=profile.pk, version=profile.version,
            defaults={"changed_by_id": profile.owner_id, "snapshot": {
                "name": profile.name, "description": profile.description,
                "instructions": profile.instructions, "sections": {},
                "operation": profile.operation, "is_active": profile.is_active,
                "product_id": profile.product_id}})
    products = Profile.objects.using(alias).exclude(product_id=None).values_list("product_id", flat=True).distinct()
    for product_id in products:
        profiles = list(Profile.objects.using(alias).filter(product_id=product_id))
        # No arbitrary winner if multiple accounts maintained different content.
        if len(profiles) == 1:
            profile = profiles[0]
            Binding.objects.using(alias).get_or_create(product_id=product_id,
                defaults={"profile_id": profile.pk, "published_by_id": profile.owner_id})


class Migration(migrations.Migration):
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL),
                    ("ai_assistant", "0039_suite_script_order")]
    operations = [
        migrations.AddField(model_name="aiinstructionprofile", name="sections",
                            field=models.JSONField(default=dict, blank=True)),
        migrations.CreateModel(name="ProjectAIRuleBinding", fields=[
            ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("updated", models.DateTimeField(auto_now=True)),
            ("product", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                related_name="ai_rule_binding", to="management.product")),
            ("profile", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                related_name="project_binding", to="ai_assistant.aiinstructionprofile")),
            ("published_by", models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name="AIInstructionRevision", fields=[
            ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("version", models.PositiveIntegerField()),
            ("snapshot", models.JSONField(default=dict)),
            ("created", models.DateTimeField(auto_now_add=True)),
            ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                related_name="revisions", to="ai_assistant.aiinstructionprofile")),
            ("changed_by", models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
        ], options={"ordering": ("-version",), "constraints": [
            models.UniqueConstraint(fields=("profile", "version"), name="unique_ai_instruction_revision")]}),
        migrations.RunPython(preserve_and_publish, migrations.RunPython.noop),
    ]
