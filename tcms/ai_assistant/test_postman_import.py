import json
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from tcms.tests.factories import UserFactory, ProductFactory
from .postman_import import parse_collection
from .crypto import decrypt_api_key
from .models import APICase, APIEnvironment, APIRun, PostmanImport, ProjectResourceAssignment
from .api_runner import submit_run
from .api_forms import APICaseForm
from .roles import ROLE_VIEWER


def collection(request=None, **extra):
    return dict(info={'name': '账户接口', 'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json'},
        item=[{'name':'用户管理', 'item':[{'name':'查询用户', 'request':request or {
            'method':'GET', 'url':'{{base_url}}/users/{{user_id}}?page=1',
            'header':[{'key':'Accept','value':'application/json'}]}}]}], **extra)


class PostmanParserTests(SimpleTestCase):
    def parse(self, data):
        return parse_collection(json.dumps(data).encode())

    def test_nested_folders_query_and_source_origin_removed(self):
        row = self.parse(collection())['rows'][0]
        self.assertEqual(row['folders'], ['账户接口','用户管理'])
        self.assertEqual(row['configuration']['query'], {'page':'1'})
        self.assertEqual(row['path'], '/users/{{user_id}}')
        self.assertFalse(row['error'])
        row = self.parse(collection({'method':'GET','url':'https://example.invalid/api/users?a=1'}))['rows'][0]
        self.assertEqual(row['path'], '/api/users')
        self.assertNotIn('example.invalid', json.dumps(row))

    def test_json_body_preserved(self):
        row = self.parse(collection({'method':'POST','url':'{{base}}/users','body':{'mode':'raw','raw':'{"name":"demo"}'}}))['rows'][0]
        self.assertEqual(row['configuration']['body'], {'name':'demo'})
        self.assertTrue(row['configuration']['send_body'])

    def test_scripts_are_reported_but_not_stored_or_executed(self):
        data = collection(event=[{'listen':'test','script':{'exec':['doNotExecuteSECRET()']}}], variable=[{'key':'token','value':'doNotStoreTOKEN'}])
        output = self.parse(data)
        self.assertNotIn('doNotExecuteSECRET', json.dumps(output))
        self.assertNotIn('doNotStoreTOKEN', json.dumps(output))
        self.assertTrue(any('JavaScript' in item for item in output['rows'][0]['warnings']))

    def test_unsupported_forms_and_duplicate_queries_are_not_importable(self):
        requests = [{'method':'POST','url':'/login','body':{'mode':'formdata','formdata':[]}},
                    {'method':'GET','url':'/users?a=1&a=2'},
                    {'method':'GET','url':'/{{$randomInt}}'},
                    {'method':'GET','url':'//evil.example/users'},
                    {'method':'OPTIONS','url':'/users'}]
        for request in requests:
            with self.subTest(request=request):
                row = self.parse(collection(request))['rows'][0]
                self.assertTrue(row['error'])
                self.assertIsNone(row['configuration'])

    def test_credentials_are_rejected_without_leaking_to_preview(self):
        for request in [{'method':'GET','url':'/me','header':[{'key':'Authorization','value':'Bearer SECRET_VALUE'}]},
                        {'method':'POST','url':'/login','body':{'mode':'raw','raw':'{"password":"SECRET_VALUE"}'}},
                        {'method':'GET','url':'/me?token=SECRET_VALUE'}]:
            row = self.parse(collection(request))['rows'][0]
            self.assertTrue(row['error'])
            self.assertNotIn('SECRET_VALUE', json.dumps(row))
        row = self.parse(collection({'method':'GET','url':'/me','header':[{'key':'Authorization','value':'Bearer {{token}}'}]}))['rows'][0]
        self.assertFalse(row['error'])

    def test_invalid_format_and_limits(self):
        for raw in [b'{}', b'[]', b'{"info":{},"info":{}}', b'NaN', b'x'*(2*1024*1024+1)]:
            with self.assertRaises(ValueError): parse_collection(raw)
        data = collection(); data['item'] *= 201
        with self.assertRaises(ValueError): self.parse(data)
        data = collection(); data['info']['schema']='https://evil.example/collection.json'
        with self.assertRaises(ValueError): self.parse(data)


@override_settings(API_AUTOMATION_ALLOWED_ORIGINS=['http://api-demo:8080'])
class PostmanImportTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_active=True, is_superuser=True)
        self.product = ProductFactory()
        self.client.force_login(self.owner)
        self.upload_url = reverse('ai_assistant:postman_upload', args=[self.product.pk])

    def upload(self, data=None):
        response = self.client.post(self.upload_url, {'collection':SimpleUploadedFile('collection.json', json.dumps(data or collection()).encode(), content_type='application/json')}, secure=True)
        self.assertEqual(response.status_code, 302)
        return PostmanImport.objects.get(owner=self.owner), response.url

    def test_preview_encrypted_private_and_no_business_writes(self):
        batch, url = self.upload()
        self.assertNotIn('查询用户', batch.payload_encrypted)
        self.assertIn('查询用户', decrypt_api_key(batch.payload_encrypted))
        self.assertFalse(APICase.objects.filter(owner=self.owner).exists())
        self.assertContains(self.client.get(url, secure=True), '待复核')
        self.client.force_login(UserFactory(is_active=True,is_superuser=True))
        self.assertEqual(self.client.get(url, secure=True).status_code, 404)

    def test_confirm_creates_directory_and_prevents_repeated_import(self):
        batch, url = self.upload()
        self.assertEqual(self.client.post(url, {'selected':['0']}, secure=True).status_code,200)
        self.assertFalse(APICase.objects.filter(owner=self.owner).exists())
        self.assertEqual(self.client.post(url, {'selected':['0'], 'confirmed':'on'}, secure=True).status_code,302)
        script = APICase.objects.get(owner=self.owner)
        assignment = ProjectResourceAssignment.objects.get(resource_type='case', object_id=script.test_case_id)
        self.assertEqual(assignment.folder.name,'用户管理')
        self.assertEqual(assignment.folder.parent.name,'账户接口')
        self.assertEqual(assignment.folder.product_id,self.product.pk)
        self.assertTrue(script.import_review_required)
        batch.refresh_from_db();self.assertEqual(batch.payload_encrypted,'')
        self.client.post(url, {'selected':['0'], 'confirmed':'on'}, secure=True)
        self.upload()
        self.assertEqual(APICase.objects.filter(owner=self.owner).count(),1)

    def test_expired_and_invalid_selection_rejected(self):
        batch, url = self.upload()
        self.client.post(url, {'selected':['99'], 'confirmed':'on'}, secure=True)
        self.assertFalse(APICase.objects.filter(owner=self.owner).exists())
        batch.expires=timezone.now()-timedelta(seconds=1);batch.save()
        self.assertEqual(self.client.post(url, {'selected':['0'],'confirmed':'on'}, secure=True).status_code,410)

    def test_readonly_cannot_upload_or_confirm(self):
        batch, url = self.upload()
        self.owner.groups.add(Group.objects.get_or_create(name=ROLE_VIEWER)[0])
        self.assertEqual(self.client.post(url, {'selected':['0'],'confirmed':'on'}, secure=True).status_code,403)
        self.assertEqual(self.client.get(self.upload_url, secure=True).status_code,403)
        self.assertFalse(APICase.objects.filter(owner=self.owner).exists())

    def test_unsupported_selected_request_cannot_create_case(self):
        batch,url=self.upload(collection({'method':'GET','url':'/me?token=literal-secret'}))
        self.assertNotIn('literal-secret',decrypt_api_key(batch.payload_encrypted))
        self.client.post(url,{'selected':['0'],'confirmed':'on'},secure=True)
        self.assertFalse(APICase.objects.filter(owner=self.owner).exists())

    def test_pending_review_blocks_submission_until_explicit_confirm(self):
        batch,url=self.upload(collection({'method':'GET','url':'/health'}))
        self.client.post(url,{'selected':['0'],'confirmed':'on'},secure=True)
        script=APICase.objects.get(owner=self.owner)
        env=APIEnvironment.objects.create(owner=self.owner,product=self.product,name='demo',base_url='http://api-demo:8080')
        with self.assertRaisesMessage(ValueError,'待复核'):
            submit_run(self.owner,self.product,{'environment':env,'cases':[script],'submission_token':uuid.uuid4()})
        self.assertFalse(APIRun.objects.filter(owner=self.owner).exists())
        data={key:getattr(script,key) for key in ('name','sequence','method','path','query','headers','send_body','body','expected_status','assertions','extracts','max_elapsed_ms')}
        data['test_case']=script.test_case_id
        form=APICaseForm(data,instance=script,owner=self.owner,product=self.product)
        self.assertFalse(form.is_valid());self.assertIn('confirmed',form.errors)
        data['confirmed']='on'
        form=APICaseForm(data,instance=script,owner=self.owner,product=self.product)
        self.assertTrue(form.is_valid(),form.errors);form.save()
        script.refresh_from_db();self.assertFalse(script.import_review_required)
        with patch('tcms.ai_assistant.api_runner.prepare_case', wraps=__import__('tcms.ai_assistant.api_runner',fromlist=['prepare_case']).prepare_case):
            run=submit_run(self.owner,self.product,{'environment':env,'cases':[script],'submission_token':uuid.uuid4()})
        self.assertEqual(run.status,'queued')

    def test_existing_script_is_unchanged_by_import(self):
        original=APICase.objects.create(owner=self.owner,product=self.product,name='查询用户',path='/old')
        batch,url=self.upload()
        self.client.post(url,{'selected':['0'],'confirmed':'on'},secure=True)
        original.refresh_from_db();self.assertEqual(original.path,'/old')
        self.assertFalse(original.import_review_required)

    def test_request_path_and_method_filters_match_directory_scope(self):
        batch,url=self.upload(collection({'method':'GET','url':'/health'}))
        self.client.post(url,{'selected':['0'],'confirmed':'on'},secure=True)
        script=APICase.objects.get(owner=self.owner)
        page=self.client.get(reverse('ai_assistant:api_home'),{'tab':'cases','product':self.product.pk,'q':'/health','method':'GET'},secure=True)
        self.assertEqual([item.pk for item in page.context['cases']], [script.pk])
        self.assertContains(page, '待复核')
        self.assertContains(page, '导入 Postman')
        for method in ('POST','INVALID'):
            page=self.client.get(reverse('ai_assistant:api_home'),{'tab':'cases','product':self.product.pk,'q':'/health','method':method},secure=True)
            self.assertEqual(list(page.context['cases']), [])
