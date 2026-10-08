from django.db import migrations


def preserve_execution_order(apps, schema_editor):
    Suite = apps.get_model("ai_assistant", "APISuite")
    Case = apps.get_model("ai_assistant", "APICase")
    alias = schema_editor.connection.alias
    for suite in Suite.objects.using(alias).all().iterator():
        ids = suite.case_ids
        if not isinstance(ids, list) or any(type(value) is not int for value in ids):
            continue  # Invalid legacy suites remain invalid, never silently repair/drop IDs.
        cases = Case.objects.using(alias).filter(
            pk__in=ids, owner_id=suite.owner_id, product_id=suite.product_id
        ).values_list("pk", "sequence")
        rank = {pk: (sequence, pk) for pk, sequence in cases}
        ordered = sorted(ids, key=lambda pk: rank.get(pk, (65536, pk)))
        if ordered != ids:
            Suite.objects.using(alias).filter(pk=suite.pk).update(case_ids=ordered)


class Migration(migrations.Migration):
    dependencies = [("ai_assistant", "0038_postman_import")]
    operations = [migrations.RunPython(preserve_execution_order, migrations.RunPython.noop)]
