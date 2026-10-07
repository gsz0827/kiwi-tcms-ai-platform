"""Changing requirement must scope candidates and POST validation to the selected product."""

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from tcms.tests.factories import ProductFactory

from . import roles, test_document_layout
from .forms import AIDevTaskForm
from .models import AIDevTask, AIRequest, AIJob


from .edit_test_client import EditClient


class TaskAssigneeLinkTests(TestCase):
    client_class = EditClient
    def setUp(self):
        test_document_layout.DocumentLayoutTests.setUp(self)
        self.other_product = ProductFactory()
        roles.add_product_member(self.author, self.other_product)
        roles.add_product_member(self.outsider, self.other_product)
        self.other_requirement = AIRequest.objects.create(
            created_by=self.author,
            category=self.other_product.category.get(name="--default--"),
            title="项目 B 的需求",
            requirement="B 项目规则",
            version=7,
        )
        self.task.assignee = self.member
        self.task.save()

    def choices(self, requirement=None, task=None, **params):
        query = {"request": (requirement or self.requirement).pk}
        if task:
            query["task"] = task.pk
        return self.client.get(
            reverse("ai_assistant:dev_task_assignees"), query | params, secure=True
        )

    def payload(self, requirement=None, assignee=None, **changes):
        return {
            "request": (requirement or self.requirement).pk,
            "task_number": "DEV-X",
            "title": self.task.title,
            "module": "模块",
            "description": "开发说明",
            "acceptance": "验收规则",
            "priority": "P2",
            "estimate_hours": "",
            "assignee": assignee.pk if assignee else "",
            "status": "todo",
            "return_detail": "1",
        } | changes

    def test_options_are_members_of_selected_product_only_and_uncached(self):
        for source, included, excluded in [
            (self.requirement, self.member, self.outsider),
            (self.other_requirement, self.outsider, self.member),
        ]:
            page = self.choices(source)
            self.assertEqual(page.status_code, 200)
            ids = {item["id"] for item in page.json()["assignees"]}
            self.assertIn(included.pk, ids)
            self.assertNotIn(excluded.pk, ids)
            self.assertEqual(page.json()["request_id"], source.pk)
            self.assertEqual(page.json()["product_id"], source.category.product_id)
            self.assertIn("no-store", page.headers["Cache-Control"])

    def test_editor_endpoint_matches_existing_task_edit_access(self):
        self.assertEqual(self.choices(self.other_requirement, self.task).status_code, 200)
        # A project reader cannot obtain create-form options without creation permission.
        self.client.force_login(self.member)
        self.assertEqual(self.choices(self.requirement).status_code, 403)
        self.assertEqual(self.choices(self.requirement, self.task).status_code, 403)

    def test_readonly_cannot_fetch_member_options_even_for_visible_requirement(self):
        self.author.groups.add(Group.objects.get(name=roles.ROLE_VIEWER))
        self.assertEqual(self.choices().status_code, 403)
        self.assertEqual(self.choices(task=self.task).status_code, 403)

    def test_invisible_requirement_and_task_are_not_exposed(self):
        private = AIRequest.objects.create(
            created_by=self.outsider, title="私人需求", requirement="private"
        )
        private_task = AIDevTask.objects.create(
            request=private, owner=self.outsider, title="private task"
        )
        self.assertEqual(self.choices(private).status_code, 404)
        self.assertEqual(self.choices(self.requirement, private_task).status_code, 404)

    def test_assignee_only_user_cannot_query_other_product_member_options(self):
        self.task.assignee = self.outsider
        self.task.save()
        self.client.force_login(self.outsider)
        self.assertEqual(self.choices(self.other_requirement, self.task).status_code, 403)
        self.assertEqual(self.choices(self.requirement, self.task).status_code, 404)

    def test_anonymous_requires_login_and_post_is_rejected(self):
        url = reverse("ai_assistant:dev_task_assignees")
        self.assertEqual(
            self.client.post(url, {"request": self.requirement.pk}, secure=True).status_code, 405
        )
        self.client.logout()
        self.assertEqual(
            self.client.get(url, {"request": self.requirement.pk}, secure=True).status_code, 302
        )

    def test_invalid_parameters_are_rejected_not_server_errors(self):
        for query in [
            {"request": ""},
            {"request": "bad"},
            {"request": "²"},
            {"task": "bad"},
            {"request": "9" * 5000},
            {"request": "9" * 100},
            {"task": "0"},
            {"task": "9" * 100},
        ]:
            response = self.client.get(
                reverse("ai_assistant:dev_task_assignees"),
                {"request": self.requirement.pk} | query,
                secure=True,
            )
            self.assertEqual(response.status_code, 400)

    def test_requirement_without_product_has_no_candidates(self):
        source = AIRequest.objects.create(
            created_by=self.author, title="无项目需求", requirement="rule"
        )
        self.assertEqual(self.choices(source).json()["assignees"], [])
        self.assertIsNone(self.choices(source).json()["product_id"])

    def test_member_names_are_data_not_html_and_no_profile_details_are_exposed(self):
        self.member.username = "<img src=x onerror=alert(1)>"
        self.member.save()
        people = self.choices().json()["assignees"]
        record = next(item for item in people if item["id"] == self.member.pk)
        self.assertEqual(record, {"id": self.member.pk, "name": self.member.username})

    def test_existing_get_uses_original_product_but_post_uses_selected_product(self):
        initial = AIDevTaskForm(instance=self.task, user=self.author)
        self.assertIn(self.member, initial.fields["assignee"].queryset)
        form = AIDevTaskForm(
            self.payload(self.other_requirement, self.outsider), instance=self.task, user=self.author
        )
        self.assertIn(self.outsider, form.fields["assignee"].queryset)
        self.assertNotIn(self.member, form.fields["assignee"].queryset)
        self.assertTrue(form.is_valid(), form.errors)

    def test_old_product_member_cannot_be_forged_when_switching_requirement(self):
        page = self.client.post(
            reverse("ai_assistant:edit_dev_task", args=[self.task.pk]),
            self.payload(self.other_requirement, self.member),
            secure=True,
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("assignee", page.context["form"].errors)
        self.task.refresh_from_db()
        self.assertEqual(self.task.request_id, self.requirement.pk)
        self.assertEqual(self.task.assignee_id, self.member.pk)

    def test_switch_saves_selected_product_member_and_correct_revision_then_marks_review(self):
        page = self.client.post(
            reverse("ai_assistant:edit_dev_task", args=[self.task.pk]),
            self.payload(self.other_requirement, self.outsider),
            secure=True,
        )
        self.assertEqual(page.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.request_id, self.other_requirement.pk)
        self.assertEqual(self.task.assignee_id, self.outsider.pk)
        self.assertEqual(self.task.requirement_version, 7)
        self.assertTrue(self.task.needs_update)
        self.assertEqual(self.task.assigned_by_id, self.author.pk)

    def test_new_task_uses_current_requirement_revision_and_member(self):
        page = self.client.post(
            reverse("ai_assistant:dev_task_create"),
            self.payload(self.other_requirement, self.outsider, title="新建 B 文档"),
            secure=True,
        )
        self.assertEqual(page.status_code, 302)
        task = AIDevTask.objects.get(title="新建 B 文档")
        self.assertEqual(task.requirement_version, 7)
        self.assertFalse(task.needs_update)
        self.assertEqual(task.assignee_id, self.outsider.pk)

    def test_status_only_save_cannot_change_source_or_review_flag(self):
        self.task.assignee = self.outsider
        self.task.needs_update = True
        self.task.save()
        self.client.force_login(self.outsider)
        self.client.post(
            reverse("ai_assistant:edit_dev_task", args=[self.task.pk]),
            {"status": "doing", "request": self.other_requirement.pk, "assignee": self.author.pk},
            secure=True,
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.request_id, self.requirement.pk)
        self.assertEqual(self.task.assignee_id, self.outsider.pk)
        self.assertTrue(self.task.needs_update)

    def test_missing_bound_request_never_falls_back_to_original_candidates(self):
        data = self.payload()
        data.pop("request")
        form = AIDevTaskForm(data, instance=self.task, user=self.author)
        self.assertFalse(form.fields["assignee"].queryset.exists())
        self.assertFalse(form.is_valid())

    def test_get_does_not_save_documents_or_queue_ai(self):
        before = list(AIRequest.objects.values()), list(AIDevTask.objects.values())
        self.choices()
        self.choices(self.other_requirement, self.task)
        self.assertEqual(before, (list(AIRequest.objects.values()), list(AIDevTask.objects.values())))
        self.assertFalse(AIJob.objects.exists())
