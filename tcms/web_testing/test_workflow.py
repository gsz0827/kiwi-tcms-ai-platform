import json
import uuid
from datetime import datetime, timezone as datetime_timezone

from django.contrib.auth.models import Group, Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from guardian.shortcuts import assign_perm

from tcms.tests.factories import UserFactory, ProductFactory, VersionFactory, BuildFactory, TestPlanFactory, TestCaseFactory
from tcms.testruns.models import TestRun, TestExecutionStatus
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from tcms.ai_assistant.automation_archive import publish
from tcms.ai_assistant.models import AutomationArchive, AIDefectDraft
from tcms.kiwi_auth.models import UserPreference
from .models import WebCase, WebSuite, WebEnvironment, WebRun, WebResult


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class FormalWorkflowTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True, is_active=True)
        self.product = ProductFactory()
        self.version = VersionFactory(product=self.product)
        self.plan = TestPlanFactory(product=self.product, product_version=self.version)
        self.build = BuildFactory(version=self.version, is_active=True)
        self.case = TestCaseFactory(category__product=self.product)
        self.case.save()
        self.plan.add_case(self.case)
        self.steps = [{'action': 'goto', 'value': '/'}, {'action': 'assert_visible', 'selector': 'body'}]
        self.config = WebCase.objects.create(owner=self.owner, product=self.product, test_case=self.case,
                                            name='Login config', steps_encrypted=encrypt_api_key(json.dumps(self.steps)))
        self.env = WebEnvironment.objects.create(owner=self.owner, product=self.product, name='QA environment',
                                                base_url='https://kiwi-web:8443', ignore_https_errors=True,
                                                variables_encrypted=encrypt_api_key('{"secret":"never-disclose"}'))
        self.suite = WebSuite.objects.create(owner=self.owner, product=self.product, name='Regression',
                                            base_url='https://kiwi-web:8443', case_ids=[self.config.pk])
        self.states = {key: TestExecutionStatus.objects.create(name='Formal ' + key, weight=weight)
                       for key, weight in [('passed', 1), ('failed', -1), ('pending', 0)]}
        self.client.force_login(self.owner)

    def submit(self, **changes):
        data = {'token': str(uuid.uuid4()), 'execution_mode': 'formal', 'plan': self.plan.pk,
                'build': self.build.pk, 'environment': self.env.pk}
        data.update(changes)
        return self.client.post(reverse('web_testing:submit', args=[self.suite.pk]), data)

    def archive(self, run, **changes):
        data = dict(self.states, plan=self.plan, build=self.build, confirm=True, defects=[])
        data.update(changes)
        return publish(self.owner, 'web', run.pk, data)

    def finish(self, run, failed=False):
        run.status = 'failed' if failed else 'passed'
        run.completed_count = run.total
        run.save()
        for position in range(1, run.total + 1):
            WebResult.objects.create(run=run, position=position, name='Result', status=run.status)

    def test_formal_creates_native_task_and_frozen_identity(self):
        self.assertEqual(self.submit().status_code, 302)
        run = WebRun.objects.get()
        target = run.test_run
        execution = target.executions.get()
        self.assertEqual((run.execution_mode, target.plan_id, target.build_id), ('formal', self.plan.pk, self.build.pk))
        self.assertEqual(execution.case_id, self.case.pk)
        self.assertEqual(execution.case_text_version, self.case.history.latest().history_id)
        self.assertEqual(execution.status.weight, 0)
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        self.assertEqual(snapshot['execution_context']['test_run_id'], target.pk)
        self.assertNotIn('never-disclose', self.client.get(reverse('web_testing:run', args=[run.pk])).content.decode())
        self.assertEqual(target.executions.count(), run.total)

    def test_repeated_submission_does_not_create_extra_native_tasks(self):
        token = str(uuid.uuid4())
        self.submit(token=token)
        self.submit(token=token)
        self.assertEqual(WebRun.objects.count(), 1)
        self.assertEqual(TestRun.objects.count(), 1)

    def test_missing_business_link_blocks_before_task_creation(self):
        self.config.test_case = None
        self.config.save()
        response = self.submit()
        self.assertContains(response, f'WEB-{self.config.pk}')
        self.assertFalse(WebRun.objects.exists())
        self.assertFalse(TestRun.objects.exists())

    def test_missing_environment_and_mismatching_build_are_blocked(self):
        self.assertEqual(self.submit(environment='').status_code, 200)
        other = BuildFactory(version=VersionFactory(product=self.product), is_active=True)
        self.assertContains(self.submit(build=other.pk), '构建版本必须与测试计划版本一致')
        self.assertFalse(WebRun.objects.exists())
        self.assertFalse(TestRun.objects.exists())

    def test_cross_owner_environment_and_foreign_plan_are_rejected(self):
        other_owner = UserFactory()
        foreign_env = WebEnvironment.objects.create(owner=other_owner, product=self.product, name='private-env', base_url=self.env.base_url)
        response = self.submit(environment=foreign_env.pk)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'private-env')
        foreign_plan = TestPlanFactory()
        self.submit(plan=foreign_plan.pk)
        self.assertFalse(WebRun.objects.exists())

    def test_business_cases_must_already_be_in_plan(self):
        self.plan.delete_case(self.case)
        self.assertContains(self.submit(), '请先将这些业务用例加入所选测试计划')
        self.assertFalse(self.plan.cases.exists())
        self.assertFalse(TestRun.objects.exists())

    def test_business_visibility_checked_even_with_plan_permissions(self):
        self.owner.is_superuser = False
        self.owner.save()
        self.owner.user_permissions.set(Permission.objects.filter(codename__in=[
            'add_testrun', 'add_testexecution', 'change_testexecution']))
        assign_perm('testplans.change_testplan', self.owner, self.plan)
        response = self.submit()
        self.assertContains(response, '部分关联业务用例不可访问')
        self.assertFalse(TestRun.objects.exists())

    def test_readonly_cannot_submit(self):
        self.owner.groups.add(Group.objects.get_or_create(name='AI 只读')[0])
        self.assertEqual(self.submit().status_code, 403)
        self.assertFalse(WebRun.objects.exists())

    def test_debug_allows_unlinked_config_without_native_task_or_archive(self):
        self.config.test_case = None
        self.config.save()
        self.assertEqual(self.submit(execution_mode='debug', plan='', build='', environment='').status_code, 302)
        run = WebRun.objects.get()
        self.assertEqual(run.execution_mode, 'debug')
        self.assertIsNone(run.test_run_id)
        self.finish(run)
        with self.assertRaisesMessage(ValueError, '调试执行不进入正式报告'):
            self.archive(run)
        response = self.client.get(reverse('web_testing:run', args=[run.pk]))
        self.assertNotContains(response, '确认结果 / 归档报告')
        self.assertFalse(TestRun.objects.exists())

    def test_formal_archive_reuses_exact_precreated_task_with_multiple_datasets(self):
        self.suite.datasets_encrypted = encrypt_api_key('[{"user":1},{"user":2}]')
        self.suite.save()
        self.submit()
        run = WebRun.objects.get()
        self.assertEqual(run.test_run.executions.count(), 2)
        self.finish(run, failed=True)
        archived = self.archive(run, defects=['1'])
        self.assertEqual(archived.test_run_id, run.test_run_id)
        self.assertEqual(TestRun.objects.count(), 1)
        self.assertEqual(archived.test_run.executions.filter(status__weight__lt=0).count(), 2)
        self.assertEqual(AIDefectDraft.objects.count(), 1)
        self.assertEqual(self.archive(run).pk, archived.pk)

    def test_archive_cannot_change_plan_build_or_target(self):
        self.submit()
        run = WebRun.objects.get()
        self.finish(run)
        other_build = BuildFactory(version=self.version)
        with self.assertRaisesMessage(ValueError, '提交时选定'):
            self.archive(run, build=other_build)
        other = TestRun.objects.create(plan=self.plan, build=self.build, manager=self.owner, summary='Other task')
        with self.assertRaisesMessage(ValueError, '其他任务'):
            self.archive(run, target_run=other)
        self.assertFalse(AutomationArchive.objects.exists())

    def test_archive_form_disables_submission_binding_fields(self):
        self.submit()
        run = WebRun.objects.get()
        self.finish(run)
        response = self.client.get(reverse('ai_assistant:automation_archive', args=['web', run.pk]))
        self.assertEqual(response.status_code, 200)
        for key in ('plan', 'build', 'target_run'):
            self.assertTrue(response.context['form'].fields[key].disabled)
        self.assertEqual(response.context['form'].initial['target_run'], run.test_run_id)

    def test_native_mutation_rejects_archive_without_overwriting(self):
        self.submit()
        run = WebRun.objects.get()
        self.finish(run)
        execution = run.test_run.executions.get()
        execution.status = self.states['failed']
        execution.save()
        with self.assertRaisesMessage(ValueError, '已修改'):
            self.archive(run)
        execution.refresh_from_db()
        self.assertEqual(execution.status_id, self.states['failed'].pk)
        self.assertFalse(AutomationArchive.objects.exists())

    def test_retry_is_debug_and_does_not_copy_native_task_binding(self):
        self.submit()
        source = WebRun.objects.get()
        self.finish(source)
        self.client.post(reverse('web_testing:retry', args=[source.pk]), {'token': str(uuid.uuid4())})
        child = WebRun.objects.get(source=source)
        self.assertEqual(child.execution_mode, 'debug')
        self.assertIsNone(child.test_run_id)
        self.assertEqual(child.snapshot_encrypted, source.snapshot_encrypted)
        self.assertEqual(TestRun.objects.count(), 1)

    def test_environment_override_and_frozen_display(self):
        self.suite.environment = self.env
        self.suite.save()
        selected = WebEnvironment.objects.create(owner=self.owner, product=self.product, name='Override',
                                                 base_url='https://kiwi-web:8443/accounts/login/', ignore_https_errors=False)
        self.submit(environment=selected.pk)
        run = WebRun.objects.get()
        self.assertEqual(run.environment_id, selected.pk)
        self.assertEqual(json.loads(decrypt_api_key(run.snapshot_encrypted))['base_url'], selected.base_url)
        selected.name = 'Changed after submit'
        selected.save()
        response = self.client.get(reverse('web_testing:run', args=[run.pk]))
        self.assertContains(response, '环境：Override')
        self.assertNotContains(response, 'Changed after submit')

    def test_project_scope_all_override_and_filters(self):
        other = ProductFactory()
        alien = WebSuite.objects.create(owner=self.owner, product=other, name='Other project suite', case_ids=[])
        session = self.client.session
        session['ai_product_id'] = self.product.pk
        session.save()
        response = self.client.get(reverse('web_testing:suites'))
        self.assertNotContains(response, alien.name)
        self.assertContains(self.client.get(reverse('web_testing:suites'), {'product': ''}), alien.name)
        self.assertNotContains(self.client.get(reverse('web_testing:environments'), {'product': other.pk}), self.env.name)
        self.submit()
        self.assertContains(self.client.get(reverse('web_testing:runs'), {'version': self.version.pk, 'status': 'queued'}), self.suite.name)
        self.assertNotContains(self.client.get(reverse('web_testing:runs'), {'status': 'passed'}), 'Login config')
        self.assertEqual(self.client.get(reverse('web_testing:suites'), {'product': 'invalid'}).status_code, 404)

    @override_settings(USE_TZ=True)
    def test_list_datetime_uses_account_zone_and_date_boundary(self):
        UserPreference.objects.update_or_create(user=self.owner, defaults={'time_zone': 'Asia/Shanghai'})
        self.submit()
        run = WebRun.objects.get()
        WebRun.objects.filter(pk=run.pk).update(created=datetime(2026, 1, 1, 20, 15, tzinfo=datetime_timezone.utc))
        response = self.client.get(reverse('web_testing:runs'), {'after': '2026-01-02', 'before': '2026-01-02'})
        self.assertContains(response, '2026-01-02 04:15:00')
        self.assertNotContains(self.client.get(reverse('web_testing:runs'), {'before': '2026-01-01'}), '2026-01-02 04:15:00')

    @override_settings(USE_TZ=False, TIME_ZONE='Etc/UTC')
    def test_naive_database_dates_also_use_account_zone(self):
        UserPreference.objects.update_or_create(user=self.owner, defaults={'time_zone': 'Asia/Shanghai'})
        self.submit()
        run = WebRun.objects.get()
        WebRun.objects.filter(pk=run.pk).update(created=datetime(2026, 1, 1, 20, 15))
        response = self.client.get(reverse('web_testing:runs'), {'after': '2026-01-02', 'before': '2026-01-02'})
        self.assertContains(response, '2026-01-02 04:15:00')

    def test_changed_release_version_is_not_relabelled_on_archive(self):
        self.submit()
        run = WebRun.objects.get()
        self.finish(run)
        new_version = VersionFactory(product=self.product)
        self.build.version = new_version
        self.build.save()
        self.plan.product_version = new_version
        self.plan.save()
        with self.assertRaisesMessage(ValueError, '发布版本已变更'):
            self.archive(run)
        self.assertFalse(AutomationArchive.objects.exists())

    def test_new_forms_inherit_project_and_plan_page_links_to_web_source(self):
        for name in ('case_new', 'suite_new', 'environment_new'):
            response = self.client.get(reverse('web_testing:' + name), {'product': self.product.pk})
            self.assertEqual(response.context['form'].initial['product'], self.product.pk)
        self.submit()
        run = WebRun.objects.get()
        response = self.client.get(reverse('test_plan_url', args=[self.plan.pk]))
        self.assertContains(response, reverse('web_testing:run', args=[run.pk]))

    def test_tokens_are_required_and_bad_input_has_no_side_effects(self):
        self.assertEqual(self.submit(token='invalid').status_code, 200)
        self.assertEqual(self.submit(execution_mode='').status_code, 200)
        self.assertFalse(WebRun.objects.exists())
        self.assertFalse(TestRun.objects.exists())
