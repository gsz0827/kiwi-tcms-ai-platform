from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import Group
from tcms.tests.factories import ProductFactory, UserFactory
from tcms.web_testing.models import WebCase
from tcms.ai_assistant.models import APICase, ProjectResourceFolder as Folder, ProjectResourceAssignment as Assignment


class AutomationFolderTests(TestCase):
    def setUp(self):
        self.owner, self.other = UserFactory(), UserFactory()
        self.product, self.other_product = ProductFactory(), ProductFactory()
        self.client.force_login(self.owner)
        self.cases, self.foreign, self.folders = {}, {}, {}
        for kind, model in [('web_case', WebCase), ('api_case', APICase)]:
            self.cases[kind] = model.objects.create(owner=self.owner, product=self.product, name=kind+' visible')
            self.foreign[kind] = model.objects.create(owner=self.other, product=self.product, name=kind+' SECRET')
            self.folders[kind] = Folder.objects.create(product=self.product, resource_type=kind, name=kind+' shared')
        self.manual = Folder.objects.create(product=self.product, resource_type='case', name='MANUAL_ONLY')

    def page(self, kind, **params):
        params.setdefault('product', self.product.pk)
        if kind == 'api_case': params['tab'] = 'cases'
        return self.client.get(reverse('web_testing:cases' if kind == 'web_case' else 'ai_assistant:api_home'), params, secure=True)

    def move(self, kind, case=None, folder=None):
        return self.client.post(reverse('ai_assistant:assign_resource_folder'), {
            'resource_type':kind, 'object_id':(case or self.cases[kind]).pk,
            'folder':folder.pk if folder else '', 'next':'/'}, secure=True)

    def test_shared_layout_preserves_private_cases_and_separate_types(self):
        for kind in self.cases:
            response = self.page(kind)
            self.assertContains(response, 'class="kiwi-resource-pane"')
            self.assertContains(response, '管理共享目录')
            self.assertContains(response, self.folders[kind].name)
            self.assertContains(response, self.cases[kind].name)
            self.assertNotContains(response, self.foreign[kind].name)
            self.assertNotContains(response, 'MANUAL_ONLY')
        self.client.force_login(self.other)
        for kind in self.cases:
            self.assertContains(self.page(kind), self.folders[kind].name)
            self.assertNotContains(self.page(kind), self.cases[kind].name)

    def test_move_to_folder_and_unfiled(self):
        for kind in self.cases:
            self.assertEqual(self.move(kind, folder=self.folders[kind]).status_code, 302)
            self.assertEqual(Assignment.objects.get(resource_type=kind, object_id=self.cases[kind].pk).folder_id, self.folders[kind].pk)
            self.assertEqual(self.move(kind).status_code, 302)
            self.assertFalse(Assignment.objects.filter(resource_type=kind, object_id=self.cases[kind].pk).exists())

    def test_reject_foreign_owner_product_and_resource_type(self):
        for kind in self.cases:
            self.assertEqual(self.move(kind, case=self.foreign[kind], folder=self.folders[kind]).status_code, 404)
            wrong = Folder.objects.create(product=self.other_product, resource_type=kind, name='wrong project')
            self.assertEqual(self.move(kind, folder=wrong).status_code, 403)
            self.assertEqual(self.move(kind, folder=self.manual).status_code, 403)

    def test_parent_includes_children_and_filters_match(self):
        for kind in self.cases:
            child = Folder.objects.create(product=self.product, resource_type=kind, name='Child', parent=self.folders[kind])
            self.move(kind, folder=child)
            response = self.page(kind, folder=self.folders[kind].pk)
            self.assertEqual([c.pk for c in response.context['cases']], [self.cases[kind].pk])
            self.assertEqual(self.page(kind, folder='unfiled').context['cases'].paginator.count, 0)
            self.assertEqual(self.page(kind, folder='invalid').context['cases'].paginator.count, 0)
            self.assertEqual(self.page(kind, q='no match').context['cases'].paginator.count, 0)
            self.assertNotContains(self.page(kind, q='no match'), self.cases[kind].name)
            wrong = Folder.objects.create(product=self.other_product, resource_type=kind, name='OTHER_PRODUCT_FOLDER')
            self.assertNotContains(self.page(kind), wrong.name)

    def test_crud_keeps_cases_when_folder_deleted(self):
        for kind in self.cases:
            self.assertEqual(self.client.post(reverse('ai_assistant:create_resource_folder'), {
                'resource_type':kind, 'product':self.product.pk, 'parent':self.folders[kind].pk, 'name':'Child'}, secure=True).status_code, 302)
            child = Folder.objects.get(resource_type=kind, name='Child')
            self.move(kind, folder=child)
            self.assertEqual(self.client.post(reverse('ai_assistant:rename_resource_folder', args=[child.pk]), {'name':'Renamed'}, secure=True).status_code, 302)
            child.refresh_from_db(); self.assertEqual(child.name, 'Renamed')
            self.assertEqual(self.client.post(reverse('ai_assistant:delete_resource_folder', args=[child.pk]), secure=True).status_code, 302)
            self.assertTrue(type(self.cases[kind]).objects.filter(pk=self.cases[kind].pk).exists())
            self.assertFalse(Assignment.objects.filter(resource_type=kind, object_id=self.cases[kind].pk).exists())

    def test_readonly_cannot_change_folders(self):
        group, _ = Group.objects.get_or_create(name='AI 只读'); self.owner.groups.add(group)
        for kind in self.cases:
            self.assertEqual(self.move(kind, folder=self.folders[kind]).status_code, 403)
            self.assertEqual(self.client.post(reverse('ai_assistant:create_resource_folder'), {
                'resource_type':kind, 'product':self.product.pk, 'name':'denied'}, secure=True).status_code, 403)
            self.assertNotContains(self.page(kind), '管理共享目录')

    def test_no_pane_on_other_api_tabs(self):
        for tab in ['environments', 'suites', 'runs']:
            response = self.client.get(reverse('ai_assistant:api_home'), {'product':self.product.pk, 'tab':tab}, secure=True)
            self.assertNotContains(response, 'class="kiwi-resource-pane"')
