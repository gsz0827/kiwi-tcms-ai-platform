import json
import urllib.error
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .crypto import decrypt_api_key, encrypt_api_key
from .forms import AIDefectDraftForm, AIModelConfigForm, AITestCaseDraftForm
from .engineering import (
    duplicate_candidates,
    evaluate_release_gate,
    make_chinese_pdf,
    transition_defect,
)
from .models import (
    AIDefectDraft,
    AIDefectStatusHistory,
    AIIterationReport,
    AIJob,
    AIModelConfig,
    AIReleaseGateRule,
    AIRegressionVerification,
    AIRequest,
    AIRequirementVersion,
    AITestCaseDraft,
    AITestCaseReview,
    AITestReport,
    AITestRunAnalysis,
    AIUsageLog,
    ProjectResourceAssignment,
    ProjectResourceFolder,
)
from .jobs import _execute_test_report, enqueue_ai_job, execute_next_job
from .services import (
    AIResponseError,
    _get_config,
    analyze_requirement,
    analyze_test_coverage,
    analyze_test_run,
    apply_test_case_review,
    assign_unique_case_numbers,
    build_test_run_snapshot,
    format_test_case_text,
    generate_defect_draft,
    generate_coverage_gap_test_cases,
    generate_test_report,
    generate_test_cases,
    parse_defect_draft,
    parse_requirement_analysis,
    parse_coverage_analysis,
    parse_test_case_review,
    parse_test_cases,
    parse_test_run_analysis,
    parse_test_report,
    test_model_connection,
    verify_regression,
)
from tcms.core.contrib.linkreference.models import LinkReference
from tcms.management.models import Build, Classification, Priority, Product, Version
from tcms.testcases.models import TestCase as FormalTestCase
from tcms.testcases.models import TestCaseStatus
from tcms.testplans.models import PlanType, TestPlan
from tcms.testruns.models import TestExecution, TestExecutionStatus, TestRun


class ParseTestCasesTests(SimpleTestCase):
    def test_parses_fenced_structured_response(self):
        fence = chr(96) * 3
        content = (
            f"{fence}json\n"
            + json.dumps(
                {
                    "test_cases": [
                        {
                            "case_number": "TC-001",
                            "title": "正确验证码登录成功",
                            "priority": "P1",
                            "test_type": "功能测试",
                            "preconditions": ["用户已注册"],
                            "steps": [
                                {
                                    "action": "输入正确验证码并提交",
                                    "expected": "登录成功",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )
            + f"\n{fence}"
        )
        cases = parse_test_cases(content)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["summary"], "正确验证码登录成功")
        self.assertEqual(cases[0]["priority"], "P1")
        self.assertEqual(cases[0]["steps"][0]["expected"], "登录成功")

    def test_normalizes_unknown_priority(self):
        content = json.dumps(
            {
                "test_cases": [
                    {
                        "title": "边界测试",
                        "priority": "urgent",
                        "steps": ["输入边界值"],
                    }
                ]
            }
        )
        cases = parse_test_cases(content)
        self.assertEqual(cases[0]["case_number"], "TC-001")
        self.assertEqual(cases[0]["priority"], "P3")
        self.assertEqual(cases[0]["steps"][0]["action"], "输入边界值")

    def test_rejects_non_json_response(self):
        with self.assertRaisesMessage(AIResponseError, "合法 JSON"):
            parse_test_cases("这不是 JSON")


class AssignUniqueCaseNumbersTests(SimpleTestCase):
    def test_reassigns_existing_and_duplicate_numbers(self):
        test_cases = [
            {"case_number": "TC-001", "summary": "缺口一"},
            {"case_number": "TC-003", "summary": "缺口二"},
            {"case_number": "tc-003", "summary": "缺口三"},
        ]

        result = assign_unique_case_numbers(test_cases, ["TC-001", "TC-002"])

        self.assertEqual(
            [test_case["case_number"] for test_case in result],
            ["TC-003", "TC-004", "TC-005"],
        )


class ParseRequirementAnalysisTests(SimpleTestCase):
    def test_parses_and_normalizes_requirement_analysis(self):
        result = parse_requirement_analysis(
            json.dumps(
                {
                    "summary": "验证码登录需求",
                    "risk_level": "critical",
                    "functional_points": ["发送验证码", "校验验证码"],
                    "business_rules": "验证码五分钟有效\n连续错误五次锁定",
                    "boundary_conditions": ["手机号长度边界"],
                    "exception_scenarios": ["短信服务超时"],
                    "security_risks": ["验证码暴力破解"],
                    "clarification_questions": ["锁定多久？"],
                    "recommended_test_types": ["功能测试", "安全测试"],
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(result["risk_level"], "medium")
        self.assertEqual(result["functional_points"][0], "发送验证码")
        self.assertEqual(len(result["business_rules"]), 2)

    def test_requires_summary(self):
        with self.assertRaisesMessage(AIResponseError, "summary"):
            parse_requirement_analysis(json.dumps({"risk_level": "low"}))


class ParseCoverageAnalysisTests(SimpleTestCase):
    def test_parses_and_normalizes_coverage_matrix(self):
        result = parse_coverage_analysis(
            json.dumps(
                {
                    "overall_score": 108,
                    "summary": "核心流程已覆盖，异常流程不足",
                    "coverage_items": [
                        {
                            "dimension": "功能点",
                            "item": "验证码登录",
                            "status": "covered",
                            "matched_cases": ["TC-001"],
                            "evidence": "包含成功登录步骤",
                        },
                        {
                            "dimension": "异常场景",
                            "item": "短信服务超时",
                            "status": "unknown",
                            "matched_cases": "TC-003\nTC-004",
                            "gap": "缺少明确超时断言",
                        },
                    ],
                    "missing_coverage": ["无权限访问"],
                    "recommendations": [
                        {
                            "priority": "urgent",
                            "title": "补充权限用例",
                            "reason": "权限风险未覆盖",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(result["overall_score"], 100)
        self.assertEqual(result["coverage_items"][1]["status"], "partial")
        self.assertEqual(
            result["coverage_items"][1]["matched_cases"], ["TC-003", "TC-004"]
        )
        self.assertEqual(result["recommendations"][0]["priority"], "P2")

    def test_requires_coverage_items(self):
        with self.assertRaisesMessage(AIResponseError, "coverage_items"):
            parse_coverage_analysis(
                json.dumps({"overall_score": 60, "summary": "尚未分析"})
            )


class FormatTestCaseTextTests(SimpleTestCase):
    def test_formats_markdown_for_kiwi(self):
        class Draft:
            case_number = "TC-007"
            test_type = "安全测试"
            preconditions = ["用户已登录"]
            steps = [{"action": "访问管理页", "expected": "系统拒绝访问"}]
            request_id = 7

        result = format_test_case_text(Draft())
        self.assertIn("TC-007", result)
        self.assertIn("访问管理页", result)
        self.assertIn("系统拒绝访问", result)
        self.assertIn("请求 #7", result)


class ParseTestCaseReviewTests(SimpleTestCase):
    def test_parses_and_normalizes_review(self):
        result = parse_test_case_review(
            json.dumps(
                {
                    "score": 108,
                    "strengths": ["步骤清晰"],
                    "issues": [
                        {
                            "severity": "urgent",
                            "title": "缺少异常路径",
                            "detail": "未覆盖无权限用户",
                        }
                    ],
                    "missing_scenarios": ["网络超时"],
                    "optimized_summary": "优化后的登录测试",
                    "optimized_preconditions": ["用户已注册"],
                    "optimized_steps": [
                        {"action": "提交登录", "expected": "登录成功"}
                    ],
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(result["score"], 100)
        self.assertEqual(result["issues"][0]["severity"], "medium")
        self.assertEqual(result["optimized_steps"][0]["expected"], "登录成功")

    def test_requires_optimized_steps(self):
        with self.assertRaisesMessage(AIResponseError, "optimized_steps"):
            parse_test_case_review(
                json.dumps({"optimized_summary": "标题", "optimized_steps": []})
            )


class ParseTestRunAnalysisTests(SimpleTestCase):
    def test_parses_and_normalizes_run_analysis(self):
        result = parse_test_run_analysis(
            json.dumps(
                {
                    "executive_summary": "运行存在高风险失败",
                    "risk_level": "critical",
                    "completion_assessment": "已完成 80%",
                    "release_recommendation": "stop",
                    "status_insights": ["失败集中在登录模块"],
                    "failure_clusters": [
                        {
                            "cluster": "登录失败",
                            "affected_cases": ["TC-101"],
                            "evidence": "TC-101 状态为 FAILED",
                            "likely_causes": ["认证服务异常"],
                            "recommended_actions": ["检查认证服务日志"],
                        }
                    ],
                    "blocking_issues": ["P1 登录失败"],
                    "regression_recommendations": [
                        {"priority": "urgent", "scope": "登录回归", "reason": "失败集中"}
                    ],
                    "next_actions": ["修复后重跑"],
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(result["risk_level"], "medium")
        self.assertEqual(result["release_recommendation"], "conditional_go")
        self.assertEqual(result["failure_clusters"][0]["cluster"], "登录失败")
        self.assertEqual(
            result["regression_recommendations"][0]["priority"], "P2"
        )

    def test_requires_executive_summary(self):
        with self.assertRaisesMessage(AIResponseError, "executive_summary"):
            parse_test_run_analysis(json.dumps({"risk_level": "low"}))


class ParseClosedLoopArtifactsTests(SimpleTestCase):
    def test_parses_defect_draft_and_keeps_causes_separate(self):
        result = parse_defect_draft(
            json.dumps(
                {
                    "title": "登录超时后没有错误提示",
                    "severity": "high",
                    "description": "请求超时后页面无反馈",
                    "reproduction_steps": ["提交登录表单"],
                    "actual_result": "页面持续等待",
                    "evidence": ["执行状态为 FAILED"],
                    "likely_causes": ["可能未处理超时异常"],
                }
            )
        )
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["evidence"], ["执行状态为 FAILED"])
        self.assertEqual(result["likely_causes"], ["可能未处理超时异常"])

    def test_report_rejects_invented_defect_url(self):
        result = parse_test_report(
            json.dumps(
                {
                    "title": "登录回归测试报告",
                    "summary": "存在一条失败",
                    "release_decision": "no_go",
                    "defect_summary": [
                        {"title": "真实缺陷", "url": "https://bugs.test/1"},
                        {"title": "虚构缺陷", "url": "https://bugs.test/999"},
                    ],
                }
            ),
            valid_defect_urls=["https://bugs.test/1"],
        )
        self.assertEqual(len(result["defect_summary"]), 1)
        self.assertEqual(result["defect_summary"][0]["url"], "https://bugs.test/1")


class DraftEditFormTests(SimpleTestCase):
    def test_converts_editable_lines_back_to_structured_draft(self):
        form = AITestCaseDraftForm(
            data={
                "case_number": "TC-101",
                "summary": "可编辑草稿",
                "priority": "P2",
                "test_type": "功能测试",
                "preconditions_text": "用户已登录\n账号状态正常",
                "steps_text": "打开页面 => 页面显示正常\n提交表单 → 保存成功",
            },
            instance=AITestCaseDraft(),
        )

        self.assertTrue(form.is_valid(), form.errors)
        draft = form.save(commit=False)
        self.assertEqual(draft.preconditions, ["用户已登录", "账号状态正常"])
        self.assertEqual(
            draft.steps,
            [
                {"action": "打开页面", "expected": "页面显示正常"},
                {"action": "提交表单", "expected": "保存成功"},
            ],
        )


@override_settings(SECRET_KEY="ai-assistant-test-secret")
class PersonalAIModelConfigTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user_one = user_model.objects.create_user(
            username="ai-user-one", email="one@example.test", password="password"
        )
        self.user_two = user_model.objects.create_user(
            username="ai-user-two", email="two@example.test", password="password"
        )

    def _config(self, owner, name, key, active=True):
        return AIModelConfig.objects.create(
            owner=owner,
            name=name,
            api_base="https://api.example.test/v1",
            model=f"{name}-model",
            timeout=300,
            api_key_encrypted=encrypt_api_key(key),
            is_active=active,
        )

    def test_encrypted_key_and_runtime_config_are_isolated_per_user(self):
        config_one = self._config(self.user_one, "one", "secret-one")
        self._config(self.user_two, "two", "secret-two")

        self.assertNotIn("secret-one", config_one.api_key_encrypted)
        self.assertEqual(decrypt_api_key(config_one.api_key_encrypted), "secret-one")
        self.assertEqual(
            _get_config(self.user_one),
            (
                "https://api.example.test/v1",
                "secret-one",
                "one-model",
                300,
            ),
        )

    def test_activating_model_only_deactivates_same_owners_model(self):
        old_config = self._config(self.user_one, "old", "old-key")
        other_users_config = self._config(self.user_two, "other", "other-key")
        new_config = self._config(self.user_one, "new", "new-key")

        old_config.refresh_from_db()
        other_users_config.refresh_from_db()
        self.assertFalse(old_config.is_active)
        self.assertTrue(new_config.is_active)
        self.assertTrue(other_users_config.is_active)

    def test_model_form_requires_key_on_create_and_preserves_it_on_edit(self):
        missing_key_form = AIModelConfigForm(
            data={
                "name": "missing-key",
                "api_base": "https://api.example.test/v1",
                "model": "example-model",
                "timeout": 300,
                "is_active": True,
                "api_key": "",
            },
            owner=self.user_one,
        )
        self.assertFalse(missing_key_form.is_valid())
        self.assertIn("api_key", missing_key_form.errors)

        config = self._config(self.user_one, "editable", "keep-this-key")
        original_ciphertext = config.api_key_encrypted
        edit_form = AIModelConfigForm(
            data={
                "name": "editable",
                "api_base": "https://api.example.test/v2",
                "model": "updated-model",
                "timeout": 240,
                "is_active": True,
                "api_key": "",
            },
            instance=config,
            owner=self.user_one,
        )
        self.assertTrue(edit_form.is_valid(), edit_form.errors)
        saved = edit_form.save()
        self.assertEqual(saved.api_key_encrypted, original_ciphertext)

    def test_user_cannot_view_or_edit_another_users_model(self):
        other_config = self._config(self.user_two, "private-model", "private-key")
        self.client.force_login(self.user_one)

        listing = self.client.get(reverse("ai_assistant:model_settings"), secure=True)
        editing = self.client.get(
            reverse("ai_assistant:edit_model_config", args=[other_config.pk]),
            secure=True,
        )

        self.assertEqual(listing.status_code, 200)
        self.assertNotContains(listing, "private-model")
        self.assertEqual(editing.status_code, 404)

    @patch("tcms.ai_assistant.services._request_config_content")
    def test_connection_test_uses_selected_config(self, request_content):
        config = self._config(self.user_one, "connection-model", "connection-key")
        request_content.return_value = "OK"

        result = test_model_connection(config)

        self.assertEqual(result["reply"], "OK")
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertIs(request_content.call_args.args[0], config)
        self.assertEqual(request_content.call_args.kwargs["max_tokens"], 16)
        self.assertEqual(
            request_content.call_args.kwargs["operation"], "connection_test"
        )

    @patch("tcms.ai_assistant.services.urllib.request.urlopen")
    def test_successful_api_call_records_duration_and_tokens(self, urlopen):
        config = self._config(self.user_one, "logged-model", "logged-key")
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 1,
                    "total_tokens": 9,
                },
            }
        ).encode("utf-8")
        urlopen.return_value.__enter__.return_value = response

        result = test_model_connection(config)

        self.assertEqual(result["reply"], "OK")
        log = AIUsageLog.objects.get(owner=self.user_one)
        self.assertEqual(log.operation, "connection_test")
        self.assertEqual(log.status, "success")
        self.assertEqual(log.config_name, "logged-model")
        self.assertEqual(log.model_name, "logged-model-model")
        self.assertEqual(log.prompt_tokens, 8)
        self.assertEqual(log.completion_tokens, 1)
        self.assertEqual(log.total_tokens, 9)
        self.assertEqual(log.error_message, "")

    @patch("tcms.ai_assistant.services.urllib.request.urlopen")
    def test_failed_api_call_records_safe_error(self, urlopen):
        config = self._config(self.user_one, "offline-model", "offline-key")
        urlopen.side_effect = urllib.error.URLError("network offline")

        with self.assertRaisesMessage(RuntimeError, "无法连接AI接口"):
            test_model_connection(config)

        log = AIUsageLog.objects.get(owner=self.user_one)
        self.assertEqual(log.operation, "connection_test")
        self.assertEqual(log.status, "error")
        self.assertIn("network offline", log.error_message)
        self.assertNotIn("offline-key", log.error_message)

    @patch("tcms.ai_assistant.jobs.test_model_connection")
    def test_connection_view_queues_and_worker_reports_success(self, connection_test):
        config = self._config(self.user_one, "working-model", "working-key")
        connection_test.return_value = {"reply": "OK", "elapsed_ms": 1250}
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse("ai_assistant:test_model_config", args=[config.pk]),
            secure=True,
        )

        job = AIJob.objects.get(owner=self.user_one, operation="connection_test")
        self.assertRedirects(
            response,
            reverse("ai_assistant:job_detail", args=[job.pk]),
            fetch_redirect_response=False,
        )
        self.assertTrue(execute_next_job())
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.result, {"reply": "OK", "elapsed_ms": 1250})

    @patch("tcms.ai_assistant.jobs.test_model_connection")
    def test_connection_view_queues_and_worker_reports_failure(self, connection_test):
        config = self._config(self.user_one, "broken-model", "broken-key")
        connection_test.side_effect = RuntimeError("模型不存在")
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse("ai_assistant:test_model_config", args=[config.pk]),
            secure=True,
        )

        job = AIJob.objects.get(owner=self.user_one, operation="connection_test")
        self.assertRedirects(
            response,
            reverse("ai_assistant:job_detail", args=[job.pk]),
            fetch_redirect_response=False,
        )
        self.assertTrue(execute_next_job())
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("模型不存在", job.error_message)

    def test_user_cannot_test_another_users_model(self):
        other_config = self._config(self.user_two, "private-test", "private-key")
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse("ai_assistant:test_model_config", args=[other_config.pk]),
            secure=True,
        )

        self.assertEqual(response.status_code, 404)

    def test_enqueue_deduplicates_active_jobs_per_account(self):
        config = self._config(self.user_one, "dedupe-model", "dedupe-key")

        first, first_created = enqueue_ai_job(
            self.user_one,
            "connection_test",
            {"model_config_id": config.pk},
            model_config=config,
            dedupe_key=f"connection_test:model:{config.pk}",
        )
        second, second_created = enqueue_ai_job(
            self.user_one,
            "connection_test",
            {"model_config_id": config.pk},
            model_config=config,
            dedupe_key=f"connection_test:model:{config.pk}",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AIJob.objects.count(), 1)

    def test_job_pages_and_status_are_isolated_per_account(self):
        config = self._config(self.user_one, "job-private", "private-key")
        job, _created = enqueue_ai_job(
            self.user_one,
            "connection_test",
            {"model_config_id": config.pk},
            model_config=config,
            dedupe_key="private-job",
        )
        self.client.force_login(self.user_two)

        detail = self.client.get(
            reverse("ai_assistant:job_detail", args=[job.pk]), secure=True
        )
        status = self.client.get(
            reverse("ai_assistant:job_status", args=[job.pk]), secure=True
        )

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(status.status_code, 404)

    def test_owner_can_render_job_center_and_read_uncached_status(self):
        config = self._config(self.user_one, "job-visible", "visible-key")
        job, _created = enqueue_ai_job(
            self.user_one,
            "connection_test",
            {"model_config_id": config.pk},
            model_config=config,
            dedupe_key="visible-job",
        )
        self.client.force_login(self.user_one)

        detail = self.client.get(
            reverse("ai_assistant:job_detail", args=[job.pk]), secure=True
        )
        listing = self.client.get(reverse("ai_assistant:job_list"), secure=True)
        status = self.client.get(
            reverse("ai_assistant:job_status", args=[job.pk]), secure=True
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "真实任务阶段")
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, str(job.pk))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "queued")
        self.assertEqual(status["Cache-Control"], "no-store")

    def test_queued_job_can_be_cancelled_without_worker_execution(self):
        config = self._config(self.user_one, "cancel-model", "cancel-key")
        job, _created = enqueue_ai_job(
            self.user_one,
            "connection_test",
            {"model_config_id": config.pk},
            model_config=config,
            dedupe_key="cancel-job",
        )
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse("ai_assistant:cancel_job", args=[job.pk]), secure=True
        )

        self.assertRedirects(
            response,
            reverse("ai_assistant:job_detail", args=[job.pk]),
            fetch_redirect_response=False,
        )
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.progress, 100)
        self.assertFalse(execute_next_job())

    @patch("tcms.ai_assistant.jobs.test_model_connection")
    def test_worker_records_real_progress_and_result(self, connection_test):
        config = self._config(self.user_one, "worker-model", "worker-key")
        connection_test.return_value = {"reply": "OK", "elapsed_ms": 321}
        job, _created = enqueue_ai_job(
            self.user_one,
            "connection_test",
            {"model_config_id": config.pk},
            model_config=config,
            dedupe_key="worker-job",
        )

        self.assertTrue(execute_next_job())

        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.stage, "任务已完成")
        self.assertEqual(job.result["reply"], "OK")
        self.assertEqual(job.result_url, reverse("ai_assistant:model_settings"))

    @patch("tcms.ai_assistant.jobs.test_model_connection")
    def test_running_job_honors_cancel_request_before_saving_result(
        self, connection_test
    ):
        config = self._config(self.user_one, "running-cancel", "running-key")
        job, _created = enqueue_ai_job(
            self.user_one,
            "connection_test",
            {"model_config_id": config.pk},
            model_config=config,
            dedupe_key="running-cancel-job",
        )

        def request_cancel(_config):
            AIJob.objects.filter(pk=job.pk).update(status="cancel_requested")
            return {"reply": "OK", "elapsed_ms": 100}

        connection_test.side_effect = request_cancel
        self.assertTrue(execute_next_job())

        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.result, {})

    def test_usage_log_page_is_isolated_per_user(self):
        config_one = self._config(self.user_one, "visible-config", "visible-key")
        config_two = self._config(self.user_two, "private-config", "private-key")
        AIUsageLog.objects.create(
            owner=self.user_one,
            model_config=config_one,
            config_name="visible-config",
            model_name="visible-model",
            operation="test_case_generation",
            status="success",
            duration_ms=1200,
            total_tokens=88,
        )
        AIUsageLog.objects.create(
            owner=self.user_two,
            model_config=config_two,
            config_name="private-config",
            model_name="private-model",
            operation="connection_test",
            status="error",
            duration_ms=300,
            error_message="private error",
        )
        self.client.force_login(self.user_one)

        response = self.client.get(
            reverse("ai_assistant:usage_logs"), secure=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "visible-config")
        self.assertContains(response, "生成测试用例")
        self.assertContains(response, "88")
        self.assertNotContains(response, "private-config")
        self.assertNotContains(response, "private error")

    def test_generation_redirects_to_model_setup_when_unconfigured(self):
        self.client.force_login(self.user_one)
        response = self.client.post(reverse("ai_assistant:index"), {}, secure=True)

        self.assertRedirects(
            response,
            reverse("ai_assistant:model_settings"),
            fetch_redirect_response=False,
        )

    @patch("tcms.ai_assistant.services._request_ai_content")
    def test_generation_passes_current_user_to_personal_model_request(self, request):
        request.return_value = (
            json.dumps(
                {
                    "test_cases": [
                        {
                            "title": "账号隔离测试",
                            "steps": [{"action": "生成", "expected": "成功"}],
                        }
                    ]
                }
            ),
            None,
        )

        cases = generate_test_cases("标题", "需求", self.user_one)

        self.assertEqual(cases[0]["summary"], "账号隔离测试")
        self.assertIs(request.call_args.args[0], self.user_one)

    @patch("tcms.ai_assistant.services._request_ai_content")
    def test_analysis_passes_current_user_to_personal_model_request(self, request):
        raw_result = json.dumps(
            {
                "summary": "登录需求",
                "risk_level": "high",
                "functional_points": ["登录"],
            }
        )
        model_config = object()
        request.return_value = (raw_result, model_config)

        analysis, returned_config, returned_raw = analyze_requirement(
            "标题", "需求", self.user_one
        )

        self.assertEqual(analysis["risk_level"], "high")
        self.assertIs(returned_config, model_config)
        self.assertEqual(returned_raw, raw_result)
        self.assertIs(request.call_args.args[0], self.user_one)

    @patch("tcms.ai_assistant.services._request_ai_content")
    def test_coverage_analysis_uses_owned_request_and_current_user(self, request):
        ai_request = AIRequest.objects.create(
            title="覆盖分析",
            requirement="用户通过验证码登录",
            created_by=self.user_one,
        )
        AITestCaseDraft.objects.create(
            request=ai_request,
            case_number="TC-001",
            summary="验证码登录成功",
            priority="P1",
            steps=[{"action": "提交验证码", "expected": "登录成功"}],
        )
        raw_result = json.dumps(
            {
                "overall_score": 80,
                "summary": "成功流程已覆盖",
                "coverage_items": [
                    {
                        "dimension": "功能点",
                        "item": "验证码登录",
                        "status": "covered",
                        "matched_cases": ["TC-001"],
                    }
                ],
            }
        )
        model_config = object()
        request.return_value = (raw_result, model_config)

        result, returned_config, returned_raw = analyze_test_coverage(
            ai_request, self.user_one
        )

        self.assertEqual(result["overall_score"], 80)
        self.assertIs(returned_config, model_config)
        self.assertEqual(returned_raw, raw_result)
        self.assertIs(request.call_args.args[0], self.user_one)
        self.assertIn("TC-001", request.call_args.args[2])

    @patch("tcms.ai_assistant.services._request_ai_content")
    def test_gap_generation_uses_only_coverage_gaps_and_current_user(self, request):
        ai_request = AIRequest.objects.create(
            title="补充覆盖缺口",
            requirement="验证码登录需求",
            created_by=self.user_one,
            coverage_analysis={
                "overall_score": 60,
                "summary": "异常场景不足",
                "coverage_items": [
                    {
                        "dimension": "功能点",
                        "item": "登录成功",
                        "status": "covered",
                    },
                    {
                        "dimension": "异常场景",
                        "item": "短信服务超时",
                        "status": "missing",
                        "gap": "缺少超时验证",
                    },
                ],
                "missing_coverage": ["短信服务超时"],
                "recommendations": [],
            },
        )
        AITestCaseDraft.objects.create(
            request=ai_request,
            case_number="TC-001",
            summary="登录成功",
            steps=[{"action": "登录", "expected": "成功"}],
        )
        request.return_value = (
            json.dumps(
                {
                    "test_cases": [
                        {
                            "case_number": "TC-002",
                            "title": "短信服务超时",
                            "steps": [
                                {"action": "模拟短信超时", "expected": "提示稍后重试"}
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            object(),
        )

        result = generate_coverage_gap_test_cases(ai_request, self.user_one)

        self.assertEqual(result[0]["summary"], "短信服务超时")
        self.assertIs(request.call_args.args[0], self.user_one)
        self.assertIn("短信服务超时", request.call_args.args[2])
        self.assertNotIn('"item": "登录成功"', request.call_args.args[2])

    def test_user_cannot_generate_from_another_users_analysis(self):
        other_request = AIRequest.objects.create(
            title="其他账号需求",
            requirement="私有需求",
            created_by=self.user_two,
            analysis={"summary": "私有分析", "risk_level": "low"},
        )
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse(
                "ai_assistant:generate_from_analysis", args=[other_request.pk]
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 404)

    def test_user_cannot_analyze_another_users_coverage(self):
        other_request = AIRequest.objects.create(
            title="其他账号覆盖分析",
            requirement="私有需求",
            created_by=self.user_two,
        )
        AITestCaseDraft.objects.create(
            request=other_request,
            case_number="TC-PRIVATE",
            summary="私有用例",
            steps=[{"action": "操作", "expected": "结果"}],
        )
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse("ai_assistant:analyze_coverage", args=[other_request.pk]),
            secure=True,
        )

        self.assertEqual(response.status_code, 404)

    def test_user_cannot_supplement_another_users_coverage(self):
        other_request = AIRequest.objects.create(
            title="其他账号缺口",
            requirement="私有需求",
            created_by=self.user_two,
            coverage_analysis={
                "overall_score": 50,
                "summary": "存在缺口",
                "missing_coverage": ["私有缺口"],
            },
        )
        AITestCaseDraft.objects.create(
            request=other_request,
            case_number="TC-PRIVATE",
            summary="私有用例",
            steps=[{"action": "操作", "expected": "结果"}],
        )
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse(
                "ai_assistant:supplement_from_coverage",
                args=[other_request.pk],
            ),
            secure=True,
        )

        self.assertEqual(response.status_code, 404)

    @patch("tcms.ai_assistant.jobs.analyze_test_coverage")
    def test_coverage_view_queues_and_worker_saves_personal_result(self, analyze):
        config = self._config(self.user_one, "coverage-model", "coverage-key")
        ai_request = AIRequest.objects.create(
            title="自己的覆盖分析",
            requirement="登录需求",
            created_by=self.user_one,
        )
        AITestCaseDraft.objects.create(
            request=ai_request,
            case_number="TC-001",
            summary="登录成功",
            steps=[{"action": "登录", "expected": "成功"}],
        )
        result = {
            "overall_score": 75,
            "summary": "仍需补充异常场景",
            "coverage_items": [
                {
                    "dimension": "功能点",
                    "item": "登录",
                    "status": "covered",
                    "matched_cases": ["TC-001"],
                    "evidence": "覆盖成功登录",
                    "gap": "",
                }
            ],
            "missing_coverage": [],
            "recommendations": [],
        }
        analyze.return_value = (result, config, "raw-coverage")
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse("ai_assistant:analyze_coverage", args=[ai_request.pk]),
            secure=True,
        )

        job = AIJob.objects.get(owner=self.user_one, operation="coverage_analysis")
        self.assertRedirects(response, reverse("ai_assistant:job_detail", args=[job.pk]), fetch_redirect_response=False)
        self.assertTrue(execute_next_job())
        ai_request.refresh_from_db()
        self.assertEqual(ai_request.coverage_analysis, result)
        self.assertEqual(ai_request.coverage_model_config, config)
        self.assertEqual(ai_request.coverage_raw, "raw-coverage")
        self.assertIsNotNone(ai_request.coverage_analyzed_at)

    @patch("tcms.ai_assistant.jobs.generate_coverage_gap_test_cases")
    def test_supplement_view_queues_and_worker_adds_unique_drafts(
        self, generate
    ):
        config = self._config(self.user_one, "gap-model", "gap-key")
        ai_request = AIRequest.objects.create(
            title="补充自己的覆盖缺口",
            requirement="登录需求",
            created_by=self.user_one,
            coverage_analysis={
                "overall_score": 65,
                "summary": "缺少异常场景",
                "missing_coverage": ["登录超时"],
                "recommendations": [
                    {"priority": "P1", "title": "补充登录超时", "reason": "未覆盖"}
                ],
            },
            coverage_raw="raw-before-supplement",
            coverage_model_config=config,
        )
        AITestCaseDraft.objects.create(
            request=ai_request,
            case_number="TC-001",
            summary="登录成功",
            steps=[{"action": "登录", "expected": "成功"}],
        )
        generate.return_value = [
            {
                "case_number": "TC-001",
                "summary": "登录超时",
                "priority": "P1",
                "test_type": "异常测试",
                "preconditions": [],
                "steps": [{"action": "模拟超时", "expected": "提示重试"}],
            }
        ]
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse(
                "ai_assistant:supplement_from_coverage", args=[ai_request.pk]
            ),
            secure=True,
        )

        job = AIJob.objects.get(owner=self.user_one, operation="coverage_supplement")
        self.assertRedirects(response, reverse("ai_assistant:job_detail", args=[job.pk]), fetch_redirect_response=False)
        self.assertTrue(execute_next_job())
        self.assertEqual(ai_request.drafts.count(), 2)
        self.assertTrue(
            ai_request.drafts.filter(case_number="TC-002", summary="登录超时").exists()
        )
        ai_request.refresh_from_db()
        self.assertEqual(ai_request.coverage_analysis, {})
        self.assertEqual(ai_request.coverage_raw, "")
        self.assertIsNone(ai_request.coverage_model_config)

    @patch("tcms.ai_assistant.jobs.analyze_requirement")
    def test_index_queues_and_worker_saves_personal_requirement_analysis(self, analyze):
        classification = Classification.objects.create(name="Analysis View")
        product = Product.objects.create(
            name="Analysis View Product", classification=classification
        )
        config = self._config(self.user_one, "analysis-model", "analysis-key")
        analysis = {
            "summary": "登录分析",
            "risk_level": "high",
            "functional_points": ["登录"],
        }
        analyze.return_value = (analysis, config, "raw-analysis")
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse("ai_assistant:index"),
            {
                "category": product.category.get(name="--default--").pk,
                "title": "登录",
                "requirement": "用户通过验证码登录",
                "action": "analyze",
            },
            secure=True,
        )

        job = AIJob.objects.get(owner=self.user_one, operation="requirement_analysis")
        self.assertRedirects(response, reverse("ai_assistant:job_detail", args=[job.pk]), fetch_redirect_response=False)
        self.assertTrue(execute_next_job())
        saved = AIRequest.objects.get(created_by=self.user_one, title="登录")
        self.assertEqual(saved.analysis, analysis)
        self.assertEqual(saved.analysis_model_config, config)
        self.assertEqual(saved.analysis_raw, "raw-analysis")
        self.assertIsNotNone(saved.analyzed_at)

    @patch("tcms.ai_assistant.jobs.generate_test_cases")
    def test_worker_generates_drafts_from_owned_analysis(self, generate):
        self._config(self.user_one, "generation-model", "generation-key")
        ai_request = AIRequest.objects.create(
            title="分析后生成",
            requirement="需求描述",
            created_by=self.user_one,
            analysis={"summary": "分析结果", "risk_level": "medium"},
        )
        generate.return_value = [
            {
                "case_number": "TC-001",
                "summary": "成功场景",
                "priority": "P1",
                "test_type": "功能测试",
                "preconditions": [],
                "steps": [{"action": "提交", "expected": "成功"}],
            }
        ]
        self.client.force_login(self.user_one)

        response = self.client.post(
            reverse(
                "ai_assistant:generate_from_analysis", args=[ai_request.pk]
            ),
            secure=True,
        )

        job = AIJob.objects.get(owner=self.user_one, operation="test_case_generation")
        self.assertRedirects(response, reverse("ai_assistant:job_detail", args=[job.pk]), fetch_redirect_response=False)
        self.assertTrue(execute_next_job())
        self.assertEqual(ai_request.drafts.count(), 1)
        self.assertEqual(ai_request.drafts.get().summary, "成功场景")
        self.assertEqual(generate.call_args.kwargs["analysis"], ai_request.analysis)


@override_settings(SECRET_KEY="ai-run-analysis-test-secret")
class TestRunAnalysisTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="run-analysis-owner", password="password"
        )
        self.other_user = user_model.objects.create_user(
            username="run-analysis-other", password="password"
        )
        view_permission = Permission.objects.get(
            content_type__app_label="testruns", codename="view_testrun"
        )
        self.owner.user_permissions.add(view_permission)
        self.other_user.user_permissions.add(view_permission)

        classification = Classification.objects.create(name="AI Run Analysis")
        product = Product.objects.create(
            name="AI Run Analysis Product", classification=classification
        )
        version = Version.objects.create(value="1.0", product=product)
        build = Build.objects.create(name="AI Run Build", version=version)
        plan_type = PlanType.objects.create(name="AI Run Plan Type")
        plan = TestPlan.objects.create(
            name="AI Run Plan",
            text="回归测试计划",
            product_version=version,
            author=self.owner,
            product=product,
            type=plan_type,
        )
        self.test_run = TestRun.objects.create(
            summary="登录模块回归",
            notes="验证登录模块发布质量",
            plan=plan,
            build=build,
            manager=self.owner,
            default_tester=self.owner,
        )
        passed_status = TestExecutionStatus.objects.create(
            name="AI PASSED", weight=1, icon="fa fa-check", color="#00FF00"
        )
        failed_status = TestExecutionStatus.objects.create(
            name="AI FAILED", weight=-1, icon="fa fa-times", color="#FF0000"
        )
        priority, _created = Priority.objects.get_or_create(value="P1")
        case_status = TestCaseStatus.objects.filter(is_confirmed=True).first()
        if case_status is None:
            case_status = TestCaseStatus.objects.create(
                name="AI CONFIRMED", is_confirmed=True
            )
        category = product.category.get(name="--default--")
        passed_case = FormalTestCase.objects.create(
            summary="正确凭据登录成功",
            requirement="登录需求",
            text="提交正确凭据并验证登录成功",
            author=self.owner,
            default_tester=self.owner,
            reviewer=self.owner,
            priority=priority,
            case_status=case_status,
            category=category,
        )
        failed_case = FormalTestCase.objects.create(
            summary="认证服务超时处理",
            requirement="登录需求",
            text="模拟认证服务超时并检查提示",
            author=self.owner,
            default_tester=self.owner,
            reviewer=self.owner,
            priority=priority,
            case_status=case_status,
            category=category,
        )
        passed_history = passed_case.history.first()
        failed_history = failed_case.history.first()
        TestExecution.objects.create(
            run=self.test_run,
            build=self.test_run.build,
            case=passed_case,
            case_text_version=passed_history.history_id if passed_history else 0,
            status=passed_status,
            assignee=self.owner,
            tested_by=self.owner,
        )
        self.failed_execution = TestExecution.objects.create(
            run=self.test_run,
            build=self.test_run.build,
            case=failed_case,
            case_text_version=failed_history.history_id if failed_history else 0,
            status=failed_status,
            assignee=self.owner,
            tested_by=self.owner,
        )
        LinkReference.objects.create(
            execution=self.failed_execution,
            name="AUTH-101",
            url="https://bugs.example.test/AUTH-101",
            is_defect=True,
        )

    def _config(self, owner, name="run-model"):
        return AIModelConfig.objects.create(
            owner=owner,
            name=name,
            api_base="https://api.example.test/v1",
            model=f"{name}-name",
            timeout=300,
            api_key_encrypted=encrypt_api_key("run-analysis-key"),
            is_active=True,
        )

    def test_snapshot_aggregates_execution_results_and_defects(self):
        snapshot = build_test_run_snapshot(self.test_run)

        self.assertEqual(snapshot["metrics"]["total"], 2)
        self.assertEqual(snapshot["metrics"]["success"], 1)
        self.assertEqual(snapshot["metrics"]["failure"], 1)
        self.assertEqual(snapshot["metrics"]["pending"], 0)
        self.assertEqual(snapshot["metrics"]["defect_links"], 1)
        self.assertEqual(len(snapshot["failure_details"]), 1)
        self.assertEqual(snapshot["failure_details"][0]["defect_count"], 1)
        self.assertEqual(
            snapshot["failure_details"][0]["case_summary"], "认证服务超时处理"
        )

    @patch("tcms.ai_assistant.services._request_ai_content")
    def test_run_analysis_uses_snapshot_and_current_user(self, request_content):
        config = self._config(self.owner)
        raw_result = json.dumps(
            {
                "executive_summary": "一条成功，一条失败",
                "risk_level": "high",
                "completion_assessment": "全部执行完成",
                "release_recommendation": "no_go",
                "failure_clusters": [],
            }
        )
        request_content.return_value = (raw_result, config)

        result, returned_config, returned_raw, snapshot = analyze_test_run(
            self.test_run, self.owner
        )

        self.assertEqual(result["release_recommendation"], "no_go")
        self.assertIs(returned_config, config)
        self.assertEqual(returned_raw, raw_result)
        self.assertEqual(snapshot["metrics"]["failure"], 1)
        self.assertIs(request_content.call_args.args[0], self.owner)
        self.assertEqual(
            request_content.call_args.kwargs["operation"], "test_run_analysis"
        )
        self.assertIn('"defect_links": 1', request_content.call_args.args[2])

    @patch("tcms.ai_assistant.jobs.analyze_test_run")
    def test_view_queues_and_worker_saves_personal_run_analysis(self, analyze):
        config = self._config(self.owner)
        snapshot = build_test_run_snapshot(self.test_run)
        result = {
            "executive_summary": "失败阻塞发布",
            "risk_level": "high",
            "completion_assessment": "已完成",
            "release_recommendation": "no_go",
            "status_insights": [],
            "failure_clusters": [],
            "blocking_issues": ["认证失败"],
            "regression_recommendations": [],
            "next_actions": ["修复后重跑"],
        }
        analyze.return_value = (result, config, "raw-run-analysis", snapshot)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("ai_assistant:run_analysis", args=[self.test_run.pk]),
            secure=True,
        )

        job = AIJob.objects.get(owner=self.owner, operation="test_run_analysis")
        self.assertRedirects(response, reverse("ai_assistant:job_detail", args=[job.pk]), fetch_redirect_response=False)
        self.assertTrue(execute_next_job())
        saved = AITestRunAnalysis.objects.get(owner=self.owner)
        self.assertEqual(saved.test_run, self.test_run)
        self.assertEqual(saved.model_config, config)
        self.assertEqual(saved.result, result)
        self.assertEqual(saved.execution_snapshot["metrics"]["failure"], 1)

    def test_analysis_history_is_isolated_and_run_page_has_entry(self):
        config = self._config(self.owner)
        other_config = self._config(self.other_user, "other-run-model")
        snapshot = build_test_run_snapshot(self.test_run)
        base_result = {
            "risk_level": "medium",
            "completion_assessment": "已完成",
            "release_recommendation": "conditional_go",
        }
        AITestRunAnalysis.objects.create(
            owner=self.owner,
            test_run=self.test_run,
            model_config=config,
            execution_snapshot=snapshot,
            result={**base_result, "executive_summary": "自己的运行分析"},
        )
        AITestRunAnalysis.objects.create(
            owner=self.other_user,
            test_run=self.test_run,
            model_config=other_config,
            execution_snapshot=snapshot,
            result={**base_result, "executive_summary": "其他账号私有分析"},
        )
        self.client.force_login(self.owner)

        analysis_page = self.client.get(
            reverse("ai_assistant:run_analysis", args=[self.test_run.pk]),
            secure=True,
        )
        run_page = self.client.get(
            reverse("testruns-get", args=[self.test_run.pk]), secure=True
        )

        self.assertEqual(analysis_page.status_code, 200)
        self.assertContains(analysis_page, "自己的运行分析")
        self.assertNotContains(analysis_page, "其他账号私有分析")
        self.assertEqual(run_page.status_code, 200)
        self.assertContains(run_page, "AI 运行分析")
        self.assertContains(
            run_page,
            reverse("ai_assistant:run_analysis", args=[self.test_run.pk]),
        )

    @patch("tcms.ai_assistant.services._request_ai_content")
    def test_generates_defect_draft_from_failed_execution(self, request_content):
        config = self._config(self.owner)
        raw = json.dumps(
            {
                "title": "认证服务超时未正确处理",
                "severity": "high",
                "description": "失败执行对应认证超时场景",
                "reproduction_steps": ["模拟认证服务超时", "提交登录"],
                "expected_result": "显示明确超时提示",
                "actual_result": "请测试人员根据真实观察补充",
                "evidence": ["执行状态为 AI FAILED"],
                "likely_causes": ["异常处理可能不完整"],
            }
        )
        request_content.return_value = (raw, config)

        result, returned_config, returned_raw, snapshot = generate_defect_draft(
            self.failed_execution, self.owner
        )

        self.assertEqual(result["severity"], "high")
        self.assertEqual(returned_config, config)
        self.assertEqual(returned_raw, raw)
        self.assertEqual(snapshot["execution"]["status_weight"], -1)
        self.assertEqual(
            request_content.call_args.kwargs["operation"],
            "defect_draft_generation",
        )

    @patch("tcms.ai_assistant.services._request_ai_content")
    def test_generates_report_with_real_defect_links_only(self, request_content):
        config = self._config(self.owner)
        raw = json.dumps(
            {
                "title": "登录模块回归测试报告",
                "summary": "两条执行中一条失败",
                "scope": "登录模块",
                "conclusion": "认证超时场景阻塞发布",
                "release_decision": "no_go",
                "defect_summary": [
                    {
                        "execution_id": self.failed_execution.pk,
                        "case_number": f"TC-{self.failed_execution.case_id}",
                        "title": "AUTH-101",
                        "url": "https://bugs.example.test/AUTH-101",
                    }
                ],
                "recommendations": ["修复后执行回归"],
            }
        )
        request_content.return_value = (raw, config)

        result, returned_config, _raw, snapshot = generate_test_report(
            self.test_run, self.owner
        )

        self.assertEqual(result["release_decision"], "no_go")
        self.assertEqual(len(result["defect_summary"]), 1)
        self.assertEqual(returned_config, config)
        self.assertEqual(snapshot["metrics"]["failure"], 1)
        self.assertEqual(
            request_content.call_args.kwargs["operation"],
            "test_report_generation",
        )

    def test_draft_link_requires_confirmation_and_creates_native_reference(self):
        change_permission = Permission.objects.get(
            content_type__app_label="testruns", codename="change_testrun"
        )
        self.owner.user_permissions.add(change_permission)
        draft = AIDefectDraft.objects.create(
            owner=self.owner,
            execution=self.failed_execution,
            title="认证超时",
            actual_result="实际显示为空白页",
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("ai_assistant:link_defect_draft", args=[draft.pk]),
            {"name": "AUTH-202", "url": "https://bugs.example.test/AUTH-202"},
            secure=True,
        )

        self.assertRedirects(
            response,
            reverse("ai_assistant:edit_defect_draft", args=[draft.pk]),
            fetch_redirect_response=False,
        )
        draft.refresh_from_db()
        self.assertIsNotNone(draft.linked_reference)
        self.assertTrue(draft.linked_reference.is_defect)
        self.assertEqual(draft.linked_reference.execution, self.failed_execution)

    def test_regression_verification_compares_original_failures(self):
        snapshot = build_test_run_snapshot(self.test_run)
        report = AITestReport.objects.create(
            owner=self.owner,
            test_run=self.test_run,
            title="登录报告",
            summary="存在失败",
            metrics_snapshot=snapshot,
        )
        regression_run = TestRun.objects.create(
            summary="登录修复后回归",
            notes="",
            plan=self.test_run.plan,
            build=self.test_run.build,
            manager=self.owner,
            default_tester=self.owner,
        )
        passed_status = TestExecutionStatus.objects.filter(weight__gt=0).first()
        TestExecution.objects.create(
            run=regression_run,
            build=regression_run.build,
            case=self.failed_execution.case,
            case_text_version=self.failed_execution.case_text_version,
            status=passed_status,
            assignee=self.owner,
            tested_by=self.owner,
        )

        status, result = verify_regression(report, regression_run)

        self.assertEqual(status, "passed")
        self.assertEqual(result["counts"]["passed"], 1)
        self.assertEqual(result["counts"]["failed"], 0)
        self.assertEqual(result["items"][0]["outcome"], "passed")

    def test_closed_loop_pages_render_and_keep_account_data_isolated(self):
        snapshot = build_test_run_snapshot(self.test_run)
        own_draft = AIDefectDraft.objects.create(
            owner=self.owner,
            execution=self.failed_execution,
            title="自己的缺陷草稿",
            actual_result="真实失败现象",
        )
        AIDefectDraft.objects.create(
            owner=self.other_user,
            execution=self.failed_execution,
            title="其他账号的缺陷草稿",
        )
        own_report = AITestReport.objects.create(
            owner=self.owner,
            test_run=self.test_run,
            title="自己的测试报告",
            summary="存在一条失败",
            metrics_snapshot=snapshot,
        )
        AITestReport.objects.create(
            owner=self.other_user,
            test_run=self.test_run,
            title="其他账号的测试报告",
            summary="私有内容",
            metrics_snapshot=snapshot,
        )
        self.client.force_login(self.owner)

        defect_page = self.client.get(
            reverse(
                "ai_assistant:execution_defect", args=[self.failed_execution.pk]
            ),
            secure=True,
        )
        defect_edit_page = self.client.get(
            reverse("ai_assistant:edit_defect_draft", args=[own_draft.pk]),
            secure=True,
        )
        report_page = self.client.get(
            reverse("ai_assistant:run_report", args=[self.test_run.pk]),
            secure=True,
        )
        report_edit_page = self.client.get(
            reverse("ai_assistant:edit_report", args=[own_report.pk]),
            secure=True,
        )

        for response in (
            defect_page,
            defect_edit_page,
            report_page,
            report_edit_page,
        ):
            self.assertEqual(response.status_code, 200)
        self.assertContains(defect_page, "自己的缺陷草稿")
        self.assertNotContains(defect_page, "其他账号的缺陷草稿")
        self.assertContains(report_page, "自己的测试报告")
        self.assertNotContains(report_page, "其他账号的测试报告")
        self.assertContains(report_edit_page, "修复后回归验证")

    def test_quality_dashboard_renders_closed_loop_and_is_account_isolated(self):
        own_request = AIRequest.objects.create(
            created_by=self.owner,
            title="自己的闭环需求",
            requirement="登录回归需求",
        )
        AIRequest.objects.create(
            created_by=self.other_user,
            title="其他账号的私有需求",
            requirement="不应显示",
        )
        snapshot = build_test_run_snapshot(self.test_run)
        AITestRunAnalysis.objects.create(
            owner=self.owner,
            test_run=self.test_run,
            execution_snapshot=snapshot,
            result={
                "executive_summary": "自己的运行分析",
                "risk_level": "high",
                "release_recommendation": "no_go",
            },
        )
        AITestRunAnalysis.objects.create(
            owner=self.other_user,
            test_run=self.test_run,
            execution_snapshot=snapshot,
            result={"executive_summary": "其他账号的运行分析"},
        )
        own_report = AITestReport.objects.create(
            owner=self.owner,
            test_run=self.test_run,
            title="自己的看板报告",
            summary="存在失败",
            metrics_snapshot=snapshot,
        )
        AITestReport.objects.create(
            owner=self.other_user,
            test_run=self.test_run,
            title="其他账号的看板报告",
            summary="不应显示",
            metrics_snapshot=snapshot,
        )
        self.client.force_login(self.owner)

        dashboard_page = self.client.get(
            reverse("ai_assistant:dashboard"), secure=True
        )
        index_page = self.client.get(reverse("ai_assistant:index"), secure=True)

        self.assertEqual(dashboard_page.status_code, 200)
        self.assertContains(dashboard_page, own_request.title)
        self.assertContains(dashboard_page, own_report.title)
        self.assertNotContains(dashboard_page, "其他账号的私有需求")
        self.assertNotContains(dashboard_page, "其他账号的看板报告")
        self.assertContains(dashboard_page, "待办事项")
        self.assertContains(index_page, "需求分析")
        self.assertContains(index_page, "执行任务")
        self.assertContains(index_page, reverse("testcases-search"))
        self.assertContains(index_page, reverse("testruns-search"))
        self.assertContains(index_page, reverse("ai_assistant:model_settings"))

    def test_project_and_version_context_are_saved_for_dashboard(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("ai_assistant:set_project_context"),
            {
                "product": self.test_run.plan.product_id,
                "version": self.test_run.build.version_id,
            },
            secure=True,
        )
        self.assertRedirects(
            response,
            reverse("ai_assistant:dashboard"),
            fetch_redirect_response=False,
        )
        session = self.client.session
        self.assertEqual(session["ai_product_id"], self.test_run.plan.product_id)
        self.assertEqual(session["ai_version_id"], self.test_run.build.version_id)
        dashboard = self.client.get(reverse("ai_assistant:dashboard"), secure=True)
        self.assertContains(dashboard, self.test_run.plan.product.name)
        self.assertContains(dashboard, "当前项目与版本")

    def test_resource_tree_browsers_render_details_and_keep_requirements_private(self):
        self.owner.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="testcases", codename="view_testcase"
            ),
            Permission.objects.get(
                content_type__app_label="testplans", codename="view_testplan"
            ),
        )
        own_request = AIRequest.objects.create(
            created_by=self.owner,
            category=self.test_run.plan.product.category.get(name="--default--"),
            title="自己的资源目录需求",
            requirement="只有当前账号能看到的需求描述",
            analysis={"summary": "自己的分析摘要", "risk_level": "high"},
        )
        AIRequest.objects.create(
            created_by=self.other_user,
            title="其他账号的资源目录需求",
            requirement="不应显示在当前账号目录中",
        )
        self.client.force_login(self.owner)

        requirement_page = self.client.get(
            reverse("ai_assistant:index"), secure=True
        )
        case_page = self.client.get(reverse("testcases-search"), secure=True)
        plan_page = self.client.get(reverse("plans-search"), secure=True)

        for response in (requirement_page, case_page, plan_page):
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "输入编号或名称筛选")
            self.assertContains(response, "kiwi-resource-modal")
            self.assertContains(response, "kiwi-resource-workspace")
            self.assertContains(response, "kiwi-resource-pane")
            self.assertContains(response, "kiwi-resource-content")
        self.assertContains(requirement_page, own_request.title)
        self.assertContains(requirement_page, "自己的分析摘要")
        self.assertContains(requirement_page, "进入完整追踪页面")
        self.assertNotContains(requirement_page, "其他账号的资源目录需求")
        self.assertContains(case_page, self.failed_execution.case.summary)
        self.assertContains(case_page, "进入完整用例页面")
        self.assertContains(plan_page, self.test_run.plan.name)
        self.assertContains(plan_page, "进入完整计划页面")

    def test_project_resource_folders_are_shared_and_permission_protected(self):
        product = self.test_run.plan.product
        own_request = AIRequest.objects.create(
            created_by=self.owner,
            category=product.category.get(name="--default--"),
            title="共享目录中的私有需求",
            requirement="需求内容仍然只属于创建账号",
        )
        self.client.force_login(self.owner)

        create_response = self.client.post(
            reverse("ai_assistant:create_resource_folder"),
            {
                "resource_type": "requirement",
                "product": product.pk,
                "name": "登录模块",
                "next": reverse("ai_assistant:index"),
            },
            secure=True,
        )
        self.assertRedirects(
            create_response,
            reverse("ai_assistant:index"),
            fetch_redirect_response=False,
        )
        parent = ProjectResourceFolder.objects.get(
            product=product, resource_type="requirement", name="登录模块"
        )
        self.client.post(
            reverse("ai_assistant:create_resource_folder"),
            {
                "resource_type": "requirement",
                "product": product.pk,
                "parent": parent.pk,
                "name": "验证码登录",
                "next": reverse("ai_assistant:index"),
            },
            secure=True,
        )
        child = ProjectResourceFolder.objects.get(parent=parent)
        self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            {
                "resource_type": "requirement",
                "object_id": own_request.pk,
                "folder": child.pk,
                "next": reverse("ai_assistant:index"),
            },
            secure=True,
        )
        self.assertTrue(
            ProjectResourceAssignment.objects.filter(
                folder=child,
                resource_type="requirement",
                object_id=own_request.pk,
            ).exists()
        )
        owner_page = self.client.get(reverse("ai_assistant:index"), secure=True)
        self.assertContains(owner_page, "登录模块")
        self.assertContains(owner_page, "验证码登录")
        self.assertContains(owner_page, own_request.title)
        self.assertContains(owner_page, "管理共享目录")

        self.client.force_login(self.other_user)
        member_page = self.client.get(reverse("ai_assistant:index"), secure=True)
        self.assertContains(member_page, "登录模块")
        self.assertContains(member_page, "验证码登录")
        self.assertNotContains(member_page, own_request.title)
        rename_response = self.client.post(
            reverse("ai_assistant:rename_resource_folder", args=[child.pk]),
            {"name": "短信验证码", "next": reverse("ai_assistant:index")},
            secure=True,
        )
        self.assertEqual(rename_response.status_code, 302)
        child.refresh_from_db()
        self.assertEqual(child.name, "短信验证码")

        change_case = Permission.objects.get(
            content_type__app_label="testcases", codename="change_testcase"
        )
        view_case = Permission.objects.get(
            content_type__app_label="testcases", codename="view_testcase"
        )
        self.owner.user_permissions.add(change_case, view_case)
        self.client.force_login(self.owner)
        self.client.post(
            reverse("ai_assistant:create_resource_folder"),
            {
                "resource_type": "case",
                "product": product.pk,
                "name": "核心用例",
                "next": reverse("testcases-search"),
            },
            secure=True,
        )
        case_folder = ProjectResourceFolder.objects.get(
            product=product, resource_type="case", name="核心用例"
        )
        self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            {
                "resource_type": "case",
                "object_id": self.failed_execution.case_id,
                "folder": case_folder.pk,
                "next": reverse("testcases-search"),
            },
            secure=True,
        )
        case_page = self.client.get(reverse("testcases-search"), secure=True)
        self.assertContains(case_page, "核心用例")
        self.assertContains(case_page, self.failed_execution.case.summary)

        change_plan = Permission.objects.get(
            content_type__app_label="testplans", codename="change_testplan"
        )
        view_plan = Permission.objects.get(
            content_type__app_label="testplans", codename="view_testplan"
        )
        self.owner.user_permissions.add(change_plan, view_plan)
        self.client.post(
            reverse("ai_assistant:create_resource_folder"),
            {
                "resource_type": "plan",
                "product": product.pk,
                "name": "回归计划",
                "next": reverse("plans-search"),
            },
            secure=True,
        )
        plan_folder = ProjectResourceFolder.objects.get(
            product=product, resource_type="plan", name="回归计划"
        )
        self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            {
                "resource_type": "plan",
                "object_id": self.test_run.plan_id,
                "folder": plan_folder.pk,
                "next": reverse("plans-search"),
            },
            secure=True,
        )
        plan_page = self.client.get(reverse("plans-search"), secure=True)
        self.assertContains(plan_page, "回归计划")
        self.assertContains(plan_page, self.test_run.plan.name)

        self.client.force_login(self.other_user)
        forbidden = self.client.post(
            reverse("ai_assistant:rename_resource_folder", args=[case_folder.pk]),
            {"name": "越权修改", "next": reverse("testcases-search")},
            secure=True,
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.owner)
        self.client.post(
            reverse("ai_assistant:delete_resource_folder", args=[case_folder.pk]),
            {"next": reverse("testcases-search")},
            secure=True,
        )
        self.assertFalse(
            ProjectResourceAssignment.objects.filter(
                resource_type="case", object_id=self.failed_execution.case_id
            ).exists()
        )
        self.assertTrue(
            FormalTestCase.objects.filter(pk=self.failed_execution.case_id).exists()
        )

    def test_defect_lifecycle_records_history_and_requires_close_reason(self):
        draft = AIDefectDraft.objects.create(
            owner=self.owner,
            execution=self.failed_execution,
            title="认证服务超时",
            actual_result="请求超时",
        )
        self.assertTrue(
            transition_defect(draft, "in_progress", user=self.owner, reason="已受理")
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, "in_progress")
        self.assertEqual(AIDefectStatusHistory.objects.get(defect=draft).reason, "已受理")
        with self.assertRaisesMessage(ValueError, "关闭原因"):
            transition_defect(draft, "closed", user=self.owner)

    def test_duplicate_defect_candidates_are_account_isolated(self):
        first = AIDefectDraft.objects.create(
            owner=self.owner,
            execution=self.failed_execution,
            title="认证服务超时导致空白页",
            actual_result="登录页面显示空白",
        )
        second = AIDefectDraft.objects.create(
            owner=self.owner,
            execution=self.failed_execution,
            title="认证服务超时导致空白页面",
            actual_result="登录页面显示空白",
        )
        AIDefectDraft.objects.create(
            owner=self.other_user,
            execution=self.failed_execution,
            title=first.title,
            actual_result=first.actual_result,
        )
        matches = duplicate_candidates(second)
        self.assertEqual([item[1].pk for item in matches], [first.pk])

    def test_release_gate_blocks_open_p1_defect(self):
        AIDefectDraft.objects.create(
            owner=self.owner,
            execution=self.failed_execution,
            title="阻断缺陷",
            priority="P1",
            status="in_progress",
        )
        AIReleaseGateRule.objects.create(
            owner=self.owner,
            product=self.test_run.plan.product,
            min_success_rate=0,
            require_all_executed=False,
            max_open_defects=10,
        )
        result = evaluate_release_gate(
            self.owner,
            self.test_run.plan.product,
            build_test_run_snapshot(self.test_run),
            [self.test_run.pk],
        )
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"][0]["passed"])

    def test_requirement_change_versions_and_marks_cases_for_update(self):
        ai_request = AIRequest.objects.create(
            created_by=self.owner,
            category=self.test_run.plan.product.category.get(name="--default--"),
            title="登录需求",
            requirement="旧规则",
        )
        AIRequirementVersion.objects.create(
            request=ai_request,
            version=1,
            title=ai_request.title,
            requirement=ai_request.requirement,
            changed_by=self.owner,
        )
        draft = AITestCaseDraft.objects.create(
            request=ai_request,
            case_number="TC-001",
            summary="登录成功",
            steps=[{"action": "登录", "expected": "成功"}],
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("ai_assistant:edit_requirement", args=[ai_request.pk]),
            {"title": "登录需求 V2", "requirement": "新规则", "change_summary": "增加锁定规则"},
            secure=True,
        )
        self.assertRedirects(
            response,
            reverse("ai_assistant:requirement_trace", args=[ai_request.pk]),
            fetch_redirect_response=False,
        )
        ai_request.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(ai_request.version, 2)
        self.assertTrue(ai_request.needs_case_review)
        self.assertTrue(draft.needs_update)
        self.assertEqual(ai_request.versions.count(), 2)

    def test_defect_regression_failure_reopens_defect(self):
        draft = AIDefectDraft.objects.create(
            owner=self.owner,
            execution=self.failed_execution,
            title="认证失败",
            status="fixed",
        )
        regression_run = TestRun.objects.create(
            summary="缺陷回归",
            plan=self.test_run.plan,
            build=self.test_run.build,
            manager=self.owner,
        )
        TestExecution.objects.create(
            run=regression_run,
            build=regression_run.build,
            case=self.failed_execution.case,
            case_text_version=self.failed_execution.case_text_version,
            status=self.failed_execution.status,
            assignee=self.owner,
            tested_by=self.owner,
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("ai_assistant:create_defect_regression", args=[draft.pk]),
            {"regression_run_id": regression_run.pk, "notes": "仍可复现"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "in_progress")
        self.assertEqual(draft.regression_verifications.get().status, "failed")

    def test_report_approval_exports_and_iteration_snapshot(self):
        snapshot = build_test_run_snapshot(self.test_run)
        report = AITestReport.objects.create(
            owner=self.owner,
            test_run=self.test_run,
            title="登录测试报告",
            summary="一项失败",
            metrics_snapshot=snapshot,
            snapshot_hash="abc123",
        )
        self.client.force_login(self.owner)
        approval = self.client.post(
            reverse("ai_assistant:approve_report", args=[report.pk]),
            {"decision": "approved", "comment": "确认发布结论"},
            secure=True,
        )
        self.assertEqual(approval.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.approval_status, "approved")
        self.assertIn(self.owner.username, report.signature)
        html = self.client.get(reverse("ai_assistant:export_report_html", args=[report.pk]), secure=True)
        pdf = self.client.get(reverse("ai_assistant:export_report_pdf", args=[report.pk]), secure=True)
        self.assertContains(html, "登录测试报告")
        self.assertTrue(pdf.content.startswith(b"%PDF-1.4"))
        iteration = self.client.post(
            reverse("ai_assistant:iteration_reports"),
            {
                "title": "1.0 迭代报告",
                "product": self.test_run.plan.product_id,
                "version": self.test_run.build.version_id,
                "run_ids": str(self.test_run.pk),
                "conclusion": "等待修复",
            },
            secure=True,
        )
        self.assertEqual(iteration.status_code, 302)
        saved = AIIterationReport.objects.get(owner=self.owner)
        self.assertEqual(saved.metrics_snapshot["metrics"]["total"], 2)
        self.assertTrue(saved.snapshot_hash)

    def test_report_edit_creates_revision_and_resets_signature(self):
        report = AITestReport.objects.create(
            owner=self.owner,
            test_run=self.test_run,
            title="签字前报告",
            summary="原摘要",
            metrics_snapshot=build_test_run_snapshot(self.test_run),
            snapshot_hash="snapshot-hash",
            approval_status="approved",
            approved_by=self.owner,
            signature="已签字",
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("ai_assistant:edit_report", args=[report.pk]),
            {
                "title": "修订后报告",
                "summary": "新摘要",
                "scope": "登录模块",
                "conclusion": "继续修复",
                "release_decision": "no_go",
                "recommendations_text": "修复阻断问题",
                "change_reason": "补充回归结论",
            },
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.title, "修订后报告")
        self.assertEqual(report.approval_status, "pending")
        self.assertEqual(report.signature, "")
        self.assertEqual(report.revisions.get().change_reason, "补充回归结论")

    @patch("tcms.ai_assistant.jobs.generate_test_report")
    def test_report_job_creates_immutable_incrementing_versions(self, generate):
        config = self._config(self.owner, "report-version-model")
        snapshot = build_test_run_snapshot(self.test_run)
        result = {
            "title": "登录测试报告",
            "summary": "存在失败",
            "scope": "登录模块",
            "conclusion": "暂缓发布",
            "release_decision": "no_go",
            "defect_summary": [],
            "recommendations": ["修复后回归"],
        }
        generate.return_value = (result, config, "raw", snapshot)
        first_job = AIJob.objects.create(
            owner=self.owner,
            model_config=config,
            operation="test_report_generation",
            payload={"test_run_id": self.test_run.pk},
        )
        second_job = AIJob.objects.create(
            owner=self.owner,
            model_config=config,
            operation="test_report_generation",
            payload={"test_run_id": self.test_run.pk},
        )
        _execute_test_report(first_job)
        _execute_test_report(second_job)
        reports = list(
            AITestReport.objects.filter(owner=self.owner, test_run=self.test_run).order_by("version")
        )
        self.assertEqual([item.version for item in reports], [1, 2])
        self.assertFalse(reports[0].is_current)
        self.assertIsNone(reports[0].current_marker)
        self.assertTrue(reports[1].is_current)
        self.assertTrue(reports[1].current_marker)
        self.assertEqual(reports[0].metrics_snapshot, snapshot)


class EngineeringPdfTests(SimpleTestCase):
    def test_dependency_free_pdf_contains_multiple_chinese_lines(self):
        content = make_chinese_pdf(["测试报告", "执行成功率 98%"])
        self.assertTrue(content.startswith(b"%PDF-1.4"))
        self.assertIn(b"/UniGB-UCS2-H", content)


class ApplyTestCaseReviewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="review-owner", password="password"
        )
        self.other_user = user_model.objects.create_user(
            username="review-other", password="password"
        )
        classification = Classification.objects.create(name="AI Review")
        product = Product.objects.create(
            name="AI Review Product", classification=classification
        )
        priority, _created = Priority.objects.get_or_create(value="P3")
        status = TestCaseStatus.objects.create(name="Proposed", is_confirmed=False)
        self.test_case = FormalTestCase.objects.create(
            summary="原始标题",
            requirement="登录需求",
            text="原始正文",
            case_status=status,
            category=product.category.get(name="--default--"),
            priority=priority,
            author=self.owner,
        )
        self.review = AITestCaseReview.objects.create(
            owner=self.owner,
            test_case=self.test_case,
            score=88,
            original_summary=self.test_case.summary,
            original_text=self.test_case.text,
            optimized_summary="优化标题",
            optimized_preconditions=["用户已注册"],
            optimized_steps=[
                {"action": "输入正确凭据", "expected": "登录成功"}
            ],
        )

    def test_applies_review_once_and_preserves_original_snapshot(self):
        test_case, applied = apply_test_case_review(self.review, self.owner)

        self.assertTrue(applied)
        self.assertEqual(test_case.summary, "优化标题")
        self.assertIn("输入正确凭据", test_case.text)
        self.assertEqual(test_case.reviewer, self.owner)

        self.review.refresh_from_db()
        self.assertEqual(self.review.original_summary, "原始标题")
        self.assertEqual(self.review.original_text, "原始正文")
        self.assertIsNotNone(self.review.applied_at)

        _test_case, applied_again = apply_test_case_review(self.review, self.owner)
        self.assertFalse(applied_again)

    def test_other_account_cannot_apply_review(self):
        with self.assertRaises(AITestCaseReview.DoesNotExist):
            apply_test_case_review(self.review, self.other_user)
    generate_defect_draft,
    generate_test_report,
    parse_defect_draft,
    parse_test_report,
