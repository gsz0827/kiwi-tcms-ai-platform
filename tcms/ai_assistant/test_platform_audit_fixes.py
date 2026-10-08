import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.test import TestCase, SimpleTestCase, override_settings
from django.urls import reverse
from tcms.tests.factories import (
    UserFactory, ProductFactory, CategoryFactory, VersionFactory,
    TestPlanFactory, TestRunFactory, TestCaseFactory, TestExecutionFactory,
)
from tcms.testruns.models import TestExecutionStatus
from tcms.web_testing.models import WebCase, WebSuite, WebEnvironment
from tcms.web_testing.forms import SuiteForm
from tcms.web_testing.execution_config import suite_snapshot
from tcms.web_testing.workflow import SubmitForm
from . import roles
from .crypto import encrypt_api_key
from .models import AIRequest, AIRequirementVersion, AIJob, ProjectResourceFolder, ProjectResourceAssignment
from .quality_metrics import execution_metrics
from .views import _require_folder_management_permission


class RequirementDirectoryPermissionTests(TestCase):
    def setUp(self):
        self.creator, self.member, self.outsider, self.viewer = [UserFactory() for _ in range(4)]
        self.product, self.other_product = ProductFactory(), ProductFactory()
        roles.add_product_member(self.member, self.product)
        roles.add_product_member(self.viewer, self.product)
        self.viewer.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.requirement = AIRequest.objects.create(created_by=self.creator,
            category=CategoryFactory(product=self.product), title='Shared requirement', requirement='A feature')
        self.folder = ProjectResourceFolder.objects.create(product=self.product,
            resource_type='requirement', name='Before', created_by=self.creator, updated_by=self.creator)

    def post(self, user, route_name, args=None, **data):
        self.client.force_login(user)
        return self.client.post(reverse('ai_assistant:' + route_name, args=args), data, secure=True)

    def test_member_without_global_permissions_can_manage_shared_folder(self):
        self.assertFalse(self.member.has_perm(roles.PERM_MANAGE_REQUIREMENT))
        self.assertEqual(self.post(self.member, 'rename_resource_folder', [self.folder.pk], name='After').status_code, 302)
        self.assertEqual(self.post(self.member, 'assign_resource_folder', resource_type='requirement',
                                  object_id=self.requirement.pk, folder=self.folder.pk).status_code, 302)
        self.assertTrue(ProjectResourceAssignment.objects.filter(object_id=self.requirement.pk).exists())

    def test_readonly_and_outsider_cannot_create_rename_delete_move_or_assign(self):
        for user in (self.viewer, self.outsider):
            for name, args, data in (
                ('create_resource_folder', None, dict(resource_type='requirement', product=self.product.pk, name='Bad')),
                ('rename_resource_folder', [self.folder.pk], dict(name='Bad')),
                ('delete_resource_folder', [self.folder.pk], {}),
                ('move_resource_folder', [self.folder.pk], dict(parent='')),
                ('assign_resource_folder', None, dict(resource_type='requirement', object_id=self.requirement.pk, folder=self.folder.pk)),
            ):
                with self.subTest(user=user.pk, action=name):
                    self.assertIn(self.post(user, name, args, **data).status_code, (403, 404))
                    self.folder.refresh_from_db()
                    self.assertEqual(self.folder.name, 'Before')
                    self.assertIsNone(self.folder.parent_id)
                    self.assertEqual(ProjectResourceFolder.objects.count(), 1)
                    self.assertFalse(ProjectResourceAssignment.objects.exists())

    def test_member_cannot_create_in_another_project(self):
        self.assertEqual(self.post(self.member, 'create_resource_folder', resource_type='requirement',
                                  product=self.other_product.pk, name='Bad').status_code, 403)

    def test_inactive_and_anonymous_denied_in_backend_guard(self):
        from django.contrib.auth.models import AnonymousUser
        self.member.is_active = False
        for user in (self.member, AnonymousUser()):
            with self.assertRaises(PermissionDenied):
                _require_folder_management_permission(user, 'requirement', self.product)

    def test_own_requirement_does_not_grant_shared_directory_management(self):
        self.assertFalse(roles.can_manage_requirement_directories(self.creator, self.product))
        self.assertFalse(roles.can_manage_requirement_directories(self.creator, self.other_product))
        self.assertEqual(self.post(self.creator, 'rename_resource_folder', [self.folder.pk], name='Bad').status_code, 403)
        self.assertEqual(self.post(self.creator, 'assign_resource_folder', resource_type='requirement',
                                  object_id=self.requirement.pk, folder=self.folder.pk).status_code, 403)
        self.assertTrue(roles.requirement_directory_products(self.creator).filter(pk=self.product.pk).exists())
        page = self.client.get(reverse('ai_assistant:index'), secure=True)
        self.assertContains(page, self.requirement.title)
        self.assertNotContains(page, 'data-directory-node="' + str(self.folder.pk) + '"')
        roles.add_product_member(self.creator, self.product)
        self.assertTrue(roles.can_manage_requirement_directories(self.creator, self.product))

    def test_outsider_tree_does_not_expose_requirement_folders(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse('ai_assistant:index'), secure=True)
        self.assertNotContains(response, 'data-directory-node="' + str(self.folder.pk) + '"')


class IndependentRequirementTests(TestCase):
    def setUp(self):
        self.owner = UserFactory()
        self.category = CategoryFactory()
        self.client.force_login(self.owner)
        self.data = dict(title='Saved without AI', requirement='Feature explanation', category=self.category.pk,
                         submission_token=str(uuid.uuid4()), action='save', acceptance_criteria='1. Returns success')

    def test_new_page_and_list_have_distinct_primary_actions(self):
        page = self.client.get(reverse('ai_assistant:requirement_new'), secure=True)
        self.assertContains(page, '保存需求')
        self.assertNotContains(page, 'id="generate-button"')
        page = self.client.get(reverse('ai_assistant:index'), secure=True)
        self.assertContains(page, reverse('ai_assistant:requirement_new'))
        self.assertNotContains(page, 'id="generation-form"')

    @patch('tcms.ai_assistant.jobs.enqueue_ai_job')
    def test_save_without_model_creates_version_but_never_calls_ai(self, enqueue):
        response = self.client.post(reverse('ai_assistant:requirement_new'), self.data, secure=True)
        self.assertEqual(response.status_code, 302)
        requirement = AIRequest.objects.get(created_by=self.owner)
        self.assertEqual(response.url, reverse('ai_assistant:requirement_trace', args=[requirement.pk]))
        self.assertEqual(requirement.document_sections['acceptance_criteria'], '1. Returns success')
        self.assertEqual(AIRequirementVersion.objects.filter(request=requirement).count(), 1)
        self.assertFalse(AIJob.objects.exists())
        enqueue.assert_not_called()

    def test_repeat_save_is_idempotent_and_changed_token_content_rejected(self):
        url = reverse('ai_assistant:requirement_new')
        first = self.client.post(url, self.data, secure=True)
        replay = self.client.post(url, self.data, secure=True)
        self.assertEqual(first.url, replay.url)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(AIRequirementVersion.objects.count(), 1)
        altered = dict(self.data, title='Different')
        response = self.client.post(url, altered, secure=True)
        self.assertContains(response, '这份表单已提交过其他内容')
        self.assertEqual(AIRequest.objects.get().title, self.data['title'])

    def test_readonly_cannot_save_even_by_legacy_post(self):
        self.owner.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        for name in ('index', 'requirement_new'):
            self.assertEqual(self.client.post(reverse('ai_assistant:' + name), self.data, secure=True).status_code, 403)
        self.assertFalse(AIRequest.objects.exists())

    def test_invalid_save_stays_in_new_page_and_preserves_content(self):
        response = self.client.post(reverse('ai_assistant:requirement_new'), dict(self.data, title=''), secure=True)
        self.assertTemplateUsed(response, 'ai_assistant/requirement_new.html')
        self.assertContains(response, 'Feature explanation')
        self.assertFalse(AIRequest.objects.exists())


@override_settings(WEB_TEST_ALLOWED_ORIGINS=['https://kiwi-web:8443'])
class WebSuiteOrderTests(TestCase):
    def test_picker_and_hidden_order_do_not_inherit_text_input_height(self):
        form = SuiteForm(owner=self.owner, instance=self.suite)
        self.assertNotIn('class', form.fields['cases'].widget.attrs)
        self.assertNotIn('class', form.fields['ordered_case_ids'].widget.attrs)

    def setUp(self):
        self.owner, self.product = UserFactory(), ProductFactory()
        self.env = WebEnvironment.objects.create(owner=self.owner, product=self.product, name='Env', base_url='https://kiwi-web:8443')
        steps = encrypt_api_key(json.dumps([{'action': 'goto', 'value': '/'}, {'action': 'assert_visible', 'selector': 'body'}]))
        self.first, self.second = [WebCase.objects.create(owner=self.owner, product=self.product, name=name, steps_encrypted=steps)
                                   for name in ('First', 'Second')]
        self.suite = WebSuite.objects.create(owner=self.owner, product=self.product, name='Suite', environment=self.env,
                                             case_ids=[self.first.pk, self.second.pk])

    def data(self, order):
        return dict(name='Suite', product=self.product.pk, environment=self.env.pk,
                    cases=[self.first.pk, self.second.pk], ordered_case_ids=json.dumps(order))

    def test_order_save_reopen_and_snapshot_match(self):
        order = [self.second.pk, self.first.pk]
        form = SuiteForm(self.data(order), owner=self.owner, instance=self.suite)
        self.assertTrue(form.is_valid(), form.errors)
        suite = form.save()
        self.assertEqual(suite.case_ids, order)
        self.assertEqual(SuiteForm(owner=self.owner, instance=suite).initial['ordered_case_ids'], order)
        self.assertEqual([row['id'] for row in suite_snapshot(suite)['cases']], order)

    def test_invalid_orders_are_rejected(self):
        for order in ([self.first.pk], [self.first.pk, self.first.pk], [True, self.second.pk], [999999, self.second.pk]):
            with self.subTest(order=order):
                self.assertFalse(SuiteForm(self.data(order), owner=self.owner, instance=self.suite).is_valid())
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.case_ids, [self.first.pk, self.second.pk])

    def test_fallback_order_preserved_without_javascript(self):
        data = self.data([])
        data.pop('ordered_case_ids')
        data['cases'].reverse()
        form = SuiteForm(data, owner=self.owner, instance=self.suite)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().case_ids, data['cases'])

    def test_datasets_repeat_same_suite_order_and_old_snapshot_unchanged(self):
        before = suite_snapshot(self.suite)
        form = SuiteForm(dict(self.data([self.second.pk, self.first.pk]), datasets='[{"v":1},{"v":2}]'),
                         owner=self.owner, instance=self.suite)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([r['id'] for r in suite_snapshot(form.save())['cases']], [self.second.pk, self.first.pk] * 2)
        self.assertEqual([r['id'] for r in before['cases']], [self.first.pk, self.second.pk])

    def test_no_plan_or_unlinked_scripts_default_to_debug(self):
        self.assertEqual(SubmitForm(owner=self.owner, suite=self.suite).initial['execution_mode'], 'debug')


class QualityScopeTests(TestCase):
    def setUp(self):
        self.owner, self.outsider = UserFactory(), UserFactory()
        self.product = ProductFactory()
        roles.add_product_member(self.owner, self.product)
        self.version = VersionFactory(product=self.product)
        self.plan = TestPlanFactory(product=self.product, product_version=self.version)
        self.run = TestRunFactory(plan=self.plan)
        self.run.build.version = self.version
        self.run.build.save()
        for weight in (1, -1, 0):
            status = TestExecutionStatus.objects.filter(weight=weight).first()
            if status is None:
                status = TestExecutionStatus.objects.create(name='Status' + str(weight), weight=weight, icon='fa-circle', color='#555555')
            TestExecutionFactory(run=self.run, status=status)

    def test_metrics_use_real_results_without_ai_analysis(self):
        counts = execution_metrics(self.owner, self.product.pk, self.version.pk, self.plan.pk)
        self.assertEqual([counts[k] for k in ('tasks', 'total', 'passed', 'failed', 'pending')], [1, 3, 1, 1, 1])
        self.assertEqual(counts['pass_rate'], 50.0)

    def test_project_outsider_has_no_execution_data(self):
        self.assertEqual(execution_metrics(self.outsider, self.product.pk)['total'], 0)

    def test_requirement_version_and_plan_filters_match_selected_scope(self):
        category = CategoryFactory(product=self.product)
        AIRequest.objects.create(created_by=self.owner, category=category, target_version=self.version, title='This version', requirement='Feature')
        AIRequest.objects.create(created_by=self.owner, category=category, title='Unversioned', requirement='Feature')
        self.client.force_login(self.owner)
        page = self.client.get(reverse('ai_assistant:dashboard'), dict(product=self.product.pk, version=self.version.pk), secure=True)
        self.assertEqual(page.context['metrics']['requirements'], 1)
        self.assertEqual(page.context['execution_metrics']['failed'], 1)
        page = self.client.get(reverse('ai_assistant:dashboard'), dict(product=self.product.pk, plan=self.plan.pk), secure=True)
        self.assertEqual(page.context['metrics']['requirements'], 0)

    def test_defect_workspace_is_distinct_and_permissions_scoped(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse('ai_assistant:defect_workspace'), secure=True)
        self.assertContains(page, '缺陷与复测')
        self.assertEqual(page.context['failure_count'], 1)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse('ai_assistant:defect_workspace'), secure=True).context['failure_count'], 0)

    def test_legacy_page_uses_only_one_jquery_and_no_admin_initializer(self):
        self.client.force_login(UserFactory(is_superuser=True))
        page = self.client.get(reverse('ai_assistant:index'), secure=True)
        self.assertNotContains(page, 'grappelli.min.js')
        page = self.client.get(reverse('bugs-search'), secure=True)
        self.assertNotContains(page, 'grappelli/jquery/jquery.min.js')
        self.assertContains(page, 'js/related_namespace.js')

class DateTimeDisplayTests(SimpleTestCase):
    def test_naive_and_aware_dates_use_selected_zone_with_or_without_use_tz(self):
        from datetime import datetime, timezone
        from django.template import Context, Template
        template = Template('{% load display_time %}{{ value|in_display_zone:zone|date:"Y-m-d H:i:s" }}')
        for use_tz in (False, True):
            with override_settings(USE_TZ=use_tz, TIME_ZONE='Etc/UTC'):
                for value in (datetime(2026, 10, 7, 18, 23, 50), datetime(2026, 10, 7, 18, 23, 50, tzinfo=timezone.utc)):
                    for zone, expected in [('Asia/Shanghai', '2026-10-08 02:23:50'), ('Etc/UTC', '2026-10-07 18:23:50')]:
                        with self.subTest(use_tz=use_tz, value=value, zone=zone):
                            self.assertEqual(template.render(Context({'value':value, 'zone':zone})), expected)

    def test_no_preference_uses_beijing_and_empty_value_remains_empty(self):
        from datetime import datetime
        from django.template import Context, Template
        template = Template('{% load display_time %}{{ value|in_display_zone|date:"Y-m-d H:i:s" }}')
        with override_settings(TIME_ZONE='Etc/UTC'):
            self.assertEqual(template.render(Context({'value':datetime(2026, 10, 7, 18, 23, 50)})), '2026-10-08 02:23:50')
            self.assertEqual(template.render(Context({'value':None})), '')

class SavedRequirementAnalysisTests(TestCase):
    def setUp(self):
        IndependentRequirementTests.setUp(self)
        self.source = AIRequest.objects.create(created_by=self.owner, category=self.category,
                                              title='Saved', requirement='Saved content')

    def config(self):
        from .models import AIModelConfig
        return AIModelConfig.objects.create(owner=self.owner, name='Fake', model='test', is_active=True,
                                             api_base='https://model.example.test/v1')

    def url(self):
        return reverse('ai_assistant:analyze_saved_requirement', args=[self.source.pk])

    def test_missing_model_does_not_remove_or_duplicate_saved_requirement(self):
        page = self.client.post(self.url(), {'source_version':self.source.version}, secure=True)
        self.assertEqual(page.url, reverse('ai_assistant:model_settings'))
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertFalse(AIJob.objects.exists())

    def test_analysis_submission_reuses_saved_requirement_and_active_job(self):
        self.config()
        a = self.client.post(self.url(), {'source_version':self.source.version}, secure=True)
        b = self.client.post(self.url(), {'source_version':self.source.version}, secure=True)
        self.assertEqual(a.url, b.url)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(AIJob.objects.count(), 1)
        self.assertEqual(AIJob.objects.get().payload['source_version'], 1)

    def test_outsider_and_readonly_cannot_analyze_saved_requirement(self):
        self.client.force_login(UserFactory())
        self.assertEqual(self.client.post(self.url(), {'source_version':1}, secure=True).status_code, 404)
        self.owner.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url(), {'source_version':1}, secure=True).status_code, 403)
        self.assertFalse(AIJob.objects.exists())

    def test_stale_form_cannot_queue_analysis(self):
        self.config()
        page = self.client.post(self.url(), {'source_version':0}, secure=True)
        self.assertEqual(page.url, reverse('ai_assistant:requirement_trace', args=[self.source.pk]))
        self.assertFalse(AIJob.objects.exists())

    @patch('tcms.ai_assistant.jobs.analyze_requirement')
    def test_worker_rejects_changed_version_before_spending_ai_call(self, analyze):
        self.config()
        self.client.post(self.url(), {'source_version':1}, secure=True)
        AIRequest.objects.filter(pk=self.source.pk).update(version=2)
        from .jobs import execute_next_job
        execute_next_job()
        analyze.assert_not_called()
        self.assertEqual(AIJob.objects.get().status, 'failed')

class RunOutcomeDisplayTests(SimpleTestCase):
    def test_finished_does_not_mean_passed_and_missing_results_cannot_pass(self):
        from types import SimpleNamespace
        from .run_outcomes import task_state, test_outcome
        def run():
            return SimpleNamespace(status='completed', error='', outcome_total=2, outcome_passed=2,
                                   outcome_failed=0, outcome_errors=0, outcome_unknown=0)
        # Build overridden values explicitly to avoid changing the source lifecycle.
        passing = run()
        self.assertEqual(task_state(passing), '已结束')
        self.assertEqual(test_outcome(passing)['label'], '通过')
        for updates, expected in [({'outcome_failed':1, 'outcome_passed':1}, '失败'),
                                  ({'outcome_passed':1}, '未完成'), ({'outcome_errors':1}, '异常'),
                                  ({'error':'worker interrupted'}, '异常'), ({'status':'running'}, '待完成'),
                                  ({'outcome_unknown':1}, '异常'), ({'status':'cancelled'}, '未完成')]:
            value = run()
            for key, field in updates.items():
                setattr(value, key, field)
            self.assertEqual(test_outcome(value)['label'], expected)
