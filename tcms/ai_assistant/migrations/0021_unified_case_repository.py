from django.db import migrations
from django.utils import timezone


def unify_cases(apps, schema_editor):
    db = schema_editor.connection.alias
    Config = apps.get_model("ai_assistant", "APICase")
    Case = apps.get_model("testcases", "TestCase")
    History = apps.get_model("testcases", "HistoricalTestCase")
    Category = apps.get_model("testcases", "Category")
    Status = apps.get_model("testcases", "TestCaseStatus")
    Priority = apps.get_model("management", "Priority")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    ObjectPermission = apps.get_model("guardian", "UserObjectPermission")
    if not Config.objects.using(db).filter(test_case__isnull=True).exists():
        return
    status = Status.objects.using(db).order_by("is_confirmed", "pk").first()
    if not status:
        status = Status.objects.using(db).create(name="待审核", description="待审核的测试用例", is_confirmed=False)
    priority = Priority.objects.using(db).filter(is_active=True).first()
    if not priority:
        priority, _ = Priority.objects.using(db).get_or_create(value="P2", defaults={"is_active": True})
    ct, _ = ContentType.objects.using(db).get_or_create(app_label="testcases", model="testcase")
    permissions = [Permission.objects.using(db).get_or_create(content_type=ct, codename=code,
                   defaults={"name": code})[0] for code in ("view_testcase", "change_testcase")]
    for config in Config.objects.using(db).filter(test_case__isnull=True).iterator():
        category, _ = Category.objects.using(db).get_or_create(product_id=config.product_id, name="接口自动化")
        case = Case.objects.using(db).create(summary=config.name, category_id=category.pk,
            author_id=config.owner_id, priority_id=priority.pk, case_status_id=status.pk,
            is_automated=True, text="接口请求与断言见自动化配置。")
        history_data = {field.attname: getattr(case, field.attname) for field in Case._meta.fields
                        if field.attname in {item.attname for item in History._meta.fields}}
        History.objects.using(db).create(**history_data, history_date=timezone.now(),
            history_type="+", history_change_reason="接口用例纳入统一用例库")
        Config.objects.using(db).filter(pk=config.pk).update(test_case_id=case.pk)
        for permission in permissions:
            ObjectPermission.objects.using(db).get_or_create(user_id=config.owner_id,
                content_type_id=ct.pk, object_pk=str(case.pk), permission_id=permission.pk)


class Migration(migrations.Migration):
    dependencies = [("ai_assistant", "0020_suites_and_scheduling"), ("guardian", "0001_initial")]
    # Reversing schema must not delete newly created user-facing case records.
    operations = [migrations.RunPython(unify_cases, migrations.RunPython.noop)]
