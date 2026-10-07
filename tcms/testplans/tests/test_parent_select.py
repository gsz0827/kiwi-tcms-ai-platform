from django.test import TestCase, RequestFactory
from django.urls import reverse
from guardian.shortcuts import assign_perm
from tcms.tests.factories import UserFactory, ProductFactory, VersionFactory, TestPlanFactory
from tcms.testplans.forms import NewPlanForm


class ParentSelectTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True)
        self.product = ProductFactory()
        self.version = VersionFactory(product=self.product)
        self.parent = TestPlanFactory(product=self.product, product_version=self.version, name='Parent')
        self.child = TestPlanFactory(product=self.product, product_version=self.version, parent=self.parent)
        self.grandchild = TestPlanFactory(product=self.product, product_version=self.version, parent=self.child)
        self.foreign = TestPlanFactory(name='Private foreign parent')
        self.request = RequestFactory().get('/')
        self.request.user = self.owner
        self.payload = dict(name='New child', author=self.owner.pk, product=self.product.pk,
                            product_version=self.version.pk, type=self.parent.type_id,
                            parent=self.parent.pk, is_active=True, text='Test')

    def test_renders_native_select_with_clear_option_and_labels(self):
        form = NewPlanForm(request=self.request)
        html = str(form['parent'])
        self.assertIn('<select', html)
        self.assertIn('name="parent"',html)
        self.assertIn('无上级计划',html)
        self.assertIn(f'TP-{self.parent.pk} · Parent · {self.version.value}',html)
        self.assertIn(f'data-product="{self.product.pk}"',html)

    def test_can_assign_and_clear_parent(self):
        form = NewPlanForm(self.payload, request=self.request)
        self.assertTrue(form.is_valid(),form.errors)
        saved = form.save(); self.assertEqual(saved.parent_id,self.parent.pk)
        self.payload['parent']=''
        form = NewPlanForm(self.payload, request=self.request, instance=saved)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertIsNone(form.save().parent_id)

    def test_edit_excludes_self_and_all_descendants(self):
        form = NewPlanForm(instance=self.parent,request=self.request)
        choices=set(form.fields['parent'].queryset.values_list('pk',flat=True))
        self.assertTrue(choices.isdisjoint({self.parent.pk,self.child.pk,self.grandchild.pk}))
        self.payload['parent']=self.grandchild.pk
        self.assertFalse(NewPlanForm(self.payload,instance=self.parent,request=self.request).is_valid())

    def test_cross_project_is_rejected_even_with_forged_post(self):
        self.payload['parent']=self.foreign.pk
        form=NewPlanForm(self.payload,request=self.request)
        self.assertFalse(form.is_valid())
        self.assertIn('父级计划必须属于同一项目。',form.errors['parent'])

    def test_choices_do_not_expose_invisible_plans(self):
        self.request.user=UserFactory()
        assign_perm('testplans.view_testplan',self.request.user,self.parent)
        form=NewPlanForm(request=self.request)
        self.assertEqual(list(form.fields['parent'].queryset.values_list('pk',flat=True)),[self.parent.pk])
        self.assertNotIn(self.foreign.name,str(form['parent']))
        self.payload['parent']=self.child.pk
        self.assertFalse(NewPlanForm(self.payload,request=self.request).is_valid())

    def test_unreadable_current_parent_can_be_preserved_without_title_leak(self):
        self.request.user=UserFactory()
        form=NewPlanForm(instance=self.child,request=self.request)
        html=str(form['parent'])
        self.assertIn('当前上级计划（无查看权限）',html)
        self.assertNotIn('· Parent ·',html)
        form=NewPlanForm(self.payload,instance=self.child,request=self.request)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.save().parent_id,self.parent.pk)

    def test_get_and_edit_show_dropdown_without_old_keyboard_help(self):
        self.client.force_login(self.owner)
        for url in [reverse('plans-new'),reverse('plan-edit',args=[self.child.pk])]:
            response=self.client.get(url,secure=True)
            self.assertContains(response,'<select name="parent"',html=False)
            self.assertNotContains(response,'input-select-parent')
            self.assertNotContains(response,'js-parent-id-value')

    def test_bound_invalid_form_keeps_parent_selection(self):
        self.payload['name']=''
        form=NewPlanForm(self.payload,request=self.request)
        self.assertFalse(form.is_valid())
        self.assertIn(f'value="{self.parent.pk}" selected',str(form['parent']))
