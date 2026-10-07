from django.db import migrations, transaction


def bind_legacy_environments(apps, schema_editor):
    Suite = apps.get_model('web_testing', 'WebSuite')
    Environment = apps.get_model('web_testing', 'WebEnvironment')
    database = schema_editor.connection.alias
    for suite_id in Suite.objects.using(database).filter(environment__isnull=True).values_list('pk', flat=True).iterator():
        with transaction.atomic(using=database):
            suite = Suite.objects.using(database).select_for_update().get(pk=suite_id)
            if suite.environment_id is not None:
                continue
            # Dedicated environment: do not accidentally inherit another environment's
            # credentials, variables or login steps merely because its URL matches.
            environment = Environment.objects.using(database).create(
                owner_id=suite.owner_id, product_id=suite.product_id,
                name=f'兼容环境 · 套件 {suite.pk} · {suite.name}'[:200],
                base_url=suite.base_url, ignore_https_errors=suite.ignore_https_errors,
                variables_encrypted='', setup_case_id=None,
            )
            Suite.objects.using(database).filter(pk=suite.pk, environment__isnull=True).update(environment_id=environment.pk)


class Migration(migrations.Migration):
    dependencies = [('web_testing', '0006_formal_execution')]
    # Reversing must never delete an environment that a user may since have edited.
    operations = [migrations.RunPython(bind_legacy_environments, migrations.RunPython.noop)]
