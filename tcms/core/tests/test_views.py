# -*- coding: utf-8 -*-
# pylint: disable=too-many-ancestors
import os
import unittest
from http import HTTPStatus

from django import test
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management import call_command
from django.urls import include, path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from tcms import urls
from tcms.ai_assistant.models import AIJob, AIRequest, AITestCaseDraft
from tcms.tests import LoggedInTestCase
from tcms.tests.factories import (
    TestExecutionFactory,
    TestPlanFactory,
    TestRunFactory,
    UserFactory,
)
from tcms.testruns.models import TestExecutionStatus


class TestDashboard(LoggedInTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # used to reproduce Sentry #KIWI-TCMS-38 where rendering fails
        # with that particular value
        cls.chinese_tp = TestPlanFactory(name="缺货反馈测试需求", author=cls.tester)
        doc_url = (
            "https://kiwitcms.readthedocs.io/en/latest/installing_docker.html"
            "#configuration-of-kiwi-tcms-domain"
        )
        cls.base_url_error_message = format_html(
            _(
                "Base URL is not configured! "
                'See <a href="{doc_url}">documentation</a> and '
                '<a href="{admin_url}">change it</a>'
            ),
            doc_url=doc_url,
            admin_url=reverse("admin:sites_site_change", args=[settings.SITE_ID]),
        )

    def test_when_not_logged_in_redirects_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("core-views-index"))
        self.assertRedirects(
            response,
            reverse("tcms-login") + "?next=/",
            target_status_code=HTTPStatus.OK,
        )

    def test_when_logged_in_renders_dashboard(self):
        response = self.client.get(reverse("core-views-index"))

        self.assertContains(response, "待继续的测试运行")
        self.assertContains(response, "工作台")
        self.assertContains(response, "我的测试计划")

    def test_workbench_ai_data_is_scoped_to_current_user(self):
        own = AIRequest.objects.create(
            created_by=self.tester, title="我的登录需求", requirement="登录"
        )
        other = UserFactory()
        private = AIRequest.objects.create(
            created_by=other, title="别人的私密需求", requirement="不可见"
        )
        for item in (own, private):
            AITestCaseDraft.objects.create(request=item, summary="草稿", case_number="TC-1")
        AIJob.objects.create(
            owner=self.tester, operation="requirement_analysis", status="queued"
        )
        AIJob.objects.create(
            owner=other, operation="test_case_generation", status="failed"
        )
        response = self.client.get(reverse("core-views-index"), secure=True)
        self.assertEqual(response.context["requirement_count"], 1)
        self.assertEqual(response.context["pending_draft_count"], 1)
        self.assertEqual(response.context["active_job_count"], 1)
        self.assertEqual(response.context["failed_job_count"], 0)
        self.assertContains(response, own.title)
        self.assertNotContains(response, private.title)
        self.assertEqual(len(response.context["recent_jobs"]), 1)

    def test_workbench_run_progress_counts_all_assignees(self):
        run = TestRunFactory()
        waiting = TestExecutionStatus.objects.filter(weight=0).first()
        passed = TestExecutionStatus.objects.filter(weight__gt=0).first()
        TestExecutionFactory(run=run, assignee=self.tester, status=waiting)
        TestExecutionFactory(run=run, status=passed)
        response = self.client.get(reverse("core-views-index"), secure=True)
        row = response.context["recent_runs"][0]
        self.assertEqual(row.execution_count, 2)
        self.assertEqual(row.completed_count, 1)
        self.assertEqual(row.completion_percent, 50)

    def test_workbench_empty_run_does_not_divide_by_zero(self):
        TestRunFactory(manager=self.tester)
        response = self.client.get(reverse("core-views-index"), secure=True)
        self.assertEqual(response.context["recent_runs"][0].completion_percent, 0)
        self.assertContains(response, "继续 1 个测试运行")

    def test_dashboard_shows_testruns_for_manager(self):
        test_run = TestRunFactory(manager=self.tester)

        response = self.client.get(reverse("core-views-index"))
        self.assertContains(response, test_run.summary)

    def test_dashboard_shows_testruns_for_default_tester(self):
        test_run = TestRunFactory(default_tester=self.tester)

        response = self.client.get(reverse("core-views-index"))
        self.assertContains(response, test_run.summary)

    def test_dashboard_shows_testruns_for_execution_assignee(self):
        execution = TestExecutionFactory(assignee=self.tester)

        response = self.client.get(reverse("core-views-index"))
        self.assertContains(response, execution.run.summary)

    def test_check_base_url_not_configured(self):
        response = self.client.get("/", follow=True)
        self.assertContains(response, self.base_url_error_message)

    def test_check_base_url_configured(self):
        site = Site.objects.create(domain="example.com", name="example")
        with test.override_settings(SITE_ID=site.pk):
            response = self.client.get("/", follow=True)
            self.assertNotContains(response, self.base_url_error_message)

    def test_check_connection_not_using_ssl(self):
        response = self.client.get("/", follow=True)
        doc_url = (
            "https://kiwitcms.readthedocs.io/en/latest/installing_docker.html"
            "#ssl-configuration"
        )
        ssl_error_message = format_html(
            _(
                "You are not using a secure connection. "
                'See <a href="{doc_url}">documentation</a> and enable SSL.'
            ),
            doc_url=doc_url,
        )
        self.assertContains(response, ssl_error_message)


@unittest.skipUnless(
    os.getenv("TEST_DASHBOARD_CHECK_UNAPPLIED_MIGRATIONS"),
    "Check for missing migrations testing is not enabled",
)
class TestDashboardCheckMigrations(test.TransactionTestCase):
    unapplied_migration_message = _(
        "unapplied migration(s). See "
        '<a href="https://kiwitcms.readthedocs.io/en/latest/'
        "installing_docker.html#initial-configuration-of-running-"
        'container">documentation</a>'
    )

    def test_check_unapplied_migrations(self):
        call_command("migrate", "bugs", "zero", verbosity=2, interactive=False)
        tester = UserFactory()
        tester.set_password("password")
        tester.save()
        self.client.login(  # nosec:B106:hardcoded_password_funcarg
            username=tester.username,
            password="password",
        )
        response = self.client.get("/", follow=True)
        self.assertContains(response, self.unapplied_migration_message)


def exception_view(request):
    raise RuntimeError


urlpatterns = [
    path("will-trigger-500/", exception_view),
    path("", include(urls)),
]


handler500 = "tcms.core.views.server_error"


@test.override_settings(ROOT_URLCONF=__name__)
class TestServerError(test.TestCase):
    def test_custom_server_error_view(self):
        client = test.Client(raise_request_exception=False)
        response = client.get("/will-trigger-500/")

        self.assertEqual(response.status_code, 500)
        self.assertTemplateUsed(response, "500.html")
