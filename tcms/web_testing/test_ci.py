import json
import uuid
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from django.contrib.auth.models import Group
from django.test import TestCase, SimpleTestCase, Client
from django.urls import reverse
from django.utils import timezone

from tcms.tests.factories import UserFactory, ProductFactory, TestCaseFactory, TestPlanFactory, VersionFactory, BuildFactory
from tcms.ai_assistant.api_scheduling import rotate_token
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from tcms.ai_assistant.roles import ROLE_VIEWER
from tcms.testruns.models import TestRun, TestExecutionStatus
from deployment.kiwi_ci import main, write_reports, MappedHTTPSHandler
from .models import WebCase, WebEnvironment, WebSuite, WebRun, WebResult


class WebCITests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True, is_active=True)
        self.product = ProductFactory()
        self.case = TestCaseFactory(category__product=self.product)
        self.case.save()
        self.config = WebCase.objects.create(owner=self.owner, product=self.product, name='body', test_case=self.case,
            steps_encrypted=encrypt_api_key(json.dumps([{'action':'goto', 'value':'/'}, {'action':'assert_visible', 'selector':'body'}])))
        self.env = WebEnvironment.objects.create(owner=self.owner, product=self.product, name='QA', base_url='https://kiwi-web:8443')
        self.suite = WebSuite.objects.create(owner=self.owner, product=self.product, environment=self.env,
            name='smoke', base_url=self.env.base_url, case_ids=[self.config.pk])
        self.token = rotate_token(self.suite)
        self.url = reverse('web_testing:ci_submit', args=[self.suite.pk])
        self.client.force_login(self.owner)

    def submit(self, key=None, **data):
        return self.client.post(self.url, data=json.dumps(data), content_type='application/json',
            HTTP_AUTHORIZATION='Bearer '+self.token, HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()), secure=True)

    def test_bearer_only_and_bad_key_no_partial_creation(self):
        self.assertEqual(self.client.post(self.url, secure=True).status_code, 401)
        self.assertEqual(self.submit(key='bad').status_code, 400)
        self.assertEqual(self.client.get(self.url, secure=True).status_code, 405)
        self.assertFalse(WebRun.objects.exists())
        secure_client = Client(enforce_csrf_checks=True)
        response = secure_client.post(self.url, '{}', content_type='application/json',
            HTTP_AUTHORIZATION='Bearer '+self.token, HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()), secure=True)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response['Cache-Control'], 'max-age=0, no-cache, no-store, must-revalidate, private')

    def test_debug_submission_snapshot_idempotence_busy_and_scope(self):
        key = uuid.uuid4()
        first = self.submit(key)
        self.assertEqual(first.status_code, 202)
        run = WebRun.objects.get()
        snapshot = run.snapshot_encrypted
        self.assertEqual((run.trigger, run.execution_mode), ('ci', 'debug'))
        self.suite.case_ids = []
        self.suite.save()
        self.assertEqual(self.submit(key).json()['run_id'], str(run.pk))
        self.assertEqual(self.submit().status_code, 409)
        run.refresh_from_db(); self.assertEqual(run.snapshot_encrypted, snapshot)
        self.assertFalse(TestRun.objects.exists())
        other = WebSuite.objects.create(owner=self.owner, product=self.product, name='other', environment=self.env)
        response = self.client.post(reverse('web_testing:ci_submit', args=[other.pk]), '{}', content_type='application/json',
            HTTP_AUTHORIZATION='Bearer '+rotate_token(other), HTTP_IDEMPOTENCY_KEY=str(key), secure=True)
        self.assertEqual(response.status_code, 409)

    def test_report_polling_exact_total_and_cross_suite_isolation(self):
        run_id = self.submit().json()['run_id']
        run = WebRun.objects.get()
        url = reverse('web_testing:ci_run', args=[self.suite.pk, run_id])
        headers = dict(HTTP_AUTHORIZATION='Bearer '+self.token, secure=True)
        self.assertFalse(self.client.get(url, **headers).json()['terminal'])
        run.status='passed';run.save()
        self.assertFalse(self.client.get(url, **headers).json()['passed'])
        WebResult.objects.create(run=run, position=1, name='body', status='passed')
        self.assertTrue(self.client.get(url, **headers).json()['passed'])
        other = WebSuite.objects.create(owner=self.owner, product=self.product, name='other', environment=self.env)
        self.assertEqual(self.client.get(reverse('web_testing:ci_run', args=[other.pk, run.pk]),
            HTTP_AUTHORIZATION='Bearer '+rotate_token(other), secure=True).status_code, 404)
        run.trigger='manual';run.save()
        self.assertEqual(self.client.get(url, **headers).status_code, 404)

    def test_token_rotation_revocation_expiry_inactive_readonly(self):
        rotate_token(self.suite)
        self.assertEqual(self.submit().status_code, 401)
        self.token = rotate_token(self.suite)
        self.suite.ci_token_expires=timezone.now()-timedelta(seconds=1);self.suite.save()
        self.assertEqual(self.submit().status_code, 401)
        self.token = rotate_token(self.suite)
        self.owner.is_active=False;self.owner.save()
        self.assertEqual(self.submit().status_code, 401)
        self.owner.is_active=True;self.owner.save()
        self.owner.groups.add(Group.objects.get_or_create(name=ROLE_VIEWER)[0])
        self.assertEqual(self.submit().status_code, 403)
        self.assertFalse(WebRun.objects.exists())

    def test_invalid_payload_and_environment_no_partial_creation(self):
        for data in [dict(extra='bad'), dict(execution_mode='nonsense'), dict(environment=987654321), dict(execution_mode='formal')]:
            self.assertIn(self.submit(**data).status_code, (400, 409))
        other = UserFactory()
        env = WebEnvironment.objects.create(owner=other, product=self.product, name='private', base_url=self.env.base_url)
        self.assertEqual(self.submit(environment=env.pk).status_code, 409)
        self.assertFalse(WebRun.objects.exists());self.assertFalse(TestRun.objects.exists())

    def test_formal_reuses_plan_build_business_validation(self):
        version=VersionFactory(product=self.product)
        plan=TestPlanFactory(product=self.product, product_version=version)
        build=BuildFactory(version=version, is_active=True)
        TestExecutionStatus.objects.create(name='CI pending', weight=0)
        self.assertEqual(self.submit(execution_mode='formal',plan=plan.pk,build=build.pk).status_code, 409)
        self.assertFalse(TestRun.objects.exists())
        plan.add_case(self.case)
        response=self.submit(execution_mode='formal',plan=plan.pk,build=build.pk)
        self.assertEqual(response.status_code, 202)
        run=WebRun.objects.get()
        self.assertEqual((run.execution_mode,run.test_run.plan_id,run.test_run.build_id),('formal',plan.pk,build.pk))
        self.assertEqual(run.test_run.executions.get().case_id,self.case.pk)
        self.assertEqual(json.loads(decrypt_api_key(run.snapshot_encrypted))['execution_context']['plan_id'],plan.pk)

    def test_token_ui_shown_once_csrf_owner_and_readonly(self):
        url=reverse('web_testing:ci_settings',args=[self.suite.pk])
        action=reverse('web_testing:ci_token_action',args=[self.suite.pk,'rotate-token'])
        response=self.client.post(action, secure=True)
        token=response.context['new_token']
        self.assertContains(response,token)
        self.assertNotContains(self.client.get(url,secure=True),token)
        self.assertEqual(self.client.get(action,secure=True).status_code,405)
        secure_client=Client(enforce_csrf_checks=True);secure_client.force_login(self.owner)
        self.assertEqual(secure_client.post(action,secure=True).status_code,403)
        self.owner.groups.add(Group.objects.get_or_create(name=ROLE_VIEWER)[0])
        self.assertEqual(self.client.post(action,secure=True).status_code,403)
        self.client.force_login(UserFactory())
        self.assertEqual(self.client.get(url,secure=True).status_code,404)
        self.client.force_login(self.owner);self.owner.groups.clear()
        self.client.post(reverse('web_testing:ci_token_action',args=[self.suite.pk,'revoke-token']),secure=True)
        self.suite.refresh_from_db();self.assertFalse(self.suite.ci_token_hash)

    def test_stale_suite_edit_does_not_overwrite_rotated_token(self):
        from .forms import SuiteForm
        form=SuiteForm(dict(product=self.product.pk,name='edited',environment=self.env.pk,cases=[self.config.pk]),
                       instance=WebSuite.objects.get(pk=self.suite.pk),owner=self.owner)
        self.assertTrue(form.is_valid(),form.errors)
        new_token=rotate_token(self.suite)
        form.save()
        self.suite.refresh_from_db()
        self.token=new_token
        self.assertEqual(self.submit().status_code,202)


class JenkinsClientTests(SimpleTestCase):
    def test_web_client_endpoint_formal_payload_and_report(self):
        response=MagicMock();opener=MagicMock();opener.open.return_value=response
        response.__enter__.return_value.read.return_value=json.dumps(dict(run_id=str(uuid.uuid4()),terminal=True,
            passed=True,status='passed',results=[dict(name='body',status='passed',elapsed_ms=1)])).encode()
        with TemporaryDirectory() as directory, patch.dict('os.environ',dict(KIWI_BASE_URL='https://localhost:8443',
            KIWI_CI_TOKEN='do-not-log-this'),clear=True),patch('deployment.kiwi_ci.build_opener',return_value=opener),patch('builtins.print') as printed:
            output=str(Path(directory)/'web.json')
            self.assertEqual(main(['--type','web','--suite','2','--mode','formal','--plan','3','--build','4','--output',output]),0)
            req=opener.open.call_args.args[0]
            self.assertEqual(req.full_url,'https://localhost:8443/web-testing/ci/suites/2/runs/')
            self.assertEqual(json.loads(req.data),dict(execution_mode='formal',plan=3,build=4))
            self.assertNotIn('do-not-log-this',str(printed.call_args_list))
            self.assertEqual(ElementTree.parse(Path(output).with_suffix('.xml')).getroot().attrib['errors'],'0')

    def test_client_error_overwrites_stale_success_report(self):
        with TemporaryDirectory() as directory,patch.dict('os.environ',dict(KIWI_BASE_URL='http://invalid',KIWI_CI_TOKEN='secret'),clear=True),patch('builtins.print'):
            output=str(Path(directory)/'result.json')
            write_reports(dict(status='completed',results=[dict(name='old',status='passed')]),output)
            self.assertEqual(main(['--suite','1','--output',output]),2)
            self.assertEqual(ElementTree.parse(Path(output).with_suffix('.xml')).getroot().attrib['errors'],'1')

    def test_web_failure_xml_and_control_character_cleanup(self):
        with TemporaryDirectory() as directory:
            output=str(Path(directory)/'result.json')
            write_reports(dict(status='failed',results=[dict(name='bad\x00',status='failed',error='failed',steps=[])]),output,'web')
            tree=ElementTree.parse(Path(output).with_suffix('.xml')).getroot()
            self.assertEqual(tree.attrib['failures'],'1')
            self.assertEqual(tree.attrib['errors'],'0')

    def test_mapping_preserves_original_tls_host(self):
        import ssl
        handler=MappedHTTPSHandler(context=ssl.create_default_context(),connect_host='kiwi-web')
        with patch.object(handler,'do_open') as opened:
            handler.https_open(object())
            conn=opened.call_args.args[0]('localhost:8443')
            self.assertEqual(conn.host,'localhost')
            with patch('deployment.kiwi_ci.socket.create_connection') as connect:
                conn._create_connection(('localhost',8443),10)
                self.assertEqual(connect.call_args.args[0],('kiwi-web',8443))
