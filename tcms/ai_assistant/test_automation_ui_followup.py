"""Presentation-only regressions for AI draft review and automation preflight."""
import copy
import json
import uuid
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from tcms.tests.factories import ProductFactory, CategoryFactory
from tcms.web_testing.models import WebAIRequest, WebAIDraft, WebCase, WebEnvironment
from .crypto import encrypt_api_key
from .models import APIAIRequest, APIAIDraft, APIEnvironment, APICase, APIRun
from .roles import ROLE_VIEWER
from .templatetags.automation_ui import automation_draft_preview

EVIDENCE = '健康检查接口 GET /health 返回 200，页面 /login/ 显示 #login-form。'
SOURCE = {'documentation': EVIDENCE, 'requirements': '', 'environment_variables': [], 'rules': {}}
CONFIG = {'method': 'GET', 'path': '/health', 'expected_status': 200, 'sequence': 1,
          'query': {'zero': 0, 'enabled': False}, 'headers': {},
          'assertions': [{'path': 'data', 'operator': 'equals', 'expected': None}]}
STEPS = [{'action': 'goto', 'value': '/login/'}, {'action': 'assert_visible', 'selector': '#login-form'}]


@override_settings(USE_TZ=True, ALLURE_ENABLED=False)
class AutomationFollowupTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='followup-owner', is_superuser=True)
        self.other = get_user_model().objects.create_user(username='followup-other', is_superuser=True)
        self.product = ProductFactory()
        self.category = CategoryFactory(product=self.product)
        common = dict(owner=self.owner, product=self.product, title='AI 复核验收',
                      submission_token=uuid.uuid4(), fingerprint='0' * 64,
                      input_encrypted=encrypt_api_key(json.dumps(SOURCE)), generated=True)
        self.web_batch = WebAIRequest.objects.create(**common)
        self.api_batch = APIAIRequest.objects.create(category=self.category, **common)
        self.web_draft = WebAIDraft.objects.create(request=self.web_batch, position=0, name='Web 草稿',
                                                  evidence=EVIDENCE, steps=STEPS)
        self.api_draft = APIAIDraft.objects.create(request=self.api_batch, position=0, name='接口草稿',
                                                  evidence=EVIDENCE, configuration=CONFIG)
        self.client.force_login(self.owner)

    def pages(self):
        return [('web_testing:ai_detail', self.web_batch, self.web_draft),
                ('ai_assistant:api_ai_detail', self.api_batch, self.api_draft)]

    def page(self, route, batch):
        return self.client.get(reverse(route, args=[batch.pk]), secure=True)

    def test_draft_workspaces_share_actions_and_have_one_header(self):
        for route, batch, _ in self.pages():
            page = self.page(route, batch)
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, '<h1>AI 复核验收</h1>', count=1, html=True)
            for text in ('返回脚本列表', '重新生成', '编辑与复核', '全选可保存草稿', '查看脚本 JSON'):
                self.assertContains(page, text)
            self.assertContains(page, 'id="automation-draft-save"')
            self.assertContains(page, 'type="submit" disabled')

    def test_web_preview_is_table_not_only_raw_json(self):
        page = self.page('web_testing:ai_detail', self.web_batch)
        self.assertContains(page, '操作与断言')
        self.assertContains(page, '<td>打开页面</td>', html=True)
        self.assertContains(page, '<td>断言元素可见</td>', html=True)
        self.assertContains(page, 'automation-draft-table')

    def test_api_preview_preserves_false_zero_and_null(self):
        page = self.page('ai_assistant:api_ai_detail', self.api_batch)
        for text in ('请求配置', '响应断言', '预期状态码', 'null', 'false'):
            self.assertContains(page, text)
        preview = automation_draft_preview(self.api_draft, 'api')
        self.assertEqual(preview['assertions'][0]['expected'], 'null')
        self.assertEqual(self.api_draft.configuration, CONFIG)

    def test_preview_handles_malformed_legacy_shapes_without_mutating(self):
        for kind, draft in [('web', SimpleNamespace(steps=[None, {'action': ['bad'], 'value': 0}])),
                            ('api', SimpleNamespace(configuration={'assertions': [None, {'operator': ['bad']}]})),
                            ('api', SimpleNamespace(configuration=None))]:
            previous = copy.deepcopy(draft.__dict__)
            result = automation_draft_preview(draft, kind)
            self.assertIn('raw', result)
            self.assertEqual(draft.__dict__, previous)

    def test_preview_escapes_html_not_rendering_ai_as_code(self):
        draft = SimpleNamespace(steps=[{'action': 'fill', 'selector': '<script>bad()</script>', 'value': '<img src=x>'}])
        html = render_to_string('ai_assistant/automation/draft_preview.html', automation_draft_preview(draft, 'web'))
        self.assertNotIn('<script>bad()', html)
        self.assertIn('&lt;script&gt;', html)

    def test_unreviewed_and_invalid_drafts_cannot_be_selected(self):
        for route, batch, draft in self.pages():
            page = self.page(route, batch)
            self.assertContains(page, f'name="draft_ids" value="{draft.pk}" disabled')
            draft.reviewed_at = timezone.now()
            if isinstance(draft, WebAIDraft):
                draft.steps = []
            else:
                draft.configuration = {}
            draft.save()
            self.assertContains(self.page(route, batch), f'name="draft_ids" value="{draft.pk}" disabled')

    def test_reviewed_valid_drafts_can_be_selected(self):
        for route, batch, draft in self.pages():
            draft.reviewed_at = timezone.now(); draft.save()
            page = self.page(route, batch)
            self.assertContains(page, f'name="draft_ids" value="{draft.pk}">')

    def test_deleted_imported_configuration_is_history_not_broken_link(self):
        for route, batch, draft in self.pages():
            draft.imported_at = timezone.now(); draft.save()
            page = self.page(route, batch)
            self.assertContains(page, '原脚本已删除，历史草稿仍保留。')
            self.assertNotContains(page, 'name="draft_ids"')

    def test_imported_api_config_without_business_case_has_safe_edit_link(self):
        config = APICase.objects.create(owner=self.owner, product=self.product, name='旧配置', path='/health')
        self.api_draft.api_case = config
        self.api_draft.imported_at = timezone.now(); self.api_draft.save()
        page = self.page('ai_assistant:api_ai_detail', self.api_batch)
        self.assertContains(page, reverse('ai_assistant:api_case_edit', args=[self.product.pk, config.pk]))

    def test_draft_pages_are_owner_private(self):
        self.client.force_login(self.other)
        for route, batch, _ in self.pages():
            self.assertEqual(self.page(route, batch).status_code, 404)

    def test_readonly_hides_write_controls_but_keeps_previews(self):
        self.owner.groups.add(Group.objects.get_or_create(name=ROLE_VIEWER)[0])
        for route, batch, _ in self.pages():
            page = self.page(route, batch)
            self.assertContains(page, '返回脚本列表')
            self.assertContains(page, 'automation-draft-table')
            for text in ('重新生成', '编辑与复核', 'name="draft_ids"', 'id="automation-draft-save"'):
                self.assertNotContains(page, text)

    def test_import_still_rejects_unreviewed_draft(self):
        for route, batch, draft in [('web_testing:ai_import', self.web_batch, self.web_draft),
                                    ('ai_assistant:api_ai_import', self.api_batch, self.api_draft)]:
            response = self.client.post(reverse(route, args=[batch.pk]), {'draft_ids': [draft.pk]}, secure=True)
            self.assertEqual(response.status_code, 302)
            draft.refresh_from_db(); self.assertIsNone(draft.imported_at)

    def test_preflight_exposes_only_owned_safe_environment_metadata(self):
        env = APIEnvironment.objects.create(owner=self.owner, product=self.product, name='本人环境',
                    base_url='http://127.0.0.1:8123', headers={'secret': 'HIDDEN-HEADER'},
                    variables={'password': 'HIDDEN-VARIABLE'}, timeout=17,
                    secret_headers_encrypted=encrypt_api_key('{"token":"HIDDEN-TOKEN"}'))
        other = APIEnvironment.objects.create(owner=self.other, product=self.product, name='PRIVATE-ENV', base_url='https://other.invalid')
        page = self.client.get(reverse('ai_assistant:api_submit', args=[self.product.pk]), secure=True)
        self.assertContains(page, '执行预览')
        self.assertEqual(page.context['environment_previews'], {str(env.pk): {'name': env.name, 'base_url': env.base_url, 'timeout': 17}})
        for secret in ('HIDDEN-HEADER', 'HIDDEN-VARIABLE', 'HIDDEN-TOKEN', other.base_url, other.name):
            self.assertNotContains(page, secret)
        self.assertEqual(APIRun.objects.count(), 0)
