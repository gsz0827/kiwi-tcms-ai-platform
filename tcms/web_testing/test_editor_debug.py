import json
import sys
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import Group
from django.test import TestCase, SimpleTestCase, override_settings
from django.urls import reverse

from tcms.tests.factories import UserFactory, ProductFactory, TestRunFactory
from tcms.testruns.models import TestRun
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from .models import WebCase, WebEnvironment, WebSuite, WebRun, WebResult, WebAIRequest, WebAIDraft
from .validation import validate_steps, editor_schema
from .execution_config import expand_steps
from .runner import perform


class AssertionTests(SimpleTestCase):
    def test_all_assertions_and_empty_input_values(self):
        for action in ['assert_hidden','assert_enabled','assert_text_exact','assert_value','assert_count']:
            value = '0' if action == 'assert_count' else 'Expected'
            steps = [{'action':action,'selector':'#target','value':value}]
            self.assertEqual(validate_steps(steps), steps)
        self.assertEqual(validate_steps([{'action':'assert_value','selector':'input','value':''}])[0]['value'], '')

    def test_count_integer_bounds_and_variables(self):
        for value in ['0','10000','{{count}}','{{ count }}']:
            validate_steps([{'action':'assert_count','selector':'.row','value':value}])
        for value in ['-1','1.2','10001','one','', '9'*2000]:
            with self.assertRaises(ValueError):
                validate_steps([{'action':'assert_count','selector':'.row','value':value}])
        self.assertEqual(expand_steps([{'action':'assert_count','selector':'.row','value':'{{count}}'}], {'count':2})[0]['value'], '2')
        with self.assertRaises(ValueError):
            expand_steps([{'action':'assert_count','selector':'.row','value':'{{count}}'}], {'count':-1})

    def test_locator_required_and_indexed_errors(self):
        with self.assertRaisesMessage(ValueError, '第 2 步'):
            validate_steps([{'action':'goto','value':'/'},{'action':'assert_hidden','selector':' '}])
        for steps in [[{'action':[]}], [{'action':'assert_unknown'}], [{'action':'assert_value','selector':'input','value':42}]]:
            with self.assertRaises(ValueError): validate_steps(steps)

    def test_schema_and_runner_match(self):
        schema={item['action']:item for item in editor_schema()}
        self.assertFalse(schema['goto']['locator'])
        self.assertTrue(schema['assert_count']['required'])
        self.assertFalse(schema['assert_value']['required'])
        fake=ModuleType('playwright.sync_api');fake.expect=Mock()
        parent=ModuleType('playwright');parent.sync_api=fake
        page=Mock();logs=[]
        steps=[{'action':action,'selector':'input','value':'2' if action=='assert_count' else 'value'}
               for action in ['assert_hidden','assert_enabled','assert_text_exact','assert_value','assert_count']]
        with patch.dict(sys.modules,{'playwright':parent,'playwright.sync_api':fake}), patch('tcms.web_testing.runner.time.monotonic',return_value=0):
            perform(page,steps,{},20,lambda:None,logs)
        self.assertEqual([entry['status'] for entry in logs],['passed']*5)
        self.assertNotIn('value', json.dumps(logs))
        assertion=fake.expect.return_value
        assertion.to_be_hidden.assert_called_once_with(timeout=10000)
        assertion.to_be_enabled.assert_called_once_with(timeout=10000)
        assertion.to_have_text.assert_called_once_with('value',timeout=10000)
        assertion.to_have_value.assert_called_once_with('value',timeout=10000)
        assertion.to_have_count.assert_called_once_with(2,timeout=10000)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class EditorDebugTests(TestCase):
    def setUp(self):
        self.owner=UserFactory(is_active=True,is_superuser=True)
        self.product=ProductFactory()
        self.steps=[{'action':'goto','value':'/accounts/login/'},{'action':'assert_visible','selector':'body'}]
        self.config=WebCase.objects.create(owner=self.owner,product=self.product,name='Login',
            steps_encrypted=encrypt_api_key(json.dumps(self.steps)))
        self.env=WebEnvironment.objects.create(owner=self.owner,product=self.product,name='QA',
            base_url='https://kiwi-web:8443',ignore_https_errors=True,variables_encrypted=encrypt_api_key('{}'))
        self.client.force_login(self.owner)

    def debug(self, **changes):
        data=dict(token=str(uuid.uuid4()),environment=self.env.pk,confirm=True);data.update(changes)
        return self.client.post(reverse('web_testing:case_debug',args=[self.config.pk]),data)

    def source(self):
        rows=[dict(id=self.config.pk,name=f'Case {i}',steps=self.steps,dataset=i//2,
                   setup_steps=self.steps, business_case_id=None) for i in range(3)]
        snapshot=dict(base_url=self.env.base_url,ignore_https_errors=True,stop_on_failure=True,cases=rows)
        run=WebRun.objects.create(owner=self.owner,product=self.product,suite=None,name='Source',status='failed',
            submission_token=uuid.uuid4(),total=3,completed_count=2,environment=self.env,
            snapshot_encrypted=encrypt_api_key(json.dumps(snapshot)))
        WebResult.objects.create(run=run,position=1,name='Pass',status='passed')
        WebResult.objects.create(run=run,position=2,name='Failure',status='failed')
        return run

    def retry(self,source,**changes):
        data=dict(token=str(uuid.uuid4()),scope='failed');data.update(changes)
        return self.client.post(reverse('web_testing:retry',args=[source.pk]),data)

    def test_debug_get_is_private_and_non_mutating(self):
        response=self.client.get(reverse('web_testing:case_debug',args=[self.config.pk]))
        self.assertContains(response,'确认在测试环境执行')
        self.assertIn('no-store',response['Cache-Control'])
        self.assertFalse(WebRun.objects.exists())
        self.assertFalse(WebSuite.objects.exists())

    def test_single_config_debug_freezes_environment_and_setup(self):
        login=WebCase.objects.create(owner=self.owner,product=self.product,name='Setup',steps_encrypted=self.config.steps_encrypted)
        self.env.setup_case=login;self.env.save()
        self.assertEqual(self.debug().status_code,302)
        run=WebRun.objects.get()
        self.assertIsNone(run.test_run_id)
        self.assertIsNone(run.suite_id)
        self.assertEqual((run.total,run.execution_mode),(1,'debug'))
        snap=json.loads(decrypt_api_key(run.snapshot_encrypted))
        self.assertEqual(snap['cases'][0]['setup_steps'],self.steps)
        self.assertEqual(snap['debug_case_id'],self.config.pk)
        self.env.name='Changed';self.env.save()
        self.config.steps_encrypted=encrypt_api_key('[]');self.config.save()
        self.assertEqual(snap['execution_context']['environment_name'],'QA')
        self.assertEqual(json.loads(decrypt_api_key(run.snapshot_encrypted)),snap)
        self.assertFalse(WebSuite.objects.exists())
        self.assertFalse(TestRun.objects.exists())

    def test_single_debug_duplicate_and_changed_environment_token(self):
        token=str(uuid.uuid4())
        self.assertEqual(self.debug(token=token).status_code,302)
        self.assertEqual(self.debug(token=token).status_code,302)
        other=WebEnvironment.objects.create(owner=self.owner,product=self.product,name='Other',base_url=self.env.base_url)
        self.assertEqual(self.debug(token=token,environment=other.pk).status_code,403)
        self.assertEqual(WebRun.objects.count(),1)

    def test_invalid_debug_input_and_missing_variable_do_not_queue(self):
        for change in [dict(token='invalid'),dict(confirm=False),dict(environment='')]:
            self.assertEqual(self.debug(**change).status_code,200)
        self.config.steps_encrypted=encrypt_api_key(json.dumps([{'action':'goto','value':'/'},{'action':'assert_text','selector':'body','value':'{{missing}}'}]));self.config.save()
        self.assertContains(self.debug(),'缺少测试变量')
        self.assertFalse(WebRun.objects.exists())

    def test_debug_environment_owner_and_product_isolation(self):
        private=WebEnvironment.objects.create(owner=UserFactory(),product=self.product,name='private-env',base_url=self.env.base_url)
        wrong=WebEnvironment.objects.create(owner=self.owner,product=ProductFactory(),name='wrong-product',base_url=self.env.base_url)
        for env in [private,wrong]:
            response=self.debug(environment=env.pk)
            self.assertEqual(response.status_code,200)
            self.assertNotContains(response,env.name)
        self.assertFalse(WebRun.objects.exists())

    def test_foreign_config_and_readonly_debug_denied(self):
        self.client.force_login(UserFactory())
        self.assertEqual(self.client.get(reverse('web_testing:case_debug',args=[self.config.pk])).status_code,404)
        self.client.force_login(self.owner)
        self.owner.groups.add(Group.objects.get_or_create(name='AI 只读')[0])
        self.assertEqual(self.debug().status_code,403)
        self.assertEqual(self.client.get(reverse('web_testing:case_debug',args=[self.config.pk])).status_code,403)

    def test_failed_retry_preserves_source_and_excludes_passed_unexecuted(self):
        source=self.source();before=source.snapshot_encrypted
        self.assertEqual(self.retry(source).status_code,302)
        child=WebRun.objects.exclude(pk=source.pk).get()
        snapshot=json.loads(decrypt_api_key(child.snapshot_encrypted))
        self.assertEqual(child.total,1)
        self.assertEqual(child.source_id,source.pk)
        self.assertEqual(child.execution_mode,'debug')
        self.assertIsNone(child.test_run_id)
        self.assertEqual(snapshot['cases'][0]['origin_position'],2)
        self.assertEqual(snapshot['cases'][0]['setup_steps'],self.steps)
        self.assertFalse(snapshot['stop_on_failure'])
        self.assertEqual(snapshot['retry_context']['source_positions'],[2])
        source.refresh_from_db()
        self.assertEqual(source.snapshot_encrypted,before)
        self.assertEqual(source.results.count(),2)
        self.assertEqual(source.status,'failed')

    def test_retry_all_is_exact_snapshot_and_never_copies_native_target(self):
        source=self.source();source.test_run=TestRunFactory();source.save()
        self.assertEqual(self.retry(source,scope='all').status_code,302)
        child=WebRun.objects.exclude(pk=source.pk).get()
        self.assertEqual(child.snapshot_encrypted,source.snapshot_encrypted)
        self.assertEqual(child.total,3)
        self.assertIsNone(child.test_run_id)
        self.assertEqual(TestRun.objects.count(),1)

    def test_failed_retry_repeated_is_idempotent_and_scope_change_denied(self):
        source=self.source();token=str(uuid.uuid4())
        self.assertEqual(self.retry(source,token=token).status_code,302)
        self.assertEqual(self.retry(source,token=token).status_code,302)
        self.assertEqual(self.retry(source,token=token,scope='all').status_code,403)
        self.assertEqual(WebRun.objects.count(),2)

    def test_retry_other_source_token_denied(self):
        source=self.source();other=self.source();token=str(uuid.uuid4())
        self.retry(source,token=token)
        self.assertEqual(self.retry(other,token=token).status_code,403)

    def test_retry_without_failed_results_and_running_source(self):
        source=self.source();source.results.filter(status='failed').delete()
        self.assertEqual(self.retry(source).status_code,409)
        source.status='running';source.save()
        self.assertEqual(self.retry(source,scope='all').status_code,409)
        self.assertEqual(WebRun.objects.count(),1)

    def test_retry_bad_token_scope_readonly_foreign_owner_and_get(self):
        source=self.source();url=reverse('web_testing:retry',args=[source.pk])
        self.assertEqual(self.client.get(url).status_code,405)
        self.assertEqual(self.retry(source,token='bad').status_code,400)
        self.assertEqual(self.retry(source,scope='invalid').status_code,400)
        self.client.force_login(UserFactory())
        self.assertEqual(self.retry(source).status_code,404)
        self.client.force_login(self.owner)
        self.owner.groups.add(Group.objects.get_or_create(name='AI 只读')[0])
        self.assertEqual(self.retry(source).status_code,403)

    def test_debug_and_retry_respect_per_account_queue_limit(self):
        source=self.source()
        for index in range(20):
            WebRun.objects.create(owner=self.owner,product=self.product,suite=None,name=str(index),
                submission_token=uuid.uuid4(),snapshot_encrypted=source.snapshot_encrypted,total=3)
        self.assertContains(self.debug(),'待执行任务过多')
        self.assertEqual(self.retry(source).status_code,409)
        self.assertEqual(WebRun.objects.count(),21)

    def test_retry_keeps_dataset_and_public_setup_per_original_row(self):
        source=self.source()
        WebResult.objects.create(run=source,position=3,name='Failure dataset 2',status='failed')
        self.retry(source)
        child=WebRun.objects.exclude(pk=source.pk).get()
        rows=json.loads(decrypt_api_key(child.snapshot_encrypted))['cases']
        self.assertEqual([row['dataset'] for row in rows],[0,1])
        self.assertEqual([row['origin_position'] for row in rows],[2,3])
        self.assertTrue(all(row['setup_steps']==self.steps for row in rows))

    def test_shared_editor_case_and_ai_draft_pages_and_review_save(self):
        response=self.client.get(reverse('web_testing:case_edit',args=[self.config.pk]))
        self.assertContains(response,'web-step-schema')
        self.assertContains(response,'表格编辑')
        self.assertContains(response,'调试已保存脚本')
        evidence='登录页面显示用户名输入框'
        batch=WebAIRequest.objects.create(owner=self.owner,product=self.product,title='Login',
            submission_token=uuid.uuid4(),fingerprint='qa',generated=True,
            input_encrypted=encrypt_api_key(json.dumps(dict(documentation=evidence,requirements='',environment_variables=[],count=1))))
        draft=WebAIDraft.objects.create(request=batch,position=1,name='AI draft',evidence=evidence,steps=self.steps)
        url=reverse('web_testing:ai_review',args=[draft.pk])
        self.assertContains(self.client.get(url),'web-step-schema')
        self.assertContains(self.client.get(url),'表格编辑')
        self.assertEqual(self.client.post(url,dict(name='AI reviewed',description='',evidence=evidence,review_notes='',
            steps=json.dumps([{'action':'goto','value':'/accounts/login/'},{'action':'assert_count','selector':'input[name=username]','value':'1'}]),revision=1,confirmed=True)).status_code,302)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.reviewed_at)
        self.assertEqual(draft.steps[1]['action'],'assert_count')
        self.assertFalse(WebRun.objects.exists())

    def test_failed_action_visible_only_for_terminal_failures_and_writers(self):
        source=self.source();url=reverse('web_testing:run',args=[source.pk])
        self.assertContains(self.client.get(url),'失败项重跑（调试）')
        source.status='running';source.save()
        self.assertNotContains(self.client.get(url),'失败项重跑（调试）')
        source.status='failed';source.save()
        self.owner.groups.add(Group.objects.get_or_create(name='AI 只读')[0])
        self.assertNotContains(self.client.get(url),'失败项重跑（调试）')
