import json
import uuid
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from tcms.tests.factories import UserFactory, ProductFactory, TestCaseFactory, TestPlanFactory, BuildFactory, VersionFactory
from tcms.testruns.models import TestExecutionStatus
from tcms.web_testing.models import WebCase, WebSuite, WebEnvironment, WebRun, WebResult
from tcms.web_testing.forms import SuiteForm
from tcms.web_testing.execution_config import EnvironmentForm, suite_snapshot
from tcms.ai_assistant.models import APICase, APIEnvironment, APIRun, APIResult, APISuite, AutomationArchive, AIDefectDraft
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from tcms.ai_assistant.automation_data import datasets
from tcms.ai_assistant.api_runner import submit_run, execute_run
from tcms.ai_assistant.automation_archive import ArchiveForm, publish


def encrypted(value): return encrypt_api_key(json.dumps(value))


class WebEnvironmentTests(TestCase):
    def setUp(self):
        self.owner, self.other = UserFactory(), UserFactory()
        self.product = ProductFactory()
        self.case = WebCase.objects.create(owner=self.owner,product=self.product,name='Business',steps_encrypted=encrypted([
            {'action':'goto','value':'/users/{{user_id}}'}, {'action':'assert_text','selector':'body','value':'{{expected}}'}]))
        self.login = WebCase.objects.create(owner=self.owner,product=self.product,name='Login',steps_encrypted=encrypted([
            {'action':'goto','value':'/login'}, {'action':'assert_visible','selector':'body'}]))
        self.env = WebEnvironment.objects.create(owner=self.owner,product=self.product,name='QA',base_url='https://kiwi-web:8443',
            setup_case=self.login,variables_encrypted=encrypted({'expected':'ok'}))
        self.suite = WebSuite.objects.create(owner=self.owner,product=self.product,name='Smoke',environment=self.env,
            base_url='https://kiwi-web:8443',case_ids=[self.case.pk],datasets_encrypted=encrypted([{'user_id':1},{'user_id':2}]))

    def test_expanded_snapshot_freezes_rows_environment_and_login(self):
        snapshot=suite_snapshot(self.suite)
        self.assertEqual(len(snapshot['cases']),2)
        self.assertEqual(snapshot['cases'][1]['steps'][0]['value'],'/users/2')
        self.assertEqual(snapshot['cases'][0]['setup_steps'][0]['value'],'/login')
        self.assertEqual(snapshot['cases'][1]['dataset'],1)
        self.env.base_url='https://changed.invalid'; self.env.save()
        self.assertEqual(snapshot['base_url'],'https://kiwi-web:8443')

    def test_missing_variable_and_limits_fail_before_queue(self):
        self.suite.datasets_encrypted=encrypted([])
        with self.assertRaises(ValueError): suite_snapshot(self.suite)
        for value in [[{}]*11, {'id':1}, [{'bad-name':1}], [{'value':None}]]:
            with self.assertRaises(ValueError): datasets(value)

    def test_environment_form_encrypts_variables_and_rejects_foreign_login(self):
        data={'product':self.product.pk,'name':'QA2','base_url':'https://kiwi-web:8443','variables':'{"password":"private-login"}','setup_case':self.login.pk}
        form=EnvironmentForm(data,owner=self.owner)
        self.assertTrue(form.is_valid(),form.errors)
        saved=form.save(); self.assertNotIn('private-login',saved.variables_encrypted)
        self.assertEqual(json.loads(decrypt_api_key(saved.variables_encrypted))['password'],'private-login')
        self.assertFalse(EnvironmentForm(data,owner=self.other).is_valid())

    def test_suite_form_saves_data_and_requires_same_product_environment(self):
        data={'product':self.product.pk,'name':'Data suite','environment':self.env.pk,'cases':[self.case.pk],'datasets':'[{"user_id":3}]'}
        form=SuiteForm(data,owner=self.owner)
        self.assertTrue(form.is_valid(),form.errors)
        saved=form.save(); self.assertEqual(json.loads(decrypt_api_key(saved.datasets_encrypted)),[{'user_id':3}])
        data['product']=ProductFactory().pk
        self.assertFalse(SuiteForm(data,owner=self.owner).is_valid())

    def test_environment_pages_and_owner_isolation(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('web_testing:environments'),secure=True).status_code,200)
        response=self.client.get(reverse('web_testing:environment_edit',args=[self.env.pk]),secure=True)
        self.assertEqual(response.status_code,200); self.assertIn('no-store',response['Cache-Control'])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('web_testing:environment_edit',args=[self.env.pk]),secure=True).status_code,404)


@override_settings(API_AUTOMATION_ALLOWED_ORIGINS=['http://api-demo:8080'])
class APIDatasetTests(TestCase):
    def setUp(self):
        self.owner,self.product=UserFactory(),ProductFactory()
        self.env=APIEnvironment.objects.create(owner=self.owner,product=self.product,name='API',base_url='http://api-demo:8080',variables={})
        self.case=APICase.objects.create(owner=self.owner,product=self.product,name='Users',path='/users/{{user_id}}')
        self.data=dict(environment=self.env,cases=[self.case],submission_token=uuid.uuid4(),datasets=[{'user_id':1},{'user_id':2}],share_cookies=True)

    def test_expansion_idempotency_and_different_rows_rejected(self):
        run=submit_run(self.owner,self.product,self.data)
        self.assertEqual(run.results.count(),2)
        self.assertEqual(submit_run(self.owner,self.product,self.data).pk,run.pk)
        self.data['datasets']=[{'user_id':3}]
        with self.assertRaises(ValueError): submit_run(self.owner,self.product,self.data)

    @patch('tcms.ai_assistant.api_runner.send_http')
    def test_rows_execute_with_separate_cookie_jars(self,send):
        seen=[]
        def reply(env,case):
            seen.append((case['path'],env['_cookies']))
            return 200,b'{}',1
        send.side_effect=reply
        run=submit_run(self.owner,self.product,self.data); run.status='running';run.save()
        execute_run(run);run.refresh_from_db()
        self.assertEqual(run.status,'completed')
        self.assertEqual(list(run.results.values_list('status',flat=True)),['passed','passed'])
        self.assertEqual([x[0] for x in seen],['/users/1','/users/2'])
        self.assertIsNot(seen[0][1],seen[1][1])

    def test_suite_scheduler_preserves_data(self):
        from tcms.ai_assistant.api_scheduling import suite_data
        suite=APISuite.objects.create(owner=self.owner,product=self.product,name='Daily',environment=self.env,
            case_ids=[self.case.pk],datasets_encrypted=encrypted(self.data['datasets']))
        self.assertEqual(suite_data(suite,uuid.uuid4())['datasets'],self.data['datasets'])


class ArchiveTests(TestCase):
    def setUp(self):
        self.owner=UserFactory(is_superuser=True)
        self.product=ProductFactory(); version=VersionFactory(product=self.product)
        self.plan=TestPlanFactory(product=self.product,product_version=version)
        self.build=BuildFactory(version=version)
        self.business_case = TestCaseFactory(category__product=self.product)
        self.business_case.save()
        self.states={key:TestExecutionStatus.objects.create(name='Archive '+key,weight=weight) for key,weight in [('passed',1),('failed',-1),('pending',0)]}
        self.run=WebRun.objects.create(owner=self.owner,product=self.product,name='Smoke',submission_token=uuid.uuid4(),
            status='failed',total=3,completed_count=2,snapshot_encrypted=encrypted({'cases':[{'name':name,'business_case_id':self.business_case.pk,'business_case_version':self.business_case.history.latest().history_id} for name in ['Passed','Failed','Not executed']],'secret':'private-value'}))
        WebResult.objects.create(run=self.run,position=1,name='Passed',status='passed')
        WebResult.objects.create(run=self.run,position=2,name='Failed',status='failed',error='private-error')
        self.data=dict(plan=self.plan,build=self.build,defects=['2'],confirm=True,**self.states)

    def test_archive_creates_report_and_selected_draft_once_without_secrets(self):
        saved=publish(self.owner,'web',self.run.pk,self.data)
        self.assertEqual(saved.test_run.executions.count(),3)
        self.assertEqual(saved.report.metrics_snapshot['metrics']['pending'],1)
        self.assertEqual(AIDefectDraft.objects.filter(execution__run=saved.test_run).count(),1)
        self.assertEqual(publish(self.owner,'web',self.run.pk,self.data).pk,saved.pk)
        self.assertNotIn('private',json.dumps(saved.report.metrics_snapshot))
        self.assertEqual(saved.report.approval_status,'pending')
        original=saved.report.snapshot_hash
        self.run.results.update(status='passed')
        saved.report.refresh_from_db(); self.assertEqual(saved.report.snapshot_hash,original)

    def test_no_draft_without_explicit_selection(self):
        self.data['defects']=[]
        saved=publish(self.owner,'web',self.run.pk,self.data)
        self.assertFalse(AIDefectDraft.objects.filter(execution__run=saved.test_run).exists())

    def test_active_run_and_wrong_owner_cannot_archive(self):
        self.run.status='running';self.run.save()
        with self.assertRaises(ValueError):publish(self.owner,'web',self.run.pk,self.data)
        self.client.force_login(UserFactory(is_superuser=True))
        self.assertEqual(self.client.get(reverse('ai_assistant:automation_archive',args=['web',self.run.pk]),secure=True).status_code,404)

    def test_form_rejects_wrong_version_and_passed_defect(self):
        from tcms.ai_assistant.automation_archive import source_rows
        payload={key:value.pk for key,value in self.states.items()}
        payload.update(plan=self.plan.pk,build=self.build.pk,defects=['1'],confirm=True)
        form=ArchiveForm(payload,owner=self.owner,product=self.product,rows=source_rows(self.run,'web'))
        self.assertFalse(form.is_valid())

    def test_archive_http_form_and_existing_report_page(self):
        self.client.force_login(self.owner)
        url=reverse('ai_assistant:automation_archive',args=['web',self.run.pk])
        self.assertEqual(self.client.get(url,secure=True).status_code,200)
        payload={key:value.pk for key,value in self.states.items()}
        payload.update(plan=self.plan.pk,build=self.build.pk,defects=['2'],confirm=True)
        response=self.client.post(url,payload,secure=True)
        self.assertEqual(response.status_code,302)
        saved=AutomationArchive.objects.get(source_id=self.run.pk)
        self.assertEqual(self.client.get(response.url,secure=True).status_code,200)
        self.assertEqual(self.client.get(reverse('ai_assistant:edit_defect_draft',args=[AIDefectDraft.objects.get(execution__run=saved.test_run).pk]),secure=True).status_code,200)
