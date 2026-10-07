from django.apps import AppConfig


class AllureReportingConfig(AppConfig):
    name = "tcms.allure_reporting"
    verbose_name = "Allure 执行报告"

    def ready(self):
        from . import signals  # noqa: F401
