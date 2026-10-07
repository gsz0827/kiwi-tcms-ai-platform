"""Related-document links must not broaden existing requirement/task visibility."""

from html.parser import HTMLParser

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from . import roles, test_shared_folders
from .models import AIDevTask, AIRequest


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.anchors = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.anchors.append(dict(attrs))


from .edit_test_client import EditClient


class RequirementTaskLinkTests(TestCase):
    client_class = EditClient
    def setUp(self):
        test_shared_folders.RequirementSharedFolderTests.setUp(self)
        self.first = AIDevTask.objects.create(
            request=self.requirement,
            owner=self.author,
            task_number="DEV-1",
            title="登录接口说明",
            description="校验验证码",
            acceptance="过期后拒绝登录",
        )
        self.second = AIDevTask.objects.create(
            request=self.requirement, owner=self.author, task_number="DEV-2", title="消息接口说明"
        )
        self.private_requirement = AIRequest.objects.create(
            created_by=self.outsider, title="PRIVATE-REQUIREMENT", requirement="PRIVATE-CONTENT"
        )
        self.private_task = AIDevTask.objects.create(
            request=self.private_requirement, owner=self.outsider, title="PRIVATE-TASK"
        )
        self.client.force_login(self.member)

    def task_url(self, task):
        return reverse("ai_assistant:dev_task_detail", args=[task.pk])

    def assert_new_tab_link(self, page, url):
        matching = [a for a in Links(page.content.decode()).anchors if a.get("href") == url]
        self.assertTrue(matching, url)
        for anchor in matching:
            self.assertEqual(anchor.get("target"), "_blank")
            self.assertIn("noopener", anchor.get("rel", "").split())
            self.assertIn("noreferrer", anchor.get("rel", "").split())

    def test_requirement_modal_has_new_tab_task_links_and_no_extra_heading_or_manager(self):
        page = self.client.get(reverse("ai_assistant:index"), secure=True)
        self.assertContains(page, "关联开发任务")
        self.assertNotContains(page, "管理共享目录")
        self.assertNotContains(page, '<h1 class="pull-left">需求与开发任务</h1>')
        self.assertNotContains(page, 'id="kiwi-folder-manager-requirement"')
        self.assertContains(page, f'data-directory-node="{self.folder.pk}"')
        self.assertContains(
            page, f'data-tree-create-url="{reverse("ai_assistant:create_resource_folder")}"'
        )
        self.assert_new_tab_link(page, self.task_url(self.first))
        self.assert_new_tab_link(page, self.task_url(self.second))
        self.assertNotContains(page, "PRIVATE-TASK")

    def test_full_requirement_detail_has_same_new_tab_links(self):
        page = self.client.get(
            reverse("ai_assistant:requirement_trace", args=[self.requirement.pk]), secure=True
        )
        self.assertContains(page, "关联开发任务")
        self.assert_new_tab_link(page, self.task_url(self.first))
        self.assert_new_tab_link(page, self.task_url(self.second))

    def test_task_detail_links_to_requirement_and_visible_siblings_not_itself(self):
        page = self.client.get(self.task_url(self.first), secure=True)
        self.assertContains(page, "校验验证码")
        self.assertContains(page, "过期后拒绝登录")
        self.assert_new_tab_link(
            page, reverse("ai_assistant:requirement_trace", args=[self.requirement.pk])
        )
        self.assert_new_tab_link(page, self.task_url(self.second))
        self.assertNotContains(page, f'href="{self.task_url(self.first)}"')
        self.assertNotContains(page, "PRIVATE-TASK")
        self.assertIn("no-store", page.headers["Cache-Control"])

    def test_list_titles_open_read_only_details(self):
        page = self.client.get(reverse("ai_assistant:dev_task_list"), secure=True)
        self.assertContains(page, f'href="{self.task_url(self.first)}"')

    def test_readonly_member_can_follow_links_without_edit_or_generate_controls(self):
        self.member.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        page = self.client.get(self.task_url(self.first), secure=True)
        self.assertEqual(page.status_code, 200)
        self.assert_new_tab_link(page, self.task_url(self.second))
        self.assertNotContains(page, "编辑开发任务")
        self.assertNotContains(page, "设计测试用例")

    def test_assignee_without_membership_cannot_see_other_documents(self):
        self.first.assignee = self.outsider
        self.first.save()
        self.client.force_login(self.outsider)
        page = self.client.get(self.task_url(self.first), secure=True)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "无权限查看关联需求")
        self.assertContains(page, "暂无关联开发任务")
        self.assertNotContains(page, self.requirement.title)
        self.assertNotContains(page, self.second.title)
        self.assertNotContains(page, "设计测试用例")
        self.assertEqual(self.client.get(self.task_url(self.second), secure=True).status_code, 404)

    def test_invisible_tasks_return_404_and_anonymous_requires_login(self):
        self.assertEqual(
            self.client.get(self.task_url(self.private_task), secure=True).status_code, 404
        )
        self.client.logout()
        self.assertEqual(self.client.get(self.task_url(self.first), secure=True).status_code, 302)

    def test_detail_is_read_only_and_rejects_post(self):
        before = list(AIDevTask.objects.values())
        self.assertEqual(self.client.get(self.task_url(self.first), secure=True).status_code, 200)
        self.assertEqual(
            self.client.post(
                self.task_url(self.first), {"title": "changed"}, secure=True
            ).status_code,
            405,
        )
        self.assertEqual(list(AIDevTask.objects.values()), before)

    def test_related_titles_and_document_content_are_escaped(self):
        self.second.title = '<script>alert("title")</script>'
        self.second.save()
        self.first.description = '<img src=x onerror="alert(1)">'
        self.first.save()
        page = self.client.get(self.task_url(self.first), secure=True)
        self.assertContains(page, escape(self.second.title))
        self.assertNotContains(page, 'onerror=')
        self.assertNotContains(page, self.second.title)
        self.assertNotContains(page, self.first.description)

    def test_last_task_has_empty_related_tasks_state(self):
        self.second.delete()
        page = self.client.get(self.task_url(self.first), secure=True)
        self.assertContains(page, "暂无关联开发任务")
