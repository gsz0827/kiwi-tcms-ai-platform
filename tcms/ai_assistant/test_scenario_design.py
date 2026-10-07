from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from tcms.testcases.models import TestCase as NativeCase
from . import test_directory_tools
from .scenario_design import CaseDesign, parse_design, serialize_design, inline_test_data

AI_TEXT = """**AI 用例编号：** TC-025
**测试类型：** 异常测试

### 前置条件
- 可模拟弱网
- 测试账号正常

### 测试步骤
1. 点击获取验证码
   - 预期：提示发送失败，请稍后重试
2. 网络恢复后再次获取
   - 预期：正常收到验证码

> 由 AI 测试助手请求 #7 生成，导入前应人工复核。"""


class DesignParserTests(SimpleTestCase):
    def test_imported_ai_text_maps_actions_and_expected_results(self):
        design = parse_design(AI_TEXT)
        self.assertEqual(design.case_number, "TC-025")
        self.assertEqual(design.test_type, "异常测试")
        self.assertEqual(design.preconditions, "可模拟弱网\n测试账号正常")
        self.assertEqual(len(design.steps), 2)
        self.assertEqual(design.steps[0]["action"], "点击获取验证码")
        self.assertEqual(design.steps[0]["expected"], "提示发送失败，请稍后重试")
        self.assertIn("请求 #7", design.extra)

    def test_multiline_data_actions_and_expected_round_trip(self):
        design = CaseDesign(
            case_number="AI-2",
            test_type="功能测试",
            preconditions="账号存在\n### 原文中的符号\n- 嵌套列表",
            test_data="name=demo\npassword=<fake-only>",
            extra="### 自定义内容\n> 不能丢失\n- 原始列表",
            steps=[
                {
                    "action": "输入账号\n1. 保留内层编号",
                    "data": "demo\n0",
                    "expected": "显示结果\n### 保留标题符号",
                }
            ],
        )
        self.assertEqual(parse_design(serialize_design(design)), design)

    def test_unknown_sections_and_free_text_remain_available(self):
        content = "原始说明\n### 自定义标题\n特殊内容 <script>\n### 后置条件\n原始旧内容"
        design = parse_design(content)
        self.assertFalse(design.structured)
        self.assertEqual(design.extra, content)

    def test_missing_expected_not_invented(self):
        design = parse_design("### 测试步骤\n1. 点击按钮")
        self.assertEqual(design.steps[0]["expected"], "")

    def test_repeated_metadata_is_retained(self):
        design = parse_design("**测试类型：** 功能\n**测试类型：** 异常")
        self.assertEqual(design.test_type, "功能")
        self.assertIn("异常", design.extra)


from .edit_test_client import EditClient


class DesignPageTests(TestCase):
    client_class = EditClient
    setUp = test_directory_tools.DirectoryToolsTests.setUp

    def payload(self, **changes):
        return (
            dict(
                summary=self.manual.summary,
                category=self.manual.category_id,
                priority=self.manual.priority_id,
                case_status=self.manual.case_status_id,
                design_mode="structured",
                confirmed="on",
                preconditions="测试账号存在",
                test_type="异常测试",
                test_data="错误密码",
                notes="人工备注",
                requirement="REQ-100",
                case_number="TC-025",
                extra="原始来源说明",
                **{
                    "steps-TOTAL_FORMS": "2",
                    "steps-INITIAL_FORMS": "0",
                    "steps-MIN_NUM_FORMS": "1",
                    "steps-MAX_NUM_FORMS": "100",
                    "steps-0-action": "输入错误密码",
                    "steps-0-data": "wrong-only",
                    "steps-0-expected": "提示密码错误",
                    "steps-1-action": "重新登录",
                    "steps-1-data": "",
                    "steps-1-expected": "进入首页",
                },
            )
            | changes
        )

    def edit(self, data):
        return self.client.post(
            reverse("ai_assistant:scenario_edit", args=[self.manual.pk]), data, secure=True
        )

    def test_detail_and_preview_render_table_not_literal_markdown(self):
        self.manual.text = AI_TEXT + "\n<script>alert('unsafe')</script>"
        self.manual.save()
        original_history = self.manual.history.count()
        for route, args, params in (
            ("ai_assistant:scenario_detail", [self.manual.pk], {}),
            ("ai_assistant:scenario_library", [], {"product": self.product.pk}),
        ):
            page = self.client.get(reverse(route, args=args), params, secure=True)
            self.assertContains(page, 'class="table scenario-steps"')
            self.assertContains(page, "操作步骤")
            self.assertContains(page, "预期结果")
            self.assertNotContains(page, "### 测试步骤")
            self.assertNotContains(page, "**AI 用例编号")
            self.assertNotContains(page, "<script>alert('unsafe')</script>")
            self.assertContains(page, "&lt;script&gt;")
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.history.count(), original_history)
        self.assertEqual(self.manual.text, AI_TEXT + "\n<script>alert('unsafe')</script>")

    def test_structured_edit_saves_native_text_and_metadata(self):
        self.assertEqual(self.edit(self.payload()).status_code, 302)
        self.manual.refresh_from_db()
        design = parse_design(self.manual.text)
        self.assertEqual(len(design.steps), 2)
        self.assertEqual(design.steps[0]["data"], "wrong-only")
        self.assertEqual(design.steps[1]["expected"], "进入首页")
        self.assertEqual(design.extra, "原始来源说明")
        self.assertEqual(self.manual.notes, "人工备注")
        self.assertEqual(self.manual.requirement, "REQ-100")
        self.assertEqual(self.manual.history.first().history_user_id, self.user.pk)

    def test_missing_expected_is_error_and_does_not_save(self):
        before = self.manual.text
        page = self.edit(self.payload(**{"steps-0-expected": ""}))
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["steps"].errors[0]["expected"])
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.text, before)

    def test_missing_confirmation_does_not_save(self):
        data = self.payload()
        data.pop("confirmed")
        page = self.edit(data)
        self.assertEqual(page.status_code, 200)
        self.assertIn("confirmed", page.context["form"].errors)

    def test_formset_management_cannot_be_omitted_or_expanded(self):
        for total in (None, "101"):
            data = self.payload()
            if total is None:
                data.pop("steps-TOTAL_FORMS")
            else:
                data["steps-TOTAL_FORMS"] = total
            page = self.edit(data)
            self.assertEqual(page.status_code, 200)
            self.assertTrue(page.context["steps"].non_form_errors())

    def test_deleted_steps_are_not_saved(self):
        data = self.payload(**{"steps-0-DELETE": "on", "steps-0-action": "", "steps-0-expected": ""})
        self.assertEqual(self.edit(data).status_code, 302)
        self.manual.refresh_from_db()
        self.assertEqual(
            [step["action"] for step in parse_design(self.manual.text).steps], ["重新登录"]
        )

    def test_original_editor_preserves_notes_and_requirement_for_old_clients(self):
        self.manual.notes, self.manual.requirement = "保留备注", "REQ-OLD"
        self.manual.save()
        data = self.payload() | {"text": "完全自由的旧格式文本"}
        for key in ("design_mode", "notes", "requirement"):
            data.pop(key)
        self.assertEqual(self.edit(data).status_code, 302)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.text, "完全自由的旧格式文本")
        self.assertEqual(self.manual.notes, "保留备注")
        self.assertEqual(self.manual.requirement, "REQ-OLD")

    def test_legacy_common_data_is_in_first_step_without_rewriting_on_read(self):
        common = '账号：demo\n<script>alert("test")</script>'
        design = CaseDesign(preconditions="账号已注册", test_data=common, steps=[{"action": "输入验证码", "data": "123456", "expected": "登录成功"}, {"action": "退出", "data": "", "expected": "退出成功"}])
        self.manual.text = serialize_design(design)
        self.manual.save()
        original, history = self.manual.text, self.manual.history.count()
        combined = common + "\n\n123456"
        result = inline_test_data(design)
        self.assertEqual(design.test_data, common)
        self.assertEqual(design.steps[0]["data"], "123456")
        self.assertEqual(result.test_data, "")
        self.assertEqual(result.steps[0]["data"], combined)
        self.assertEqual(inline_test_data(result), result)
        page = self.client.get(reverse("ai_assistant:scenario_edit", args=[self.manual.pk]), secure=True)
        self.assertNotContains(page, 'name="test_data"')
        self.assertEqual(page.context["steps"].forms[0]["data"].value(), combined)
        for route, args, params in [("scenario_detail", [self.manual.pk], {}), ("scenario_library", [], {"product": self.product.pk}), ("scenario_preview", [self.manual.pk], {})]:
            page = self.client.get(reverse("ai_assistant:" + route, args=args), params, secure=True)
            self.assertNotContains(page, "公共测试数据")
            self.assertNotContains(page, "scenario-shared-data")
            self.assertContains(page, "账号：demo")
            self.assertContains(page, "123456")
            self.assertContains(page, "&lt;script&gt;")
            self.assertNotContains(page, '<script>alert("test")</script>')
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.text, original)
        self.assertEqual(self.manual.history.count(), history)

    def test_inline_data_survives_save_and_repeat_edit_without_duplicate_common_section(self):
        combined = "公共账号 demo\n\nwrong-only"
        data = self.payload(**{"steps-0-data": combined})
        self.assertEqual(self.edit(data).status_code, 302)
        self.manual.refresh_from_db()
        design = parse_design(self.manual.text)
        self.assertEqual(design.test_data, "")
        self.assertNotIn("### 测试数据", self.manual.text)
        self.assertEqual(design.steps[0]["data"], combined)
        original = self.manual.text
        data["steps-INITIAL_FORMS"] = "2"
        repeated = self.edit(data)
        self.assertEqual(repeated.status_code, 302, {"form": repeated.context["form"].errors, "steps": repeated.context["steps"].errors} if repeated.status_code == 200 else "")
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.text, original)
        error = self.edit(self.payload(**{"steps-0-data": combined, "steps-0-expected": ""}))
        self.assertEqual(error.status_code, 200)
        self.assertEqual(error.context["steps"].forms[0]["data"].value(), combined)

    def test_common_data_without_steps_is_not_lost_and_raw_mode_is_preserved(self):
        self.manual.text = "### 测试数据\n- 旧账号 demo"
        self.manual.save()
        page = self.client.get(reverse("ai_assistant:scenario_edit", args=[self.manual.pk]), secure=True)
        self.assertEqual(page.context["form"].mode, "raw")
        self.assertEqual(page.context["steps"].forms[0]["data"].value(), "旧账号 demo")
        page = self.client.get(reverse("ai_assistant:scenario_detail", args=[self.manual.pk]), secure=True)
        self.assertContains(page, "旧账号 demo")
        self.assertNotContains(page, "公共测试数据")


    def test_edit_has_standard_fields_without_postcondition_field(self):
        self.manual.text = AI_TEXT
        self.manual.save()
        page = self.client.get(
            reverse("ai_assistant:scenario_edit", args=[self.manual.pk]), secure=True
        )
        self.assertEqual(page.context["form"].mode, "structured")
        for name in ("preconditions", "notes", "requirement", "test_type"):
            self.assertContains(page, f'name="{name}"')
        self.assertNotContains(page, 'name="test_data"')
        self.assertNotContains(page, 'name="postconditions"')
        self.assertNotContains(page, "后置条件")

    def test_creation_time_uses_account_display_timezone(self):
        from datetime import datetime
        from django.test import override_settings
        from tcms.kiwi_auth.models import UserPreference

        NativeCase.objects.filter(pk=self.manual.pk).update(create_date=datetime(2026, 10, 3, 5, 0))
        with override_settings(TIME_ZONE="UTC", USE_TZ=False):
            preference, _ = UserPreference.objects.update_or_create(
                user=self.user, defaults={"time_zone": "Asia/Shanghai"}
            )
            page = self.client.get(
                reverse("ai_assistant:scenario_detail", args=[self.manual.pk]), secure=True
            )
            self.assertContains(page, "2026-10-03 13:00")
            preference.time_zone = "Etc/UTC"
            preference.save()
            page = self.client.get(
                reverse("ai_assistant:scenario_detail", args=[self.manual.pk]), secure=True
            )
            self.assertContains(page, "2026-10-03 05:00")

    def test_structured_creation_uses_existing_permissions(self):
        from django.contrib.auth.models import Permission

        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testcases", codename="add_testcase")
        )
        data = self.payload(summary="表格创建的新用例")
        page = self.client.post(
            reverse("ai_assistant:scenario_new", args=[self.product.pk]), data, secure=True
        )
        self.assertEqual(page.status_code, 302)
        new = NativeCase.objects.get(summary="表格创建的新用例")
        self.assertTrue(self.user.has_perm("testcases.view_testcase", new))
        self.assertEqual(len(parse_design(new.text).steps), 2)
