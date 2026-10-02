from hashlib import sha256
from django.db import migrations


def migrate_paths(apps, schema_editor):
    WebCase = apps.get_model('web_testing', 'WebCase')
    Folder = apps.get_model('ai_assistant', 'ProjectResourceFolder')
    Assignment = apps.get_model('ai_assistant', 'ProjectResourceAssignment')
    alias = schema_editor.connection.alias
    for case in WebCase.objects.using(alias).exclude(folder='').iterator():
        if Assignment.objects.using(alias).filter(resource_type='web_case', object_id=case.pk).exists():
            continue
        parent = None
        for segment in case.folder.split('/'):
            name = segment.strip()
            if not name:
                continue
            if len(name) > 120:
                name = name[:103] + '-' + sha256(name.encode()).hexdigest()[:16]
            parent, _ = Folder.objects.using(alias).get_or_create(
                product_id=case.product_id, resource_type='web_case', parent=parent, name=name,
                defaults={'created_by_id':case.owner_id, 'updated_by_id':case.owner_id})
        if parent:
            Assignment.objects.using(alias).get_or_create(resource_type='web_case', object_id=case.pk,
                defaults={'folder':parent, 'assigned_by_id':case.owner_id})


class Migration(migrations.Migration):
    dependencies = [('web_testing','0001_initial'), ('ai_assistant','0028_automation_folder_types')]
    # Never erase folders edited after migrating forward. Legacy strings remain intact.
    operations = [migrations.RunPython(migrate_paths, migrations.RunPython.noop)]
