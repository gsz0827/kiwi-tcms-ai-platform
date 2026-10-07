"""Shared automation UI: private lists, safe navigation, form and timezone regressions."""
import json
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tcms.tests.factories import ProductFactory
from tcms.kiwi_auth.middleware import UserDisplayPreferenceMiddleware
from tcms.kiwi_auth.models import UserPreference
from tcms.web_testing.models import WebCase, WebEnvironment, WebSuite, WebRun
from .automation_ui import return_url
from .api_forms import APICaseForm, SuiteForm
from .api_scheduling import queue_suite, dispatch_due_suites, rotate_token
from .crypto import encrypt_api_key
from .models import APICase, APIEnvironment, APISuite, APIRun, APIResult
from .roles import ROLE_VIEWER
from .templatetags.automation_ui import automation_sections


@override_settings(USE_TZ=True, ALLURE_ENABLED=False, API_AUTOMATION_ALLOWED_ORIGINS=['http://127.0.0.1:8123'])
class AutomationUITests(TestCase):
    def setUp(self):
        users=get_user_model()
        self.owner=users.objects.create_user(username='automation-ui-owner',is_superuser=True)
        self.other=users.objects.create_user(username='automation-ui-other',is_superuser=True)
        self.product=ProductFactory()
        self.env=APIEnvironment.objects.create(owner=self.owner,product=self.product,name='QA environment',base_url='http://127.0.0.1:8123')
        self.case=APICase.objects.create(owner=self.owner,product=self.product,name='QA request',path='/health',assertions=[{'path':'data','operator':'equals','expected':None}])
        self.suite=APISuite.objects.create(owner=self.owner,product=self.product,name='QA suite',environment=self.env,case_ids=[self.case.pk])
        self.web=WebCase.objects.create(owner=self.owner,product=self.product,name='QA browser',steps_encrypted=encrypt_api_key('[{"action":"goto","value":"/"}]'))
        self.web_env=WebEnvironment.objects.create(owner=self.owner,product=self.product,name='QA Web environment',base_url='https://kiwi-web:8443')
        self.web_suite=WebSuite.objects.create(owner=self.owner,product=self.product,name='QA Web suite',case_ids=[self.web.pk],environment=self.web_env,base_url=self.web_env.base_url)
        self.client.force_login(self.owner)

    def get(self,name,args=None,**params):
        return self.client.get(reverse(name,args=args),{'product':self.product.pk}|params,secure=True)

    def readonly(self):
        self.owner.groups.add(Group.objects.get_or_create(name=ROLE_VIEWER)[0])

    def api_run(self,**kwargs):
        return APIRun.objects.create(owner=self.owner,product=self.product,environment_name='QA environment',submission_token=uuid.uuid4(),status='completed',snapshot_encrypted=encrypt_api_key('{}'),**kwargs)

    def test_all_eight_lists_share_filter_and_table(self):
        for tab in ('cases','suites','runs','environments'):
            api=self.get('ai_assistant:api_home',tab=tab)
            web=self.get('web_testing:'+tab)
            for page in (api,web):
                self.assertEqual(page.status_code,200)
                self.assertContains(page,'class="automation-filter"')
                self.assertContains(page,'table automation-table')
                self.assertContains(page,'automation-pagination')
                self.assertNotContains(page,'Showing 0 Results')

    def test_configuration_headers_have_one_create_action(self):
        for route,params in [('web_testing:cases',{}),('ai_assistant:api_home',{'tab':'cases'})]:
            page=self.get(route,**params)
            self.assertContains(page,'新建脚本',count=1)
            self.assertContains(page,'<h1>自动化脚本</h1>',html=True)

    def test_private_lists_exclude_other_account_even_superuser(self):
        APIEnvironment.objects.create(owner=self.other,product=self.product,name='PRIVATE-ENV',base_url=self.env.base_url)
        APICase.objects.create(owner=self.other,product=self.product,name='PRIVATE-CONFIG',path='/secret')
        APISuite.objects.create(owner=self.other,product=self.product,name='PRIVATE-SUITE',environment=self.env)
        for tab in ('cases','suites','environments'):
            page=self.get('ai_assistant:api_home',tab=tab)
            self.assertNotContains(page,'PRIVATE-')

    def test_number_search_matches_configuration_and_directory(self):
        for route,params,needle in [('web_testing:cases',{},f'WEB-{self.web.pk}'),('ai_assistant:api_home',{'tab':'cases'},f'API-{self.case.pk}')]:
            page=self.get(route,q=needle,**params)
            self.assertEqual(page.context['cases'].paginator.count,1)
            self.assertContains(page,needle)

    def test_pagination_preserves_filters(self):
        APISuite.objects.bulk_create([APISuite(owner=self.owner,product=self.product,name=f'Paged {i}',environment=self.env) for i in range(35)])
        page=self.get('ai_assistant:api_home',tab='suites',q='Paged',page='2')
        self.assertEqual(len(page.context['suites']),5)
        self.assertEqual(page.context['suites'].paginator.count,35)
        self.assertContains(page,'q=Paged')
        self.assertContains(page,'page=1')

    def test_run_status_and_uuid_filters(self):
        run=self.api_run()
        APIResult.objects.create(run=run,position=1,name='Done',status='passed')
        APIResult.objects.create(run=run,position=2,name='Pending',status='pending')
        page=self.get('ai_assistant:api_home',tab='runs',q=str(run.pk),status='completed')
        self.assertEqual(page.context['runs'].paginator.count,1)
        row=page.context['runs'][0]
        self.assertEqual((row.result_finished,row.result_total),(1,2))
        self.assertEqual(self.get('ai_assistant:api_home',tab='runs',status='not-a-status').context['runs'].paginator.count,0)

    def test_invalid_dates_return_warning_not_server_error(self):
        page=self.get('ai_assistant:api_home',tab='runs',after='2026-02-31')
        self.assertEqual(page.context['runs'].paginator.count,0)
        self.assertContains(page,'日期格式不正确')

    def test_local_day_filter_uses_account_timezone(self):
        UserPreference.objects.create(user=self.owner,time_zone='Asia/Shanghai')
        inside=self.api_run();outside=self.api_run()
        APIRun.objects.filter(pk=inside.pk).update(created=datetime(2026,9,30,16,1,tzinfo=dt_timezone.utc))
        APIRun.objects.filter(pk=outside.pk).update(created=datetime(2026,9,30,15,59,tzinfo=dt_timezone.utc))
        page=self.get('ai_assistant:api_home',tab='runs',after='2026-10-01',before='2026-10-01')
        self.assertEqual([row.pk for row in page.context['runs']],[inside.pk])
        self.assertContains(page,'2026-10-01 00:01:00')

    def test_return_url_preserves_exact_safe_list(self):
        url=reverse('ai_assistant:api_home')+'?tab=runs&status=completed&page=2'
        request=RequestFactory().get('/',{'return_to':url})
        self.assertEqual(return_url(request,'/fallback'),url)

    def test_return_url_rejects_external_and_wrong_routes(self):
        for url in ('https://evil.example/','//evil.example/','/accounts/logout/','/api\\evil','/api\r\nLocation:evil','http://[broken'):
            request=RequestFactory().post('/',{'return_to':url})
            self.assertEqual(return_url(request,'/fallback'),'/fallback')

    def test_api_detail_returns_to_correct_tab(self):
        run=self.api_run()
        page=self.get('ai_assistant:api_report',[run.pk])
        self.assertIn('tab=runs',page.context['back_url'])
        page=self.get('ai_assistant:api_suite',[self.suite.pk])
        self.assertIn('tab=suites',page.context['back_url'])

    def test_web_editor_keeps_filtered_return_url_after_save(self):
        back=reverse('web_testing:cases')+'?product='+str(self.product.pk)+'&q=QA'
        response=self.client.post(reverse('web_testing:case_edit',args=[self.web.pk]),{'product':self.product.pk,'name':'Renamed','description':'','steps':'[{"action":"goto","value":"/"},{"action":"assert_visible","selector":"body"}]','return_to':back},secure=True)
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.url,back)
        self.web.refresh_from_db();self.assertEqual(self.web.name,'Renamed')

    def test_readonly_lists_and_suite_hide_mutating_buttons(self):
        self.readonly()
        for route,args,params in [('ai_assistant:api_home',None,{'tab':'cases'}),('ai_assistant:api_home',None,{'tab':'suites'}),('web_testing:cases',None,{}),('web_testing:suites',None,{}),('ai_assistant:api_suite',[self.suite.pk],{})]:
            page=self.get(route,args,**params)
            for label in ('新建脚本','新建套件','立即执行','生成 CI 令牌','轮换 CI 令牌'):
                self.assertNotContains(page,label)

    def test_readonly_post_denied_before_mutation(self):
        self.readonly()
        targets=[('ai_assistant:api_case_edit',[self.product.pk,self.case.pk]),('ai_assistant:api_environment_edit',[self.product.pk,self.env.pk]),('ai_assistant:api_submit',[self.product.pk]),('ai_assistant:api_suite_edit',[self.product.pk,self.suite.pk]),('ai_assistant:api_suite_action',[self.suite.pk,'rotate-token']),('web_testing:case_edit',[self.web.pk])]
        for route,args in targets:
            self.assertEqual(self.client.post(reverse(route,args=args),{},secure=True).status_code,403)
        self.suite.refresh_from_db();self.assertFalse(self.suite.ci_token_hash)
        self.assertEqual(APIRun.objects.count(),0)

    def test_readonly_forms_are_disabled_but_have_return(self):
        self.readonly()
        for route,args in [('ai_assistant:api_case_edit',[self.product.pk,self.case.pk]),('web_testing:case_edit',[self.web.pk])]:
            page=self.get(route,args)
            self.assertContains(page,'<fieldset disabled>')
            self.assertNotContains(page,'type="submit">保存')
            self.assertContains(page,'返回列表')

    def test_api_form_all_fields_present_in_sections(self):
        form=APICaseForm(instance=self.case,owner=self.owner,product=self.product)
        sections=automation_sections(form)
        self.assertEqual([field.name for group in sections for field in group['fields']].__len__(),len(form.visible_fields()))
        self.assertEqual({field.name for group in sections for field in group['fields']},{field.name for field in form.visible_fields()})
        page=self.get('ai_assistant:api_case_edit',[self.product.pk,self.case.pk])
        for field in form.visible_fields():self.assertContains(page,f'name="{field.name}"')

    def test_no_model_prompt_and_generate_disabled(self):
        for route in ('ai_assistant:api_ai_generate','web_testing:ai_generate'):
            page=self.get(route,[self.product.pk])
            self.assertContains(page,'当前账号尚未配置 AI 模型')
            self.assertContains(page,'disabled>生成测试用例')

    def test_api_saved_secrets_never_echo_and_no_store(self):
        self.env.secret_headers_encrypted=encrypt_api_key('{"Authorization":"PRIVATE-TOKEN"}')
        self.env.save()
        page=self.get('ai_assistant:api_environment_edit',[self.product.pk,self.env.pk])
        self.assertNotContains(page,'PRIVATE-TOKEN')
        self.assertIn('no-store',page['Cache-Control'])

    def test_schedule_time_is_user_local_and_saved_exactly(self):
        UserPreference.objects.create(user=self.owner,time_zone='Asia/Shanghai')
        future=(timezone.now()+timedelta(days=3)).astimezone(ZoneInfo('Asia/Shanghai')).replace(second=0,microsecond=0)
        response=self.client.post(reverse('ai_assistant:api_suite_edit',args=[self.product.pk,self.suite.pk]),{'name':self.suite.name,'environment':self.env.pk,'cases':[self.case.pk],'datasets':'[]','interval_minutes':60,'schedule_enabled':'on','next_run_at':future.strftime('%Y-%m-%dT%H:%M')},secure=True)
        self.assertEqual(response.status_code,302)
        self.suite.refresh_from_db();self.assertEqual(self.suite.next_run_at,future)

    def test_past_schedule_rejected_without_saving(self):
        form=SuiteForm({'name':'Past','environment':self.env.pk,'cases':[self.case.pk],'datasets':'[]','interval_minutes':60,'schedule_enabled':'on','next_run_at':'2000-01-01T12:00'},instance=self.suite,owner=self.owner,product=self.product)
        self.assertFalse(form.is_valid());self.assertIn('next_run_at',form.errors)

    def test_readonly_suite_queue_and_schedule_are_blocked(self):
        self.readonly()
        with self.assertRaises(ValueError):queue_suite(self.suite.pk,self.owner.pk)
        self.suite.schedule_enabled=True;self.suite.next_run_at=timezone.now()-timedelta(minutes=1);self.suite.save()
        self.assertEqual(dispatch_due_suites(),0)
        self.suite.refresh_from_db();self.assertFalse(self.suite.schedule_enabled)
        self.assertEqual(APIRun.objects.count(),0)

    def test_readonly_ci_cannot_enqueue_even_with_valid_token(self):
        token=rotate_token(self.suite)
        self.readonly()
        response=self.client.post(reverse('ai_assistant:api_ci_submit',args=[self.suite.pk]),{},HTTP_AUTHORIZATION='Bearer '+token,HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),secure=True)
        self.assertEqual(response.status_code,409)
        self.assertEqual(APIRun.objects.count(),0)

    def test_timezone_middleware_restores_context_on_success_and_exception(self):
        pref=UserPreference.objects.create(user=self.owner,time_zone='Asia/Shanghai')
        factory=RequestFactory();request=factory.get('/');request.user=self.owner
        seen=[]
        def response(_):seen.append(timezone.get_current_timezone_name());return HttpResponse('ok')
        with timezone.override('Etc/UTC'):
            previous_zone=timezone.get_current_timezone()
            middleware=UserDisplayPreferenceMiddleware(response)
            middleware(request);self.assertEqual(seen[-1],'Asia/Shanghai')
            self.assertEqual(timezone.get_current_timezone(),previous_zone)
            pref.time_zone='Etc/UTC';pref.save();middleware(request)
            self.assertIn(seen[-1],('Etc/UTC','UTC'))
            request.user=AnonymousUser();middleware(request)
            self.assertIn(seen[-1],('Etc/UTC','UTC'))
            request.user=self.owner
            def fail(_):raise RuntimeError('QA')
            with self.assertRaises(RuntimeError):UserDisplayPreferenceMiddleware(fail)(request)
            self.assertEqual(timezone.get_current_timezone(),previous_zone)
