import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def initial_gate_history(apps, schema_editor):
    Rule = apps.get_model('ai_assistant', 'AIReleaseGateRule')
    History = apps.get_model('ai_assistant', 'HistoricalAIReleaseGateRule')
    alias = schema_editor.connection.alias
    names = ('id', 'name', 'block_priority', 'min_success_rate', 'require_all_executed',
             'max_open_defects', 'is_active', 'updated', 'product_id', 'updated_by_id')
    for rule in Rule.objects.using(alias).all().iterator():
        History.objects.using(alias).create(**{name: getattr(rule, name) for name in names},
            history_date=timezone.now(), history_type='+', history_user_id=rule.updated_by_id,
            history_change_reason='启用历史记录时的初始快照（非原始操作审计）')


class Migration(migrations.Migration):
    dependencies = [('ai_assistant', '0040_project_ai_rules'),
                    ('management', '0013_remove_initial_qa_contact'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='AIAuditLog', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('actor_username', models.CharField(blank=True, max_length=150)),
            ('actor_role', models.CharField(blank=True, max_length=64)),
            ('action', models.CharField(choices=[('permission_denied', '权限拒绝'), ('report_approve', '报告审批'), ('report_gate_waive', '风险放行'), ('report_gate_evaluate', '门禁评估'), ('release_gate_update', '门禁规则维护'), ('report_export', '报告导出'), ('member_add', '添加成员'), ('member_remove', '移除成员'), ('role_change', '角色变更'), ('folder_delete', '目录删除'), ('credential_update', '模型凭据维护'), ('project_rule_update', 'AI 规则维护')], db_index=True, max_length=64)),
            ('result', models.CharField(choices=[('success', '成功'), ('denied', '拒绝'), ('failed', '失败')], max_length=16)),
            ('target_kind', models.CharField(blank=True, max_length=128)),
            ('target_id', models.CharField(blank=True, max_length=128)),
            ('target_repr', models.CharField(blank=True, max_length=255)),
            ('reason', models.TextField(blank=True)),
            ('detail', models.JSONField(blank=True, default=dict)),
            ('ip', models.CharField(blank=True, max_length=45, null=True)),
            ('request_id', models.CharField(blank=True, db_index=True, max_length=128)),
            ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
            ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ('product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='management.product')),
        ], options={'verbose_name': '操作审计', 'verbose_name_plural': '操作审计', 'ordering': ('-created', '-pk')}),
        migrations.CreateModel(name='HistoricalAIReleaseGateRule', fields=[
            ('id', models.IntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
            ('name', models.CharField(default='默认发布门禁', max_length=128, verbose_name='规则名称')),
            ('block_priority', models.CharField(choices=[('P1', 'P1'), ('P2', 'P2'), ('P3', 'P3'), ('P4', 'P4'), ('P5', 'P5')], default='P1', max_length=4, verbose_name='阻断缺陷优先级')),
            ('min_success_rate', models.DecimalField(decimal_places=2, default=95, max_digits=5, verbose_name='最低成功率')),
            ('require_all_executed', models.BooleanField(default=True, verbose_name='要求全部执行')),
            ('max_open_defects', models.PositiveIntegerField(default=0, verbose_name='允许未关闭缺陷数')),
            ('is_active', models.BooleanField(default=True, verbose_name='启用')),
            ('updated', models.DateTimeField(blank=True, editable=False)),
            ('history_id', models.AutoField(primary_key=True, serialize=False)),
            ('history_date', models.DateTimeField(db_index=True)),
            ('history_change_reason', models.TextField(null=True)),
            ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
            ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ('product', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='management.product', verbose_name='产品')),
            ('updated_by', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='最后修改人')),
        ], options={'verbose_name': 'historical AI 发布门禁规则', 'verbose_name_plural': 'historical AI 发布门禁规则',
                    'ordering': ('-history_date', '-history_id'), 'get_latest_by': ('history_date', 'history_id')},
           bases=(simple_history.models.HistoricalChanges, models.Model)),
        migrations.RunPython(initial_gate_history, migrations.RunPython.noop),
    ]
