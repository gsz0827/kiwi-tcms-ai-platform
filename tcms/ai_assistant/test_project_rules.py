"""Security, concurrency, legacy preservation and project navigation regression."""
import importlib
from types import SimpleNamespace
from unittest.mock import patch
from django.apps import apps
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import assign_perm
from tcms.tests.factories import ProductFactory, UserFactory
from tcms.management.models import Version, Build
from . import roles
from .models import AIInstructionProfile, ProjectAIRuleBinding, AIInstructionRevision, AIRequest, AIModelConfig
from .project_rules import RuleForm, can_manage, save_rule, publish_legacy, toggle_rule
from .services import capture_instruction_snapshot
from .jobs import enqueue_ai_job


class ProjectRuleTests(TestCase):
    def setUp(self):
        self.product = ProductFactory(name="测试规则项目")
        self.other_product = ProductFactory(name="独立项目")
        self.owner, self.member, self.outsider = UserFactory(), UserFactory(), UserFactory()
        self.admin = UserFactory(is_staff=True, is_superuser=True)
        for user in (self.owner, self.member):
            roles.add_product_member(user, self.product)
        self.owner.groups.add(Group.objects.get_or_create(name=roles.ROLE_MANAGER)[0])
        self.client.force_login(self.owner)

    def data(self, **changes):
        return {"product": self.product.pk, "name": "项目测试规范", "operation": "all",
            "focus": "覆盖正常、异常、边界和权限隔离场景。", "is_active": True, **changes}

    def create(self):
        form = RuleForm(self.data(), user=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        return save_rule(self.owner, form.cleaned_data)

    def test_manager_without_global_permission_can_manage_own_project_only(self):
        self.assertFalse(self.owner.has_perm("management.change_product"))
        self.assertTrue(can_manage(self.owner, self.product))
        self.assertFalse(can_manage(self.owner, self.other_product))
        self.assertFalse(can_manage(self.member, self.product))

    def test_staff_is_not_automatically_a_project_manager(self):
        self.member.is_staff = True
        self.member.save()
        self.assertFalse(can_manage(self.member, self.product))

    def test_readonly_cannot_edit_even_with_object_permission(self):
        self.member.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        assign_perm("change_product", self.member, self.product)
        self.assertFalse(can_manage(self.member, self.product))

    def test_structured_rule_creates_revision_and_binding(self):
        profile = self.create()
        self.assertEqual(profile.version, 1)
        self.assertIn("## 测试重点", profile.instructions)
        self.assertEqual(profile.revisions.count(), 1)
        self.assertEqual(self.product.ai_rule_binding.profile_id, profile.pk)

    def test_invalid_project_and_empty_rule_rejected(self):
        for data in (self.data(product=self.other_product.pk), self.data(focus=""), self.data(focus="x" * 12001)):
            self.assertFalse(RuleForm(data, user=self.owner).is_valid())

    def test_existing_shared_rule_rejects_second_creation(self):
        self.create()
        self.assertFalse(RuleForm(self.data(), user=self.owner).is_valid())

    def test_edit_versions_include_status_and_preserve_old_snapshot(self):
        profile = self.create()
        old = profile.revisions.get(version=1).snapshot
        form = RuleForm(self.data(name="更新名称", version=1), user=self.owner, profile=profile)
        self.assertTrue(form.is_valid(), form.errors)
        updated = save_rule(self.owner, form.cleaned_data, profile)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.revisions.get(version=1).snapshot, old)
        updated = toggle_rule(self.owner, updated, 2)
        self.assertEqual(updated.version, 3)
        self.assertFalse(updated.is_active)
        self.assertTrue(updated.revisions.get(version=2).snapshot["is_active"])

    def test_stale_edit_and_toggle_rejected(self):
        profile = self.create()
        form = RuleForm(self.data(version=1), user=self.owner, profile=profile)
        self.assertTrue(form.is_valid(), form.errors)
        toggle_rule(self.owner, profile, 1)
        with self.assertRaises(ValueError):
            save_rule(self.owner, form.cleaned_data, profile)
        with self.assertRaises(ValueError):
            toggle_rule(self.owner, profile, 1)
        self.assertEqual(profile.revisions.count(), 2)

    def test_member_reads_shared_but_cannot_save_or_toggle(self):
        profile = self.create()
        self.client.force_login(self.member)
        page = self.client.get(reverse("ai_assistant:edit_instruction_profile", args=[profile.pk]), secure=True)
        self.assertContains(page, "覆盖正常")
        self.assertNotContains(page, '>保存规则</button>')
        for route in ("edit_instruction_profile", "toggle_instruction_profile"):
            self.assertEqual(self.client.post(reverse("ai_assistant:" + route, args=[profile.pk]), self.data(version=1), secure=True).status_code, 403)

    def test_nonmember_cannot_see_shared_rule_or_history(self):
        profile = self.create()
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse("ai_assistant:instruction_profiles"), secure=True), profile.name)
        for route in ("edit_instruction_profile", "instruction_profile_history"):
            self.assertEqual(self.client.get(reverse("ai_assistant:" + route, args=[profile.pk]), secure=True).status_code, 404)

    def test_private_foreign_legacy_not_visible_to_project_manager(self):
        AIInstructionProfile.objects.create(owner=self.member, product=self.product, name="私有旧规范", instructions="私有旧规则内容不能向其他成员泄露。")
        self.assertNotContains(self.client.get(reverse("ai_assistant:instruction_profiles"), secure=True), "私有旧规范")

    def test_publish_legacy_preserves_content_owner_and_version(self):
        profile = AIInstructionProfile.objects.create(owner=self.owner, product=self.product, name="原规范", instructions="原有项目测试规则必须完整保留。", version=7)
        publish_legacy(self.owner, profile)
        profile.refresh_from_db()
        self.assertEqual(profile.version, 7)
        self.assertEqual(profile.instructions, "原有项目测试规则必须完整保留。")
        self.assertEqual(profile.revisions.get(version=7).snapshot["instructions"], profile.instructions)

    def test_cannot_publish_someone_elses_private_rule(self):
        profile = AIInstructionProfile.objects.create(owner=self.member, product=self.product, name="旧规范", instructions="旧账号的内容应受到保护。")
        with self.assertRaises(PermissionDenied):
            publish_legacy(self.owner, profile)

    def test_shared_capture_and_disable_no_fallback(self):
        profile = self.create()
        AIInstructionProfile.objects.create(owner=self.member, product=self.product, name="旧私有规则", instructions="此内容不应覆盖已发布项目规则。")
        category = self.product.category.first()
        captured = capture_instruction_snapshot(self.member, category)
        self.assertEqual(captured["test_case_generation"][0]["id"], profile.pk)
        self.assertEqual(captured["test_case_generation"][0]["scope"], "project")
        self.assertFalse(capture_instruction_snapshot(self.outsider, category)["requirement_analysis"])
        toggle_rule(self.owner, profile, profile.version)
        self.assertFalse(capture_instruction_snapshot(self.member, category)["test_case_generation"])

    def test_task_snapshot_is_frozen_and_retry_keeps_original(self):
        profile = self.create()
        source = AIRequest.objects.create(created_by=self.member, title="登录需求", requirement="可通过账号登录", category=self.product.category.first())
        config = AIModelConfig.objects.create(owner=self.member, name="私有模型", model="test-model", api_base="https://example.invalid/v1", is_active=True)
        job, _ = enqueue_ai_job(self.member, "test_case_generation", {"request_id": source.pk, "additional_instructions": "关注短信过期路径。"}, model_config=config, dedupe_key="freeze")
        frozen = job.payload["rules_snapshot"]
        toggle_rule(self.owner, profile, profile.version)
        job.refresh_from_db()
        self.assertEqual(job.payload["rules_snapshot"], frozen)
        self.assertEqual(frozen["test_case_generation"][-1]["scope"], "task")
        self.assertNotIn("关注短信", AIInstructionProfile.objects.get(pk=profile.pk).instructions)
        retry, _ = enqueue_ai_job(self.member, "test_case_generation", job.payload, model_config=config)
        self.assertEqual(retry.payload["rules_snapshot"], frozen)

    def test_long_extra_rejected_without_job(self):
        from .job_rules import prepare_payload
        source = AIRequest.objects.create(created_by=self.owner, title="需求", requirement="描述", category=self.product.category.first())
        with self.assertRaises(ValueError):
            prepare_payload(self.owner, "requirement_analysis", {"request_id": source.pk, "additional_instructions": "x" * 4001})

    def test_old_and_ambiguous_profiles_are_preserved_by_data_migration(self):
        solo = AIInstructionProfile.objects.create(owner=self.owner, product=self.other_product, name="唯一旧规则", instructions="唯一规则完整保留。", version=4, is_active=False)
        first = AIInstructionProfile.objects.create(owner=self.owner, product=self.product, name="第一份", instructions="第一个账号的规则。")
        second = AIInstructionProfile.objects.create(owner=self.member, product=self.product, name="第二份", instructions="第二个账号的规则。")
        module = importlib.import_module("tcms.ai_assistant.migrations.0040_project_ai_rules")
        module.preserve_and_publish(apps, SimpleNamespace(connection=connection))
        self.assertEqual(ProjectAIRuleBinding.objects.get(product=self.other_product).profile_id, solo.pk)
        self.assertFalse(ProjectAIRuleBinding.objects.filter(product=self.product).exists())
        self.assertEqual(AIInstructionRevision.objects.count(), 3)
        self.assertEqual(AIInstructionProfile.objects.count(), 3)
        for profile in (solo, first, second):
            self.assertEqual(profile.revisions.get(version=profile.version).snapshot["instructions"], profile.instructions)

    def test_rule_pages_render_without_inline_create_form(self):
        profile = self.create()
        for route, args in (("instruction_profiles", []), ("instruction_profile_new", []), ("instruction_profile_history", [profile.pk])):
            self.assertEqual(self.client.get(reverse("ai_assistant:" + route, args=args), secure=True).status_code, 200)
        self.assertEqual(self.client.post(reverse("ai_assistant:instruction_profiles"), self.data(), secure=True).status_code, 405)


class ProjectManagementTests(TestCase):
    setUp = ProjectRuleTests.setUp
    def test_project_list_and_detail_are_project_scoped(self):
        page = self.client.get(reverse("ai_assistant:project_settings"), secure=True)
        self.assertContains(page, self.product.name)
        self.assertNotContains(page, 'href="' + reverse("ai_assistant:project_detail", args=[self.other_product.pk]) + '"')
        self.assertEqual(self.client.get(reverse("ai_assistant:project_detail", args=[self.other_product.pk]), secure=True).status_code, 404)

    def test_versions_builds_and_project_edit(self):
        endpoint = reverse("ai_assistant:project_detail", args=[self.product.pk])
        revision = self.product.history.latest().history_id
        for data in ({"action": "version", "value": "v2"}, {"action": "project", "name": "更新项目", "description": "项目说明", "revision": revision}):
            self.assertEqual(self.client.post(endpoint, data, secure=True).status_code, 302)
        version = Version.objects.get(product=self.product, value="v2")
        self.assertEqual(self.client.post(endpoint, {"action": "build", "version": version.pk, "name": "build-2"}, secure=True).status_code, 302)
        self.assertTrue(Build.objects.filter(version=version, name="build-2").exists())
        self.assertContains(self.client.get(endpoint, secure=True), "build-2")

    def test_stale_project_edit_and_foreign_build_version_rejected(self):
        endpoint = reverse("ai_assistant:project_detail", args=[self.product.pk])
        page = self.client.post(endpoint, {"action": "project", "name": self.product.name, "revision": 0}, secure=True)
        self.assertContains(page, "项目信息已被修改")
        page = self.client.post(endpoint, {"action": "build", "version": self.other_product.version.first().pk, "name": "bad"}, secure=True)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(Build.objects.filter(name="bad").exists())

    def test_member_can_read_but_not_change_project(self):
        self.client.force_login(self.member)
        endpoint = reverse("ai_assistant:project_detail", args=[self.product.pk])
        self.assertEqual(self.client.get(endpoint, secure=True).status_code, 200)
        self.assertEqual(self.client.post(endpoint, {"action": "version", "value": "bad"}, secure=True).status_code, 403)

    def test_project_switch_to_workbench_and_all_projects(self):
        for project in (str(self.product.pk), ""):
            response = self.client.post(reverse("ai_assistant:set_project_context"), {"product": project, "next": "/ai/usage/?q=old"}, secure=True)
            self.assertTrue(response.url.startswith(reverse("core-views-index") + "?"))
            self.assertNotIn("usage", response.url)
            self.assertNotIn("q=", response.url)

    def test_invalid_version_does_not_change_selected_project(self):
        session = self.client.session
        session["ai_product_id"] = self.product.pk
        session.save()
        self.client.post(reverse("ai_assistant:set_project_context"), {"product": self.product.pk, "version": self.other_product.version.first().pk}, secure=True)
        self.assertEqual(self.client.session["ai_product_id"], self.product.pk)
