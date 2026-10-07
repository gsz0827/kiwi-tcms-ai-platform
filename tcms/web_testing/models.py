import uuid

from django.conf import settings
from django.db import models


class WebCase(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey("management.Product", on_delete=models.PROTECT)
    name = models.CharField("用例名称", max_length=200)
    folder = models.CharField("目录", max_length=200, blank=True, help_text="例如：登录/异常场景")
    description = models.TextField("前置条件与说明", blank=True)
    steps_encrypted = models.TextField()
    test_case = models.ForeignKey("testcases.TestCase", null=True, blank=True,
        on_delete=models.PROTECT, related_name="web_configs", verbose_name="关联业务用例")
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("folder", "name", "pk")

    def __str__(self):
        return self.name


class WebEnvironment(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey("management.Product", on_delete=models.PROTECT)
    name = models.CharField("环境名称", max_length=200)
    base_url = models.CharField("测试站点", max_length=500)
    variables_encrypted = models.TextField(blank=True)
    setup_case = models.ForeignKey(WebCase, blank=True, null=True, on_delete=models.PROTECT)
    ignore_https_errors = models.BooleanField("允许测试站点使用自签名证书", default=False)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class WebSuite(models.Model):
    environment = models.ForeignKey(WebEnvironment, blank=True, null=True, on_delete=models.PROTECT, verbose_name="执行环境")
    datasets_encrypted = models.TextField(blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey("management.Product", on_delete=models.PROTECT)
    name = models.CharField("套件名称", max_length=200)
    base_url = models.CharField("测试站点", max_length=500)
    case_ids = models.JSONField(default=list)
    ignore_https_errors = models.BooleanField("允许测试站点使用自签名证书", default=False)
    stop_on_failure = models.BooleanField("失败后停止", default=False)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class WebRun(models.Model):
    STATES = [(key, label) for key, label in (
        ("queued", "排队中"), ("running", "执行中"), ("passed", "通过"),
        ("failed", "失败"), ("error", "执行异常"), ("cancelled", "已取消"),
        ("interrupted", "执行中断"),
    )]
    execution_mode = models.CharField(max_length=16, default="legacy", choices=[
        ("legacy", "历史执行"), ("formal", "正式执行"), ("debug", "调试执行"),
    ])
    test_run = models.ForeignKey("testruns.TestRun", null=True, blank=True, related_name="web_runs", on_delete=models.PROTECT)
    environment = models.ForeignKey(WebEnvironment, null=True, blank=True, on_delete=models.SET_NULL)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey("management.Product", on_delete=models.PROTECT)
    suite = models.ForeignKey(WebSuite, null=True, on_delete=models.SET_NULL)
    source = models.ForeignKey("self", null=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=200)
    submission_token = models.UUIDField()
    snapshot_encrypted = models.TextField()
    status = models.CharField(max_length=16, choices=STATES, default="queued")
    total = models.PositiveIntegerField(default=0)
    completed_count = models.PositiveIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(null=True)
    finished = models.DateTimeField(null=True)
    heartbeat = models.DateTimeField(null=True)

    class Meta:
        ordering = ("-created",)
        constraints = [models.UniqueConstraint(fields=("owner", "submission_token"), name="web_run_submission_unique")]

    @property
    def terminal(self):
        return self.status not in {"queued", "running"}


class WebResult(models.Model):
    run = models.ForeignKey(WebRun, on_delete=models.CASCADE, related_name="results")
    position = models.PositiveIntegerField()
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=16)
    steps = models.JSONField(default=list)
    error = models.TextField(blank=True)
    elapsed_ms = models.PositiveIntegerField(default=0)
    screenshot = models.BinaryField(null=True)

    class Meta:
        ordering = ("position",)
        constraints = [models.UniqueConstraint(fields=("run", "position"), name="web_result_position_unique")]

class WebAIRequest(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey("management.Product", on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    submission_token = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    input_encrypted = models.TextField()
    generated = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created", "-pk")
        constraints = [models.UniqueConstraint(fields=("owner", "submission_token"), name="web_ai_submission_unique")]


class WebAIDraft(models.Model):
    request = models.ForeignKey(WebAIRequest, on_delete=models.CASCADE, related_name="drafts")
    position = models.PositiveIntegerField()
    name = models.CharField("用例名称", max_length=200)
    description = models.TextField("前置条件与预期结果", blank=True)
    evidence = models.TextField("原文依据", blank=True)
    steps = models.JSONField("操作与断言步骤", default=list)
    questions = models.JSONField(default=list)
    review_notes = models.TextField("确认说明", blank=True)
    reviewed_at = models.DateTimeField(null=True)
    revision = models.PositiveIntegerField(default=1)
    web_case = models.ForeignKey(WebCase, null=True, on_delete=models.SET_NULL)
    imported_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ("position", "pk")
        constraints = [models.UniqueConstraint(fields=("request", "position"), name="web_ai_draft_position_unique")]
