import json
from importlib import import_module
from types import SimpleNamespace
from django.apps import apps
from django.db import connection
from django.test import TestCase, SimpleTestCase, override_settings
from django.template import Template, Context
from django.template.loader import get_template
from django.urls import reverse
from tcms.tests.factories import UserFactory, ProductFactory
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from tcms.ai_assistant.templatetags.ui_labels import ui_term
from tcms.ai_assistant.models import AIRegressionVerification
from tcms.web_testing.models import WebEnvironment, WebSuite, WebCase
from tcms.web_testing.forms import SuiteForm
from tcms.web_testing.execution_config import suite_snapshot


class TerminologyTests(SimpleTestCase):
    def test_known_labels_are_normalized_without_changing_values(self):
        for old, new in [('开发任务单', '开发任务'), ('环境变量', '环境参数'),
                         ('测试运行', '执行任务'), ('自动化配置', '自动化脚本'),
                         ('回归通过', '复测通过'), ('回归测试运行', '复测执行任务')]:
            self.assertEqual(ui_term(old), new)
        self.assertEqual(ui_term('回归测试'), '回归测试')
        self.assertEqual(ui_term('passed'), 'passed')

    def test_status_display_uses_new_terms_without_model_metadata_migration(self):
        value = AIRegressionVerification(status='passed')
        output = Template('{% load ui_labels %}{{ value.get_status_display|ui_term }}').render(Context({'value': value}))
        self.assertEqual(output, '复测通过')
        self.assertEqual(value.status, 'passed')

    def test_all_custom_templates_compile(self):
        from pathlib import Path
        # Check compile order even for partials not covered by individual view tests.
        for app in ('ai_assistant', 'web_testing'):
            directory = Path(__file__).resolve().parents[1] / app / 'templates'
            for path in directory.rglob('*.html'):
                with self.subTest(template=str(path)):
                    get_template(str(path.relative_to(directory)))


@override_settings(WEB_TEST_ALLOWED_ORIGINS=['https://kiwi-web:8443'])
class SuiteEnvironmentUITests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_active=True, is_superuser=True)
        self.product = ProductFactory()
        self.environment = WebEnvironment.objects.create(owner=self.owner, product=self.product,
            name='Site', base_url='https://kiwi-web:8443', ignore_https_errors=True)
        self.case = WebCase.objects.create(owner=self.owner, product=self.product, name='Page',
            steps_encrypted=encrypt_api_key(json.dumps([{'action':'goto','value':'/'}, {'action':'assert_visible','selector':'body'}])))
        self.suite = WebSuite.objects.create(owner=self.owner, product=self.product, environment=self.environment,
            name='Suite', base_url=self.environment.base_url, case_ids=[self.case.pk])

    def payload(self, **extra):
        return dict(product=self.product.pk, name='Saved suite', environment=self.environment.pk,
                    cases=[self.case.pk], datasets='[]') | extra

    def test_suite_requires_environment_and_has_no_duplicate_inputs(self):
        form = SuiteForm(owner=self.owner)
        self.assertTrue(form.fields['environment'].required)
        self.assertNotIn('base_url', form.fields)
        self.assertNotIn('ignore_https_errors', form.fields)
        rejected = SuiteForm(self.payload(environment=''), owner=self.owner)
        self.assertFalse(rejected.is_valid())
        self.assertIn('environment', rejected.errors)

    def test_environment_is_authoritative_even_if_client_posts_legacy_overrides(self):
        form = SuiteForm(self.payload(base_url='https://evil.invalid', ignore_https_errors=''), owner=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        suite = form.save()
        self.assertEqual(suite.base_url, self.environment.base_url)
        self.assertTrue(suite.ignore_https_errors)
        self.environment.base_url = 'https://kiwi-web:8443/updated'
        self.environment.save()
        self.assertEqual(suite_snapshot(suite)['base_url'], self.environment.base_url)

    def test_environment_cannot_belong_to_other_account_or_project(self):
        for owner, product in [(UserFactory(), self.product), (self.owner, ProductFactory())]:
            foreign = WebEnvironment.objects.create(owner=owner, product=product, name='Other', base_url=self.environment.base_url)
            form = SuiteForm(self.payload(environment=foreign.pk), owner=self.owner)
            self.assertFalse(form.is_valid())
            self.assertIn('environment', form.errors)

    def test_view_has_only_readonly_site_and_certificate_preview(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('web_testing:suite_edit', args=[self.suite.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-environment-site')
        self.assertContains(response, 'data-environment-certificate')
        self.assertNotContains(response, 'name="base_url"')
        self.assertNotContains(response, 'name="ignore_https_errors"')
        self.assertEqual(response.context['environment_previews'][str(self.environment.pk)]['base_url'], self.environment.base_url)

    def test_legacy_migration_preserves_behavior_and_is_idempotent(self):
        legacy = WebSuite.objects.create(owner=self.owner, product=self.product, name='Legacy',
            base_url='https://kiwi-web:8443/legacy', ignore_https_errors=True, case_ids=[self.case.pk])
        before = suite_snapshot(legacy)
        migration = import_module('tcms.web_testing.migrations.0007_bind_legacy_suite_environments')
        editor = SimpleNamespace(connection=connection)
        migration.bind_legacy_environments(apps, editor)
        legacy.refresh_from_db()
        self.suite.refresh_from_db()
        env = legacy.environment
        self.assertEqual(env.owner_id, self.owner.pk)
        self.assertEqual(env.product_id, self.product.pk)
        self.assertEqual(env.base_url, legacy.base_url)
        self.assertTrue(env.ignore_https_errors)
        self.assertEqual(env.variables_encrypted, '')
        self.assertIsNone(env.setup_case_id)
        self.assertEqual(suite_snapshot(legacy), before)
        self.assertEqual(self.suite.environment_id, self.environment.pk)
        count = WebEnvironment.objects.count()
        migration.bind_legacy_environments(apps, editor)
        self.assertEqual(WebEnvironment.objects.count(), count)
