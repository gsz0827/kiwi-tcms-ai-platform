from django.conf import settings
from django.db import models


class AllureReport(models.Model):
    STATES = [
        ("queued", "等待生成"),
        ("generating", "生成中"),
        ("ready", "已生成"),
        ("error", "生成失败"),
    ]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    web_run = models.OneToOneField(
        "web_testing.WebRun",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="allure_report",
    )
    api_run = models.OneToOneField(
        "ai_assistant.APIRun",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="allure_report",
    )
    status = models.CharField(max_length=16, choices=STATES, default="queued", db_index=True)
    artifact = models.BinaryField(null=True, editable=False)
    checksum = models.CharField(max_length=64, blank=True)
    engine_version = models.CharField(max_length=32, blank=True)
    summary = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    heartbeat = models.DateTimeField(null=True)
    created = models.DateTimeField(auto_now_add=True)
    generated = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(web_run__isnull=False, api_run__isnull=True)
                    | models.Q(web_run__isnull=True, api_run__isnull=False)
                ),
                name="allure_exactly_one_source",
            )
        ]
        indexes = [models.Index(fields=("status", "created"), name="allure_report_queue")]

    @property
    def kind(self):
        return "web" if self.web_run_id else "api"

    @property
    def source(self):
        return self.web_run if self.web_run_id else self.api_run
