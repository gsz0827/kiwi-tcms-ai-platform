"""Naming-only regressions; menu routes, query parameters and permissions stay intact."""
from types import SimpleNamespace
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve, reverse

from tcms.ai_assistant.templatetags.ai_navigation import platform_navigation
from tcms.web_testing.navigation import TEST_NAV_SECTIONS


class EnvironmentNamingTests(SimpleTestCase):
    def navigation(self, path, **params):
        request = RequestFactory().get(path, params)
        request.resolver_match = resolve(path)
        with patch('tcms.ai_assistant.templatetags.ai_navigation._user_has_perm', return_value=True), \
             patch('tcms.ai_assistant.templatetags.ai_navigation.plugin_menu_entries', return_value=[]):
            return platform_navigation({'request': request})

    def test_api_menu_name_and_target_are_consistent(self):
        section = next(section for section in TEST_NAV_SECTIONS if section['key'] == 'api')
        item = next(item for item in section['items'] if item.get('query') == 'tab=environments')
        self.assertEqual(item['label'], '测试环境')
        self.assertEqual(item['url'], 'ai_assistant:api_home')
        self.assertIn('ai_assistant:api_environment_edit', item['names'])
        self.assertEqual([item['label'] for item in section['items']],
                         ['测试环境', '自动化脚本', '测试套件', '执行任务'])

    def test_api_environment_tab_still_highlights_only_environment_menu(self):
        nav = self.navigation(reverse('ai_assistant:api_home'), tab='environments')
        section = next(section for section in nav['sections'] if section['key'] == 'api')
        active = [item for item in section['items'] if item['is_active']]
        self.assertEqual([item['label'] for item in active], ['测试环境'])
        self.assertEqual(active[0]['url'], reverse('ai_assistant:api_home') + '?tab=environments')

    def test_other_api_tabs_keep_their_active_items(self):
        for tab, label in [('cases', '自动化脚本'), ('suites', '测试套件'), ('runs', '执行任务')]:
            nav = self.navigation(reverse('ai_assistant:api_home'), tab=tab)
            section = next(section for section in nav['sections'] if section['key'] == 'api')
            self.assertEqual([item['label'] for item in section['items'] if item['is_active']], [label])

    def test_web_header_has_no_environment_shortcut_for_any_role(self):
        request = RequestFactory().get(reverse('web_testing:cases'))
        for can_write in (True, False):
            html = render_to_string('ai_assistant/automation/header.html', {
                'request': request, 'title': '自动化脚本', 'show_environments': True,
                'product': SimpleNamespace(pk=7), 'automation_access': {'can_write': can_write},
            })
            self.assertNotIn('>测试环境</a>', html)
            self.assertNotIn(reverse('web_testing:environments') + '?product=7', html)
            self.assertNotIn('环境设置', html)

    def test_no_legacy_label_in_automation_navigation_or_header(self):
        labels = [item['label'] for section in TEST_NAV_SECTIONS for item in section['items']]
        self.assertNotIn('接口管理', labels)
        self.assertNotIn('环境设置', labels)
        html = render_to_string('ai_assistant/automation/header.html', {
            'request': RequestFactory().get('/'), 'title': '测试套件', 'show_environments': True,
            'automation_access': {'can_write': False},
        })
        self.assertNotIn(f'href="{reverse("web_testing:environments")}"', html)

    def test_web_and_api_sections_have_same_menu_order(self):
        for key in ('web', 'api'):
            section = next(section for section in TEST_NAV_SECTIONS if section['key'] == key)
            self.assertEqual([item['label'] for item in section['items']],
                             ['测试环境', '自动化脚本', '测试套件', '执行任务'])

    def test_all_web_environment_routes_highlight_their_own_menu(self):
        for route, args in [('web_testing:environments', []),
                            ('web_testing:environment_new', []),
                            ('web_testing:environment_edit', [7])]:
            nav = self.navigation(reverse(route, args=args))
            section = next(section for section in nav['sections'] if section['key'] == 'web')
            self.assertTrue(section['is_current'])
            active = [item for item in section['items'] if item['is_active']]
            self.assertEqual([item['label'] for item in active], ['测试环境'])
            self.assertEqual(active[0]['url'], reverse('web_testing:environments'))
            self.assertFalse(next(section for section in nav['sections'] if section['key'] == 'api')['is_current'])

    def test_other_web_routes_keep_their_original_highlights(self):
        for route, args, label in [('web_testing:cases', [], '自动化脚本'),
                                   ('web_testing:case_edit', [7], '自动化脚本'),
                                   ('web_testing:suites', [], '测试套件'),
                                   ('web_testing:suite_edit', [7], '测试套件'),
                                   ('web_testing:runs', [], '执行任务')]:
            nav = self.navigation(reverse(route, args=args))
            section = next(section for section in nav['sections'] if section['key'] == 'web')
            self.assertEqual([item['label'] for item in section['items'] if item['is_active']], [label])

    def test_environment_routes_are_not_registered_to_suite_menu(self):
        section = next(section for section in TEST_NAV_SECTIONS if section['key'] == 'web')
        suite = next(item for item in section['items'] if item['label'] == '测试套件')
        env = next(item for item in section['items'] if item['label'] == '测试环境')
        self.assertFalse(set(suite['names']) & set(env['names']))
        self.assertEqual(env['url'], 'web_testing:environments')
        self.assertEqual(set(env['names']), {'web_testing:environments', 'web_testing:environment_new', 'web_testing:environment_edit'})

    def test_header_keeps_primary_actions_without_duplicate_environment_navigation(self):
        request = RequestFactory().get(reverse('web_testing:cases'))
        for title in ('自动化脚本', '测试套件'):
            html = render_to_string('ai_assistant/automation/header.html', {
                'request': request, 'title': title, 'automation_access': {'can_write': True},
                'create_url': '/new/', 'create_label': '新建脚本' if title == '自动化脚本' else '新建套件',
                'ai_url': '/ai-generate/' if title == '自动化脚本' else '', 'ai_label': 'AI 生成脚本',
            })
            self.assertIn('新建脚本' if title == '自动化脚本' else '新建套件', html)
            self.assertNotIn(reverse('web_testing:environments'), html)
            self.assertNotIn('>测试环境</a>', html)
            if title == '自动化脚本':
                self.assertIn('AI 生成脚本', html)

    def test_environment_creation_action_uses_scoped_url_and_is_write_only(self):
        for url in (reverse('web_testing:environment_new') + '?product=7',
                    reverse('ai_assistant:api_environment_new', args=[7])):
            for can_write in (True, False):
                html = render_to_string('ai_assistant/automation/environment_action.html', {
                    'environment_create_url': url, 'automation_access': {'can_write': can_write},
                })
                if can_write:
                    self.assertIn(f'href="{url}"', html)
                    self.assertIn('新建环境', html)
                else:
                    self.assertNotIn('<a', html)

    def test_environment_creation_action_is_absent_without_valid_destination(self):
        html = render_to_string('ai_assistant/automation/environment_action.html', {
            'automation_access': {'can_write': True},
        })
        self.assertNotIn('<a', html)

    def test_environment_creation_action_opens_safe_new_tab(self):
        html = render_to_string('ai_assistant/automation/environment_action.html', {
            'environment_create_url': '/environment/new/?product=7',
            'automation_access': {'can_write': True},
        })
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn('data-environment-create', html)

    def test_environment_select_and_action_share_inline_control(self):
        from django import forms

        class EnvironmentSelection(forms.Form):
            environment = forms.ChoiceField(choices=[('1', '测试环境')], label='执行环境')
            name = forms.CharField(label='套件名称', required=False)

        for can_write in (True, False):
            html = render_to_string('ai_assistant/automation/form_sections.html', {
                'form': EnvironmentSelection(initial={'environment': '1'}),
                'environment_create_url': '/environment/new/?product=7',
                'automation_access': {'can_write': can_write},
            })
            control = html.split('<div class="automation-environment-control">', 1)[1].split('</div>', 1)[0]
            self.assertIn('name="environment"', control)
            self.assertIn('selected', control)
            self.assertEqual('新建环境' in control, can_write)
            self.assertEqual(html.count('新建环境'), int(can_write))
            self.assertIn('for="id_environment"', html)

    def test_form_without_environment_does_not_gain_creation_action(self):
        from django import forms
        form = forms.Form()
        form.fields['name'] = forms.CharField(label='配置名称')
        html = render_to_string('ai_assistant/automation/form_sections.html', {
            'form': form, 'environment_create_url': '/new/',
            'automation_access': {'can_write': True},
        })
        self.assertNotIn('新建环境', html)

    def test_environment_select_without_destination_is_unchanged(self):
        from django import forms
        form = forms.Form()
        form.fields['environment'] = forms.ChoiceField(choices=[('1', '测试环境')])
        html = render_to_string('ai_assistant/automation/form_sections.html', {
            'form': form, 'automation_access': {'can_write': True},
        })
        self.assertIn('name="environment"', html)
        self.assertNotIn('新建环境', html)
