import json
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from tcms.tests.factories import UserFactory, ProductFactory, VersionFactory, BuildFactory, TestPlanFactory, TestCaseFactory
from tcms.testruns.models import TestExecution, TestExecutionStatus, TestRun
from tcms.testcases.models import TestCaseStatus
from tcms.web_testing.models import WebRun, WebResult, WebCase, WebSuite
from tcms.web_testing.execution_config import suite_snapshot
from .automation_archive import publish
from .crypto import encrypt_api_key, decrypt_api_key
from .engineering import verify_defect_regression
from .models import AIDefectDraft, AITestReport, AIRegressionVerification, APICase, APIEnvironment
from .regression_tasks import create_task, save_verification, RegressionTaskForm
from .services import build_test_run_snapshot, verify_regression


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class ExecutionChainTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True)
        self.product = ProductFactory()
        self.version = VersionFactory(product=self.product)
        self.plan = TestPlanFactory(product=self.product, product_version=self.version)
        self.build = BuildFactory(version=self.version)
        self.fixed_build = BuildFactory(version=self.version)
        self.case = TestCaseFactory(category__product=self.product,
                case_status=TestCaseStatus.objects.filter(is_confirmed=True).first())
        self.case.save()  # Factory deliberately mutes the history post-save signal.
        self.states = {key: TestExecutionStatus.objects.create(name='Chain '+key, weight=weight)
                       for key, weight in [('passed',1),('failed',-1),('pending',0)]}
        self.plan.add_case(self.case)
        self.origin = TestRun.objects.create(plan=self.plan, build=self.build, manager=self.owner, summary='Source')
        self.failed = self.execution(self.origin, self.states['failed'])
        self.defect = AIDefectDraft.objects.create(owner=self.owner, execution=self.failed, title='Broken login', status='fixed')
        self.report = AITestReport.objects.create(owner=self.owner, test_run=self.origin, title='Source report',
                                                 metrics_snapshot=build_test_run_snapshot(self.origin))
        self.data = {'plan': self.plan, 'build': self.fixed_build, 'submission_token': uuid.uuid4(), 'notes': 'fixed'}

    def execution(self, run, status, case=None):
        case = case or self.case
        return TestExecution.objects.create(run=run, build=run.build, case=case,
                case_text_version=case.history.latest().history_id, status=status, assignee=self.owner)

    def automate(self, kind='web', version=None, case_id=None):
        frozen = self.case.history.latest().history_id if version is None else version
        row = {'name': 'Private config name', 'business_case_id': case_id or self.case.pk,
               'business_case_version': frozen}
        run = WebRun.objects.create(owner=self.owner, product=self.product, name='Smoke', submission_token=uuid.uuid4(),
                                    status='failed', total=1, snapshot_encrypted=encrypt_api_key(json.dumps({'cases':[row]})))
        WebResult.objects.create(run=run, position=1, name='Private config name', status='failed', error='secret-password')
        return run

    def archive(self, run):
        return publish(self.owner, 'web', run.pk, dict(self.states, plan=self.plan, build=self.build, defects=['1'], confirm=True))

    def test_archive_reuses_business_case_and_version_without_mutation(self):
        run = self.automate()
        frozen = self.case.history.latest().history_id
        self.case.text = 'Updated after submission'; self.case.save()
        original_count = type(self.case).objects.count()
        archived = self.archive(run)
        execution = archived.test_run.executions.get()
        self.assertEqual(execution.case_id, self.case.pk)
        self.assertEqual(execution.case_text_version, frozen)
        self.assertEqual(type(self.case).objects.count(), original_count)
        self.assertEqual(self.archive(run).pk, archived.pk)
        self.assertNotIn('secret-password', json.dumps(archived.report.metrics_snapshot))
        self.case.refresh_from_db(); self.assertEqual(self.case.text, 'Updated after submission')

    def test_archived_failure_matches_original_case_in_new_regression(self):
        archived = self.archive(self.automate())
        defect = AIDefectDraft.objects.get(execution__run=archived.test_run)
        target = create_task(self.owner, defect, 'defect', self.data)
        pending = target.executions.get()
        self.assertEqual(pending.case_id, self.case.pk)
        self.assertEqual(verify_defect_regression(defect, target)[0], 'incomplete')
        pending.status = self.states['passed']; pending.stop_date = timezone.now(); pending.save()
        self.assertEqual(verify_defect_regression(defect, target)[0], 'passed')
        self.assertEqual(verify_regression(archived.report, target)[0], 'passed')

    def test_unbound_and_wrong_project_archives_are_atomic(self):
        run = self.automate()
        run.snapshot_encrypted = encrypt_api_key(json.dumps({'cases':[{'name':'Unbound'}]})); run.save()
        before = TestRun.objects.count()
        with self.assertRaises(ValueError): self.archive(run)
        self.assertEqual(TestRun.objects.count(), before)
        foreign = TestCaseFactory()
        with self.assertRaises(ValueError): self.archive(self.automate(case_id=foreign.pk))

    def test_foreign_history_version_cannot_archive(self):
        foreign = TestCaseFactory()
        foreign.save()
        with self.assertRaises(ValueError): self.archive(self.automate(version=foreign.history.latest().history_id))

    def test_legacy_linked_snapshot_uses_submission_time_version(self):
        run = self.automate()
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        del snapshot['cases'][0]['business_case_version']
        run.snapshot_encrypted = encrypt_api_key(json.dumps(snapshot)); run.save()
        frozen = self.case.history.latest().history_id
        self.case.text = 'Edited later'; self.case.save()
        self.assertEqual(self.archive(run).test_run.executions.get().case_text_version, frozen)

    def test_new_web_snapshot_freezes_business_case_version(self):
        web = WebCase.objects.create(owner=self.owner, product=self.product, test_case=self.case, name='Web',
              steps_encrypted=encrypt_api_key(json.dumps([{'action':'goto','value':'/'},
                                                          {'action':'assert_visible','selector':'body'}])))
        suite = WebSuite.objects.create(owner=self.owner, product=self.product, name='Suite',
              base_url='https://kiwi-web:8443', case_ids=[web.pk])
        self.assertEqual(suite_snapshot(suite)['cases'][0]['business_case_version'], self.case.history.latest().history_id)

    @override_settings(API_AUTOMATION_ALLOWED_ORIGINS=['http://api-demo:8080'])
    def test_new_api_snapshot_freezes_business_case_version(self):
        from .api_runner import submit_run
        env = APIEnvironment.objects.create(owner=self.owner, product=self.product, name='API', base_url='http://api-demo:8080')
        case = APICase.objects.create(owner=self.owner, product=self.product, test_case=self.case, name='API', path='/')
        run = submit_run(self.owner, self.product, {'environment':env,'cases':[case], 'submission_token':uuid.uuid4()})
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        self.assertEqual(snapshot['cases'][0]['business_case_version'], self.case.history.latest().history_id)

    def test_original_run_and_other_project_rejected(self):
        for verify, source in [(verify_defect_regression,self.defect),(verify_regression,self.report)]:
            with self.assertRaises(ValueError): verify(source,self.origin)
        other = TestPlanFactory()
        target = TestRun.objects.create(plan=other, build=BuildFactory(version=other.product_version), manager=self.owner)
        with self.assertRaises(ValueError): verify_defect_regression(self.defect,target)

    def test_old_target_run_is_not_regression_evidence(self):
        target = TestRun.objects.create(plan=self.plan, build=self.fixed_build, manager=self.owner)
        self.execution(target,self.states['passed'])
        target.history.update(history_date=timezone.now()-timedelta(days=1))
        with self.assertRaises(ValueError): verify_defect_regression(self.defect,target)

    def test_missing_pending_and_failed_results_are_not_passes(self):
        target = TestRun.objects.create(plan=self.plan, build=self.fixed_build, manager=self.owner)
        self.assertEqual(verify_defect_regression(self.defect,target)[1]['outcome'],'missing')
        execution = self.execution(target,self.states['pending'])
        self.assertEqual(verify_defect_regression(self.defect,target)[0],'incomplete')
        execution.status = self.states['failed']; execution.save()
        self.assertEqual(verify_defect_regression(self.defect,target)[0],'failed')

    def test_run_build_and_execution_build_must_match(self):
        target = create_task(self.owner,self.defect,'defect',self.data)
        execution = target.executions.get()
        execution.status = self.states['passed']; execution.build = self.build; execution.save()
        self.assertEqual(verify_defect_regression(self.defect,target)[0],'incomplete')
        target.build = BuildFactory(); target.save()
        with self.assertRaises(ValueError): verify_defect_regression(self.defect,target)

    def test_task_is_pending_and_idempotent_even_after_verification(self):
        target = create_task(self.owner,self.defect,'defect',self.data)
        self.assertEqual(target.executions.get().status.weight,0)
        self.assertIsNone(target.stop_date)
        self.assertEqual(create_task(self.owner,self.defect,'defect',self.data).pk,target.pk)
        status,result = verify_defect_regression(self.defect,target)
        save_verification(owner=self.owner,defect_draft=self.defect,regression_run=target,status=status,result=result,notes='')
        self.assertEqual(AIRegressionVerification.objects.count(),1)
        self.assertEqual(create_task(self.owner,self.defect,'defect',self.data).pk,target.pk)

    def test_report_task_contains_failed_cases_only_and_keeps_snapshot(self):
        passed = TestCaseFactory(category__product=self.product)
        passed.save()
        self.execution(self.origin,self.states['passed'],passed)
        original = json.loads(json.dumps(self.report.metrics_snapshot))
        target = create_task(self.owner,self.report,'report',self.data)
        self.assertEqual(list(target.executions.values_list('case_id',flat=True)),[self.case.pk])
        self.report.refresh_from_db(); self.assertEqual(self.report.metrics_snapshot,original)

    def test_multiple_failed_instances_require_full_coverage(self):
        self.execution(self.origin,self.states['failed'])
        self.report.metrics_snapshot = build_test_run_snapshot(self.origin); self.report.save()
        target = create_task(self.owner,self.report,'report',self.data)
        self.assertEqual(target.executions.count(),2)
        execution = target.executions.first(); execution.delete()
        target.executions.update(status=self.states['passed'])
        self.assertEqual(verify_regression(self.report,target)[0],'incomplete')

    def test_form_rejects_cross_version_build(self):
        payload = dict(plan=self.plan.pk,build=BuildFactory().pk,submission_token=uuid.uuid4())
        self.assertFalse(RegressionTaskForm(payload,user=self.owner,source=self.origin).is_valid())

    def test_unreviewed_case_and_no_failures_cannot_create_task(self):
        self.case.case_status.is_confirmed = False; self.case.case_status.save()
        with self.assertRaises(ValueError): create_task(self.owner,self.defect,'defect',self.data)
        self.report.metrics_snapshot = {'failure_details':[]}
        with self.assertRaises(ValueError): create_task(self.owner,self.report,'report',self.data)

    def test_readonly_and_unprivileged_cannot_create_or_verify(self):
        from django.contrib.auth.models import Group
        readonly = UserFactory(is_superuser=True)
        readonly.groups.add(Group.objects.get_or_create(name=roles_name())[0])
        with self.assertRaises(PermissionDenied): create_task(readonly,self.defect,'defect',self.data)
        with self.assertRaises(PermissionDenied): create_task(UserFactory(),self.defect,'defect',self.data)
        self.client.force_login(readonly)
        self.assertEqual(self.client.get(reverse('ai_assistant:new_regression_task',args=['report',self.report.pk]),secure=True).status_code,403)

    def test_automation_can_publish_into_fresh_regression_task(self):
        target = create_task(self.owner, self.defect, 'defect', self.data)
        run = self.automate()
        run.started = timezone.now(); run.finished = timezone.now(); run.save()
        payload = dict(self.states, plan=self.plan, build=self.fixed_build, target_run=target, defects=[], confirm=True)
        before = TestExecution.objects.count()
        archived = publish(self.owner,'web',run.pk,payload)
        self.assertEqual(archived.test_run_id,target.pk)
        self.assertEqual(TestExecution.objects.count(),before)
        self.assertEqual(verify_defect_regression(self.defect,target)[0],'failed')

    def test_automation_cannot_overwrite_results_or_backfill_old_run(self):
        target = create_task(self.owner, self.defect, 'defect', self.data)
        run = self.automate()
        payload = dict(self.states, plan=self.plan, build=self.fixed_build, target_run=target, defects=[], confirm=True)
        execution = target.executions.get(); execution.status=self.states['passed']; execution.save()
        with self.assertRaises(ValueError): publish(self.owner,'web',run.pk,payload)
        execution.refresh_from_db(); self.assertEqual(execution.status,self.states['passed'])
        execution.status=self.states['pending']; execution.save()
        run.created=timezone.now()-timedelta(days=1); run.save()
        with self.assertRaises(ValueError): publish(self.owner,'web',run.pk,payload)

    def test_regression_copies_execution_properties_and_rejects_other_environment(self):
        from tcms.testruns.models import TestExecutionProperty
        TestExecutionProperty.objects.create(execution=self.failed,name='browser',value='Firefox')
        target = create_task(self.owner,self.defect,'defect',self.data)
        execution = target.executions.get()
        self.assertEqual(list(execution.properties().values_list('name','value')),[('browser','Firefox')])
        execution.status=self.states['passed']; execution.save()
        self.assertEqual(verify_defect_regression(self.defect,target)[0],'passed')
        execution.properties().update(value='Chromium')
        self.assertEqual(verify_defect_regression(self.defect,target)[0],'incomplete')

    def test_report_snapshot_context_does_not_expose_values_and_detects_change(self):
        from tcms.testruns.models import TestExecutionProperty
        TestExecutionProperty.objects.create(execution=self.failed,name='token',value='private-env-secret')
        self.report.metrics_snapshot=build_test_run_snapshot(self.origin); self.report.save()
        self.assertNotIn('private-env-secret',json.dumps(self.report.metrics_snapshot))
        target = create_task(self.owner,self.report,'report',self.data)
        self.failed.properties().update(value='changed')
        with self.assertRaises(ValueError): verify_regression(self.report,target)

    def test_http_create_link_and_invalid_verification_do_not_crash(self):
        self.client.force_login(self.owner)
        url = reverse('ai_assistant:new_regression_task',args=['defect',self.defect.pk])
        self.assertContains(self.client.get(url,secure=True),'创建复测任务')
        payload = {'plan':self.plan.pk,'build':self.fixed_build.pk,'submission_token':str(uuid.uuid4())}
        response = self.client.post(url,payload,secure=True)
        self.assertEqual(response.status_code,302)
        target = AIRegressionVerification.objects.get().regression_run
        self.assertEqual(response.url,reverse('testruns-get',args=[target.pk]))
        self.assertEqual(self.client.post(url,payload,secure=True).url,response.url)
        verification = reverse('ai_assistant:create_defect_regression',args=[self.defect.pk])
        self.assertEqual(self.client.post(verification,{'regression_run_id':self.origin.pk},secure=True).status_code,302)
        self.defect.refresh_from_db(); self.assertEqual(self.defect.status,'fixed')
        execution = target.executions.get(); execution.status = self.states['failed']; execution.save()
        self.client.post(verification,{'regression_run_id':target.pk},secure=True)
        self.defect.refresh_from_db(); self.assertEqual(self.defect.status,'in_progress')
        self.assertEqual(AIRegressionVerification.objects.get().status,'failed')


def roles_name():
    from .roles import ROLE_VIEWER
    return ROLE_VIEWER
