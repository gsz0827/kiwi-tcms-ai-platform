from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tcms.tests import create_request_user, user_should_have_perm
from tcms.kiwi_auth.models import UserPreference


class SimplifiedProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_request_user('profile-owner', 'password')
        cls.other = create_request_user('profile-other', 'password')

    def setUp(self):
        self.client.force_login(self.owner)

    def test_own_profile_has_one_save_and_no_admin_permissions(self):
        response = self.client.get(reverse('tcms-profile', args=[self.owner.pk]), secure=True)
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertContains(response, 'name="_save"', count=1)
        self.assertNotContains(response, '用户权限')
        self.assertNotContains(response, '超级用户状态')
        self.assertNotContains(response, '保存并增加另一个')

    def test_owner_updates_only_names(self):
        response = self.client.post(reverse('tcms-profile', args=[self.owner.pk]), {
            'first_name': '小明', 'last_name': '测试'}, secure=True)
        self.assertRedirects(response, reverse('tcms-profile', args=[self.owner.pk]))
        self.owner.refresh_from_db()
        self.assertEqual((self.owner.first_name, self.owner.last_name), ('小明', '测试'))

    def test_other_profile_is_forbidden_without_permission_and_readonly_with_it(self):
        url = reverse('tcms-profile', args=[self.other.pk])
        self.assertEqual(self.client.get(url, secure=True).status_code, 403)
        user_should_have_perm(self.owner, 'auth.view_user')
        response = self.client.get(url, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')

    def test_display_settings_are_account_owned(self):
        url = reverse('display-settings')
        response = self.client.post(url, {'language': 'zh-hans', 'time_zone': 'Asia/Shanghai'}, secure=True)
        self.assertRedirects(response, url)
        preference = UserPreference.objects.get(user=self.owner)
        self.assertEqual(preference.time_zone, 'Asia/Shanghai')
        self.assertEqual(response.cookies['django_language'].value, 'zh-hans')

    def test_navbar_has_no_duplicate_run_plan_links(self):
        UserPreference.objects.create(user=self.owner, language='zh-hans', time_zone='Asia/Shanghai')
        response = self.client.get(reverse('core-views-index'), secure=True)
        html = response.content.decode()
        user_menu = html[html.index('id="user-menu"'):html.index('id="logout_link"')]
        self.assertNotIn('default_tester=', user_menu)
        self.assertNotIn('?author=', user_menu)
        self.assertIn(reverse('display-settings'), user_menu)
        self.assertIn('data-time-zone="Asia/Shanghai"', html)
