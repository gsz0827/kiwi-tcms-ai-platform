import uuid

from django.conf import settings
from django.db import models
from tcms.core.history import KiwiHistoricalRecords
from .audit_models import AIAuditLog
from .source_review_models import DocumentSourceReview
from .postman_models import PostmanImport

from tcms.management.models import Product


class APIEnvironment(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    name = models.CharField("环境名称", max_length=100)
    base_url = models.CharField("服务地址", max_length=500)
    headers = models.JSONField("公共请求头", default=dict, blank=True)
    variables = models.JSONField("环境变量", default=dict, blank=True)
    secret_headers_encrypted = models.TextField(blank=True)
    timeout = models.PositiveSmallIntegerField("单条超时（秒）", default=10)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.product} / {self.name}"


class APICase(models.Model):
    import_review_required = models.BooleanField(default=False, editable=False)
    METHODS = [(value, value) for value in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    name = models.CharField("用例名称", max_length=200)
    sequence = models.PositiveSmallIntegerField("执行顺序", default=100)
    extracts = models.JSONField("响应变量提取", default=dict, blank=True)
    method = models.CharField("请求方法", choices=METHODS, max_length=8, default="GET")
    path = models.CharField("接口路径", max_length=1000)
    headers = models.JSONField("请求头", default=dict, blank=True)
    query = models.JSONField("查询参数", default=dict, blank=True)
    body = models.JSONField("JSON 请求体", default=dict, blank=True)
    send_body = models.BooleanField("发送 JSON 请求体", default=False)
    expected_status = models.PositiveSmallIntegerField("预期状态码", default=200)
    assertions = models.JSONField("JSON 字段断言", default=list, blank=True)
    max_elapsed_ms = models.PositiveIntegerField("响应时间上限（毫秒）", default=0)
    test_case = models.ForeignKey(
        "testcases.TestCase", null=True, blank=True, on_delete=models.PROTECT,
        verbose_name="关联平台测试用例",
    )
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.test_case.summary if self.test_case_id else self.name


class APIRun(models.Model):
    STATUSES = [("queued", "排队中"), ("running", "执行中"),
                ("cancel_requested", "正在停止"), ("completed", "已完成"),
                ("cancelled", "已停止"), ("interrupted", "执行中断")]
    ACTIVE_STATUSES = ("queued", "running", "cancel_requested")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    environment_name = models.CharField(max_length=100)
    submission_token = models.UUIDField()
    snapshot_encrypted = models.TextField()
    source_run = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="reruns")
    suite = models.ForeignKey("APISuite", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="runs")
    trigger = models.CharField(max_length=12, default="manual", choices=[
        ("manual", "手动"), ("schedule", "定时"), ("ci", "CI")])
    test_run = models.ForeignKey(
        "testruns.TestRun", null=True, blank=True, on_delete=models.SET_NULL
    )
    status = models.CharField(max_length=24, choices=STATUSES, default="queued")
    error = models.CharField(max_length=500, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(null=True, blank=True)
    heartbeat = models.DateTimeField(null=True, blank=True, verbose_name="心跳时间")
    completed = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created",)
        constraints = [models.UniqueConstraint(
            fields=("owner", "submission_token"), name="unique_api_submission"
        )]
        indexes = [
            models.Index(fields=("status", "created"), name="api_run_queue"),
            models.Index(fields=("status", "heartbeat"), name="api_run_heartbeat"),
        ]

    @property
    def execution_mode(self):
        # New submissions persist mode inside the immutable encrypted snapshot;
        # absent metadata retains compatibility with historical/CI submissions.
        import json
        from .crypto import decrypt_api_key
        try:
            mode = json.loads(decrypt_api_key(self.snapshot_encrypted)).get("execution_mode", "legacy")
        except (ValueError, TypeError, AttributeError, RuntimeError):
            return "invalid"
        return mode if mode in ("formal", "debug", "legacy") else "invalid"

    @property
    def is_terminal(self):
        return self.status in ("completed", "cancelled", "interrupted")


class APIResult(models.Model):
    STATUSES = [("pending", "未执行"), ("passed", "通过"), ("failed", "断言失败"),
                ("error", "请求异常"), ("skipped", "已跳过")]
    run = models.ForeignKey(APIRun, related_name="results", on_delete=models.CASCADE)
    test_case = models.ForeignKey("testcases.TestCase", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="api_results")
    position = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=STATUSES, default="pending")
    status_code = models.PositiveSmallIntegerField(null=True)
    elapsed_ms = models.PositiveIntegerField(null=True)
    request_summary = models.JSONField(default=dict)
    response_summary = models.TextField(blank=True)
    checks = models.JSONField(default=list)
    error = models.CharField(max_length=500, blank=True)
    writeback = models.CharField(max_length=500, blank=True)
    started = models.DateTimeField(null=True)
    completed = models.DateTimeField(null=True)

    class Meta:
        ordering = ("position",)
        constraints = [models.UniqueConstraint(
            fields=("run", "position"), name="unique_api_run_position"
        )]


class APISuite(models.Model):
    datasets_encrypted = models.TextField(blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    name = models.CharField("套件名称", max_length=100)
    environment = models.ForeignKey(APIEnvironment, on_delete=models.PROTECT)
    # Preserve the selected IDs if a configuration disappears: fail validation,
    # rather than silently running a smaller suite.
    case_ids = models.JSONField(default=list)
    stop_on_failure = models.BooleanField("失败后停止", default=False)
    share_cookies = models.BooleanField("本次执行共享 Cookie", default=False)
    schedule_enabled = models.BooleanField("启用定时执行", default=False)
    interval_minutes = models.PositiveIntegerField("执行间隔（分钟）", default=60)
    next_run_at = models.DateTimeField("下次执行时间", null=True, blank=True, db_index=True)
    last_triggered = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    ci_token_hash = models.CharField(max_length=64, blank=True, editable=False)
    ci_token_expires = models.DateTimeField(null=True, blank=True, editable=False)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class AutomationArchive(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    kind = models.CharField(max_length=8, choices=[("web","Web"),("api","接口")])
    source_id = models.UUIDField()
    test_run = models.OneToOneField("testruns.TestRun", on_delete=models.PROTECT)
    report = models.OneToOneField("AITestReport", on_delete=models.PROTECT)
    results = models.JSONField(default=list)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("kind","source_id"), name="unique_automation_archive")]


class APIAIRequest(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    category = models.ForeignKey("testcases.Category", on_delete=models.PROTECT)
    target_case = models.ForeignKey("testcases.TestCase", null=True, blank=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    submission_token = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    input_encrypted = models.TextField()
    generated = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created",)
        constraints = [models.UniqueConstraint(fields=("owner", "submission_token"), name="unique_api_ai_submission")]


class APIAIDraft(models.Model):
    request = models.ForeignKey(APIAIRequest, on_delete=models.CASCADE, related_name="drafts")
    position = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    evidence = models.TextField(blank=True)
    questions = models.JSONField(default=list)
    configuration = models.JSONField(default=dict)
    review_notes = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=1)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    api_case = models.ForeignKey(APICase, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ("position",)
        constraints = [models.UniqueConstraint(fields=("request", "position"), name="unique_api_ai_position")]


from .project_rule_models import AIInstructionRevision, ProjectAIRuleBinding


class AIInstructionProfile(models.Model):
    """Versioned, user-owned guidance injected into new AI requirement jobs."""

    OPERATION_CHOICES = (
        ("all", "所有需求任务"),
        ("requirement_analysis", "需求分析"),
        ("test_case_generation", "生成测试用例"),
        ("dev_task_breakdown", "拆分开发任务"),
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_instruction_profiles",
        verbose_name="所属账号",
    )
    name = models.CharField(max_length=100, verbose_name="规则包名称")
    description = models.CharField(max_length=255, blank=True, verbose_name="说明")
    product = models.ForeignKey(
        Product,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_instruction_profiles",
        verbose_name="适用产品",
    )
    operation = models.CharField(
        max_length=32,
        choices=OPERATION_CHOICES,
        default="all",
        verbose_name="适用任务",
    )
    sections = models.JSONField(default=dict, blank=True)
    instructions = models.TextField(verbose_name="测试规则")
    version = models.PositiveIntegerField(default=1, verbose_name="版本")
    is_active = models.BooleanField(default=True, verbose_name="已启用")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-is_active", "name", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "product"),
                name="unique_ai_instruction_profile_per_project",
            )
        ]
        verbose_name = "AI 规则包"
        verbose_name_plural = "AI 规则包"

    def __str__(self):
        return f"{self.name} v{self.version}"

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "instructions", "description", "product_id", "operation", "version"
            ).first()
            if previous and any(
                getattr(self, field) != previous[field]
                for field in ("instructions", "description", "product_id", "operation")
            ):
                self.version = max(self.version, previous["version"]) + 1
        super().save(*args, **kwargs)


class AIRequest(models.Model):
    submission_token = models.UUIDField(null=True, blank=True, editable=False)
    submission_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    title = models.CharField(max_length=200, verbose_name="需求标题")
    requirement = models.TextField(verbose_name="功能说明")
    document_sections = models.JSONField(default=dict, blank=True, verbose_name="需求文档分区")
    target_version = models.ForeignKey("management.Version", blank=True, null=True, on_delete=models.SET_NULL, related_name="ai_target_requirements", verbose_name="目标产品版本")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, blank=True, null=True, on_delete=models.SET_NULL, related_name="assigned_ai_requirements", verbose_name="需求负责人")
    priority = models.CharField(max_length=8, default="P3", choices=[(f"P{i}", f"P{i}") for i in range(1,6)], verbose_name="优先级")
    status = models.CharField(max_length=16, default="draft", choices=[("draft","草稿"),("confirmed","已确认"),("doing","进行中"),("done","已完成")], verbose_name="需求状态")
    version = models.PositiveIntegerField(default=1, verbose_name="需求版本")
    needs_case_review = models.BooleanField(
        default=False, verbose_name="需求变更后用例待更新"
    )
    changed_at = models.DateTimeField(
        blank=True, null=True, verbose_name="最近变更时间"
    )
    category = models.ForeignKey(
        "testcases.Category",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_requests",
        verbose_name="目标分类",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_requests",
        verbose_name="创建人",
    )
    analysis = models.JSONField(default=dict, blank=True, verbose_name="AI需求分析")
    analysis_raw = models.TextField(blank=True, verbose_name="AI需求分析原始结果")
    analysis_model_config = models.ForeignKey(
        "AIModelConfig",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="requirement_analyses",
        verbose_name="需求分析使用的模型配置",
    )
    skill_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="需求任务使用的 AI 规则包快照",
    )
    analyzed_at = models.DateTimeField(blank=True, null=True, verbose_name="分析时间")
    coverage_analysis = models.JSONField(
        default=dict, blank=True, verbose_name="AI用例覆盖分析"
    )
    coverage_raw = models.TextField(blank=True, verbose_name="AI用例覆盖分析原始结果")
    coverage_model_config = models.ForeignKey(
        "AIModelConfig",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="coverage_analyses",
        verbose_name="覆盖分析使用的模型配置",
    )
    coverage_analyzed_at = models.DateTimeField(
        blank=True, null=True, verbose_name="覆盖分析时间"
    )
    result = models.TextField(blank=True, verbose_name="AI生成结果")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI请求"
        verbose_name_plural = "AI请求"
        permissions = (
            ("manage_members", "可以管理产品成员与角色"),
            ("manage_requirement", "可以编辑与删除产品内他人的需求"),
        )
        constraints = [
            models.UniqueConstraint(
                fields=("created_by", "submission_token"),
                name="unique_ai_submission_per_owner",
            )
        ]

    def __str__(self):
        return self.title

    @property
    def document_blocks(self):
        from .document_sections import blocks
        return blocks(self)

    @property
    def requirement_document(self):
        from .document_sections import requirement_text
        return requirement_text(self)

    @property
    def document_attributes(self):
        from .document_sections import requirement_attributes
        return requirement_attributes(self)

    @property
    def generation_failed(self):
        return self.result.startswith("AI生成失败：")

    @property
    def analysis_failed(self):
        return self.analysis_raw.startswith("AI需求分析失败：")

    @property
    def has_analysis(self):
        return bool(self.analysis)

    @property
    def coverage_failed(self):
        return self.coverage_raw.startswith("AI覆盖分析失败：")

    @property
    def has_coverage(self):
        return bool(self.coverage_analysis)

    @property
    def has_coverage_gaps(self):
        coverage = self.coverage_analysis or {}
        if coverage.get("missing_coverage") or coverage.get("recommendations"):
            return True
        return any(
            item.get("status") in {"partial", "missing"}
            for item in coverage.get("coverage_items") or []
            if isinstance(item, dict)
        )


class AITestCaseDraft(models.Model):
    source_context = models.JSONField(default=dict, blank=True, verbose_name="用例设计依据快照")
    dev_tasks = models.ManyToManyField(
        "AIDevTask", blank=True, related_name="case_designs", verbose_name="参考开发任务单"
    )
    request = models.ForeignKey(
        AIRequest,
        on_delete=models.CASCADE,
        related_name="drafts",
        verbose_name="AI请求",
    )
    case_number = models.CharField(max_length=50, verbose_name="用例编号")
    summary = models.CharField(max_length=255, verbose_name="用例标题")
    priority = models.CharField(max_length=8, default="P3", verbose_name="优先级")
    test_type = models.CharField(max_length=64, blank=True, verbose_name="测试类型")
    preconditions = models.JSONField(default=list, blank=True, verbose_name="前置条件")
    steps = models.JSONField(default=list, blank=True, verbose_name="测试步骤")
    requirement_version = models.PositiveIntegerField(
        default=1, verbose_name="来源需求版本"
    )
    needs_update = models.BooleanField(default=False, verbose_name="需要随需求更新")
    imported_case = models.OneToOneField(
        "testcases.TestCase",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_source_draft",
        verbose_name="已导入用例",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        verbose_name = "AI测试用例草稿"
        verbose_name_plural = "AI测试用例草稿"

    def __str__(self):
        return f"{self.case_number} {self.summary}"


class AIDevTask(models.Model):
    """需求拆分出来的开发任务单（开发文档）。

    一个需求对应多个任务单：需求分析回答「要做什么、有什么风险」，任务单回答
    「谁按什么顺序动手、改哪儿、怎么算做完」。任务单由 AI 拆分后落库，之后由
    人在页面上改状态和内容，所以这里保存的是可编辑的正式数据，不是草稿。
    """

    STATUS_CHOICES = (
        ("todo", "待开始"),
        ("doing", "开发中"),
        ("done", "已完成"),
        ("blocked", "阻塞"),
    )

    request = models.ForeignKey(
        AIRequest,
        on_delete=models.CASCADE,
        related_name="dev_tasks",
        verbose_name="所属需求",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_dev_tasks",
        verbose_name="所属账号",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="assigned_dev_tasks",
        verbose_name="负责人",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="dispatched_dev_tasks",
        verbose_name="指派人",
    )
    assigned_at = models.DateTimeField(blank=True, null=True, verbose_name="指派时间")
    position = models.PositiveIntegerField(default=1, verbose_name="顺序")
    task_number = models.CharField(max_length=50, blank=True, verbose_name="任务编号")
    title = models.CharField(max_length=200, verbose_name="任务标题")
    module = models.CharField(max_length=200, blank=True, verbose_name="涉及模块")
    description = models.TextField(blank=True, verbose_name="开发说明")
    acceptance = models.TextField(blank=True, verbose_name="验收标准")
    document_sections = models.JSONField(default=dict, blank=True, verbose_name="开发文档分区")
    target_version = models.ForeignKey("management.Version", blank=True, null=True, on_delete=models.SET_NULL, related_name="ai_target_dev_tasks", verbose_name="目标产品版本")
    priority = models.CharField(max_length=8, default="P3", verbose_name="优先级")
    estimate_hours = models.PositiveIntegerField(
        blank=True, null=True, verbose_name="预估工时（小时）"
    )
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="todo",
        db_index=True,
        verbose_name="状态",
    )
    requirement_version = models.PositiveIntegerField(
        default=1, verbose_name="来源需求版本"
    )
    needs_update = models.BooleanField(default=False, verbose_name="需要随需求更新")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "id")
        verbose_name = "开发任务单"
        verbose_name_plural = "开发任务单"
        permissions = (
            ("split_devtask", "可以拆分开发任务单"),
            ("assign_devtask", "可以指派开发任务单"),
        )
        indexes = [
            models.Index(fields=("owner", "status"), name="ai_dev_task_owner_status"),
            models.Index(fields=("assignee", "status"), name="ai_dev_task_assignee_status"),
        ]

    def __str__(self):
        return f"{self.task_number} {self.title}".strip()

    @property
    def document_blocks(self):
        from .document_sections import blocks
        return blocks(self, task=True)

    @property
    def is_done(self):
        return self.status == "done"

    @property
    def is_assigned(self):
        return self.assignee_id is not None


class AIModelConfig(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_model_configs",
        verbose_name="所属账号",
    )
    name = models.CharField(max_length=100, verbose_name="配置名称")
    api_base = models.URLField(max_length=500, verbose_name="API 地址")
    model = models.CharField(max_length=200, verbose_name="模型名称")
    timeout = models.PositiveIntegerField(default=300, verbose_name="超时时间（秒）")
    api_key_encrypted = models.TextField(blank=True, verbose_name="加密后的 API 密钥")
    is_active = models.BooleanField(default=False, verbose_name="当前启用")
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-is_active", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "name"), name="unique_ai_model_name_per_owner"
            )
        ]
        verbose_name = "AI 模型配置"
        verbose_name_plural = "AI 模型配置"

    def __str__(self):
        return f"{self.name} ({self.model})"

    def save(self, *args, **kwargs):
        if self.is_active and self.owner_id:
            type(self).objects.exclude(pk=self.pk).filter(
                owner_id=self.owner_id, is_active=True
            ).update(is_active=False)
        super().save(*args, **kwargs)

    @property
    def key_status(self):
        if self.api_key_encrypted:
            return "已保存密钥"
        return "未保存密钥"


class AITestCaseReview(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_test_case_reviews",
        verbose_name="评审账号",
    )
    test_case = models.ForeignKey(
        "testcases.TestCase",
        on_delete=models.CASCADE,
        related_name="ai_reviews",
        verbose_name="正式测试用例",
    )
    model_config = models.ForeignKey(
        AIModelConfig,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="test_case_reviews",
        verbose_name="使用的模型配置",
    )
    score = models.PositiveSmallIntegerField(default=0, verbose_name="质量评分")
    strengths = models.JSONField(default=list, blank=True, verbose_name="优点")
    issues = models.JSONField(default=list, blank=True, verbose_name="问题")
    missing_scenarios = models.JSONField(
        default=list, blank=True, verbose_name="缺失场景"
    )
    optimized_summary = models.CharField(max_length=255, verbose_name="优化后标题")
    optimized_preconditions = models.JSONField(
        default=list, blank=True, verbose_name="优化后前置条件"
    )
    optimized_steps = models.JSONField(
        default=list, blank=True, verbose_name="优化后步骤"
    )
    raw_result = models.TextField(blank=True, verbose_name="AI 原始结果")
    original_summary = models.CharField(max_length=255, verbose_name="原始标题快照")
    original_text = models.TextField(blank=True, verbose_name="原始正文快照")
    created = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(blank=True, null=True, verbose_name="应用时间")
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="applied_ai_test_case_reviews",
        verbose_name="应用人",
    )

    class Meta:
        ordering = ("-created",)
        verbose_name = "AI 测试用例评审"
        verbose_name_plural = "AI 测试用例评审"

    def __str__(self):
        return f"TC-{self.test_case_id} AI 评审 #{self.pk}"

    @property
    def is_applied(self):
        return self.applied_at is not None


class AITestRunAnalysis(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_test_run_analyses",
        verbose_name="分析账号",
    )
    test_run = models.ForeignKey(
        "testruns.TestRun",
        on_delete=models.CASCADE,
        related_name="ai_analyses",
        verbose_name="测试运行",
    )
    model_config = models.ForeignKey(
        AIModelConfig,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="test_run_analyses",
        verbose_name="使用的模型配置",
    )
    execution_snapshot = models.JSONField(
        default=dict, verbose_name="执行结果快照"
    )
    result = models.JSONField(default=dict, verbose_name="AI运行分析")
    raw_result = models.TextField(blank=True, verbose_name="AI原始结果")
    created = models.DateTimeField(auto_now_add=True, verbose_name="分析时间")

    class Meta:
        ordering = ("-created",)
        indexes = [
            models.Index(
                fields=("owner", "test_run", "-created"),
                name="ai_run_owner_created",
            )
        ]
        verbose_name = "AI 测试运行分析"
        verbose_name_plural = "AI 测试运行分析"

    def __str__(self):
        return f"TR-{self.test_run_id} AI 分析 #{self.pk}"


class AIDefectDraft(models.Model):
    SEVERITY_CHOICES = (
        ("critical", "致命"),
        ("high", "严重"),
        ("medium", "一般"),
        ("low", "轻微"),
    )
    PRIORITY_CHOICES = tuple((f"P{i}", f"P{i}") for i in range(1, 6))
    STATUS_CHOICES = (
        ("pending_submission", "待提交"),
        ("in_progress", "处理中"),
        ("fixed", "已修复"),
        ("pending_verification", "待验证"),
        ("closed", "已关闭"),
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_defect_drafts",
        verbose_name="所属账号",
    )
    execution = models.ForeignKey(
        "testruns.TestExecution",
        on_delete=models.CASCADE,
        related_name="ai_defect_drafts",
        verbose_name="失败执行",
    )
    model_config = models.ForeignKey(
        AIModelConfig,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="defect_drafts",
        verbose_name="使用的模型配置",
    )
    title = models.CharField(max_length=255, verbose_name="缺陷标题")
    severity = models.CharField(
        max_length=16,
        choices=SEVERITY_CHOICES,
        default="medium",
        verbose_name="严重程度",
    )
    priority = models.CharField(
        max_length=4,
        choices=PRIORITY_CHOICES,
        default="P3",
        verbose_name="优先级",
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default="pending_submission",
        db_index=True,
        verbose_name="缺陷状态",
    )
    fix_version = models.CharField(max_length=128, blank=True, verbose_name="修复版本")
    assignee_name = models.CharField(max_length=150, blank=True, verbose_name="负责人")
    closure_reason = models.TextField(blank=True, verbose_name="关闭原因")
    description = models.TextField(blank=True, verbose_name="问题描述")
    preconditions = models.JSONField(default=list, blank=True, verbose_name="前置条件")
    reproduction_steps = models.JSONField(
        default=list, blank=True, verbose_name="复现步骤"
    )
    expected_result = models.TextField(blank=True, verbose_name="预期结果")
    actual_result = models.TextField(blank=True, verbose_name="实际结果")
    environment = models.TextField(blank=True, verbose_name="测试环境")
    evidence = models.JSONField(default=list, blank=True, verbose_name="直接证据")
    likely_causes = models.JSONField(
        default=list, blank=True, verbose_name="待验证的可能原因"
    )
    raw_result = models.TextField(blank=True, verbose_name="AI 原始结果")
    linked_reference = models.OneToOneField(
        "linkreference.LinkReference",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_defect_draft",
        verbose_name="已关联缺陷",
    )
    fingerprint = models.CharField(
        max_length=64, blank=True, db_index=True, verbose_name="重复识别指纹"
    )
    duplicate_of = models.ForeignKey(
        "self",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="duplicates",
        verbose_name="重复于",
    )
    external_status = models.CharField(
        max_length=128, blank=True, verbose_name="外部缺陷状态"
    )
    last_synced_at = models.DateTimeField(
        blank=True, null=True, verbose_name="外部状态同步时间"
    )
    sync_note = models.TextField(blank=True, verbose_name="同步说明")
    created = models.DateTimeField(auto_now_add=True, verbose_name="生成时间")
    updated = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-created",)
        indexes = [
            models.Index(
                fields=("owner", "execution", "-created"),
                name="ai_defect_owner_created",
            )
        ]
        verbose_name = "AI 缺陷草稿"
        verbose_name_plural = "AI 缺陷草稿"

    def __str__(self):
        return f"TE-{self.execution_id} 缺陷草稿 #{self.pk}"


class AITestReport(models.Model):
    RELEASE_CHOICES = (
        ("go", "可以发布"),
        ("conditional_go", "有条件发布"),
        ("no_go", "不建议发布"),
    )
    APPROVAL_CHOICES = (
        ("pending", "待审批"),
        ("approved", "已批准"),
        ("rejected", "已驳回"),
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_test_reports",
        verbose_name="所属账号",
    )
    test_run = models.ForeignKey(
        "testruns.TestRun",
        on_delete=models.CASCADE,
        related_name="ai_reports",
        verbose_name="测试运行",
    )
    series_uuid = models.UUIDField(default=uuid.uuid4, db_index=True, verbose_name="报告系列")
    version = models.PositiveIntegerField(default=1, verbose_name="报告版本")
    previous_version = models.ForeignKey(
        "self",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="next_versions",
        verbose_name="上一版本",
    )
    is_current = models.BooleanField(default=True, db_index=True, verbose_name="当前版本")
    current_marker = models.BooleanField(
        blank=True,
        null=True,
        default=None,
        verbose_name="当前版本唯一标记",
    )
    source_analysis = models.ForeignKey(
        AITestRunAnalysis,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="reports",
        verbose_name="来源分析",
    )
    model_config = models.ForeignKey(
        AIModelConfig,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="test_reports",
        verbose_name="使用的模型配置",
    )
    title = models.CharField(max_length=255, verbose_name="报告标题")
    summary = models.TextField(verbose_name="执行摘要")
    scope = models.TextField(blank=True, verbose_name="测试范围")
    conclusion = models.TextField(blank=True, verbose_name="测试结论")
    release_decision = models.CharField(
        max_length=24,
        choices=RELEASE_CHOICES,
        default="conditional_go",
        verbose_name="发布结论",
    )
    metrics_snapshot = models.JSONField(default=dict, verbose_name="执行指标快照")
    snapshot_hash = models.CharField(max_length=64, blank=True, verbose_name="快照校验值")
    gate_result = models.JSONField(default=dict, blank=True, verbose_name="发布门禁结果")
    gate_waived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="gate_waived_ai_test_reports",
        verbose_name="风险放行人",
    )
    gate_waived_at = models.DateTimeField(blank=True, null=True, verbose_name="风险放行时间")
    gate_waive_reason = models.TextField(blank=True, verbose_name="风险放行理由")
    defect_summary = models.JSONField(default=list, blank=True, verbose_name="缺陷摘要")
    recommendations = models.JSONField(default=list, blank=True, verbose_name="后续建议")
    raw_result = models.TextField(blank=True, verbose_name="AI 原始结果")
    approval_status = models.CharField(
        max_length=16,
        choices=APPROVAL_CHOICES,
        default="pending",
        db_index=True,
        verbose_name="审批状态",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="approved_ai_test_reports",
        verbose_name="审批人",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="审批时间")
    approval_comment = models.TextField(blank=True, verbose_name="审批意见")
    signature = models.CharField(max_length=255, blank=True, verbose_name="签字确认")
    created = models.DateTimeField(auto_now_add=True, verbose_name="生成时间")
    updated = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("-created",)
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "test_run", "version"),
                name="unique_ai_report_run_version",
            ),
            models.UniqueConstraint(
                fields=("owner", "test_run", "current_marker"),
                name="unique_current_ai_report_per_run",
            ),
        ]
        indexes = [
            models.Index(
                fields=("owner", "test_run", "-created"),
                name="ai_report_owner_created",
            )
        ]
        verbose_name = "AI 测试报告"
        verbose_name_plural = "AI 测试报告"
        permissions = (
            ("approve_aireport", "可以审批 AI 测试报告"),
        )

    def __str__(self):
        return f"TR-{self.test_run_id} 测试报告 #{self.pk}"

    def save(self, *args, **kwargs):
        self.current_marker = True if self.is_current else None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "is_current" in update_fields:
            kwargs["update_fields"] = tuple(set(update_fields) | {"current_marker"})
        super().save(*args, **kwargs)


class AIRegressionVerification(models.Model):
    STATUS_CHOICES = (
        ("passed", "回归通过"),
        ("failed", "回归失败"),
        ("incomplete", "回归未完成"),
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_regression_verifications",
        verbose_name="所属账号",
    )
    source_report = models.ForeignKey(
        AITestReport,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="regression_verifications",
        verbose_name="来源测试报告",
    )
    defect_draft = models.ForeignKey(
        AIDefectDraft,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="regression_verifications",
        verbose_name="来源缺陷",
    )
    regression_run = models.ForeignKey(
        "testruns.TestRun",
        on_delete=models.CASCADE,
        related_name="ai_regression_results",
        verbose_name="回归测试运行",
    )
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default="incomplete",
        verbose_name="验证结论",
    )
    result = models.JSONField(default=dict, verbose_name="比对结果")
    notes = models.TextField(blank=True, verbose_name="人工备注")
    created = models.DateTimeField(auto_now_add=True, verbose_name="验证时间")

    class Meta:
        ordering = ("-created",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(source_report__isnull=False, defect_draft__isnull=True)
                    | models.Q(source_report__isnull=True, defect_draft__isnull=False)
                ),
                name="ai_regression_exactly_one_source",
            )
        ]
        indexes = [
            models.Index(
                fields=("owner", "source_report", "-created"),
                name="ai_regress_owner_created",
            )
        ]
        verbose_name = "AI 回归验证"
        verbose_name_plural = "AI 回归验证"

    def __str__(self):
        source = (
            f"报告 #{self.source_report_id}"
            if self.source_report_id
            else f"缺陷 #{self.defect_draft_id}"
        )
        return f"{source} 回归验证 #{self.pk}"


class AIDefectStatusHistory(models.Model):
    SOURCE_CHOICES = (
        ("manual", "人工操作"),
        ("external_sync", "外部同步"),
        ("regression", "回归验证"),
        ("link", "关联缺陷"),
    )

    defect = models.ForeignKey(
        AIDefectDraft,
        on_delete=models.CASCADE,
        related_name="status_history",
        verbose_name="缺陷",
    )
    from_status = models.CharField(max_length=32, blank=True, verbose_name="原状态")
    to_status = models.CharField(max_length=32, verbose_name="新状态")
    source = models.CharField(
        max_length=24, choices=SOURCE_CHOICES, default="manual", verbose_name="变更来源"
    )
    reason = models.TextField(blank=True, verbose_name="变更原因")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_defect_status_changes",
        verbose_name="操作人",
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name="变更时间")

    class Meta:
        ordering = ("-created",)
        verbose_name = "AI 缺陷状态历史"
        verbose_name_plural = "AI 缺陷状态历史"

    @property
    def from_status_display(self):
        return dict(AIDefectDraft.STATUS_CHOICES).get(self.from_status, self.from_status or "-")

    @property
    def to_status_display(self):
        return dict(AIDefectDraft.STATUS_CHOICES).get(self.to_status, self.to_status)


class AITestReportRevision(models.Model):
    report = models.ForeignKey(
        AITestReport,
        on_delete=models.CASCADE,
        related_name="revisions",
        verbose_name="报告",
    )
    revision = models.PositiveIntegerField(verbose_name="编辑修订号")
    content_snapshot = models.JSONField(default=dict, verbose_name="内容快照")
    change_reason = models.CharField(max_length=255, verbose_name="修改原因")
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_report_revisions",
        verbose_name="修改人",
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name="修改时间")

    class Meta:
        ordering = ("-created",)
        constraints = [
            models.UniqueConstraint(
                fields=("report", "revision"), name="unique_ai_report_revision"
            )
        ]
        verbose_name = "AI 报告修订记录"
        verbose_name_plural = "AI 报告修订记录"


class AIReleaseGateRule(models.Model):
    history = KiwiHistoricalRecords()
    """产品级发布门禁规则：一个产品一条，由测试经理维护。

    规则不挂在账号上：门禁是产品的发布政策，不是某个人的偏好。挂账号时
    ``evaluate_release_gate`` 只会看报告自己的 owner，经理配的规则管不到工程师
    生成的报告，等于没有门禁。
    """

    product = models.ForeignKey(
        "management.Product",
        on_delete=models.CASCADE,
        related_name="ai_release_gate_rules",
        verbose_name="产品",
    )
    name = models.CharField(max_length=128, default="默认发布门禁", verbose_name="规则名称")
    block_priority = models.CharField(
        max_length=4,
        choices=AIDefectDraft.PRIORITY_CHOICES,
        default="P1",
        verbose_name="阻断缺陷优先级",
    )
    min_success_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=95, verbose_name="最低成功率"
    )
    require_all_executed = models.BooleanField(default=True, verbose_name="要求全部执行")
    max_open_defects = models.PositiveIntegerField(default=0, verbose_name="允许未关闭缺陷数")
    is_active = models.BooleanField(default=True, verbose_name="启用")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_release_gate_rules_updated",
        verbose_name="最后修改人",
    )
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("product__name", "name")
        constraints = [
            models.UniqueConstraint(fields=("product",), name="unique_ai_gate_rule"),
        ]
        verbose_name = "AI 发布门禁规则"
        verbose_name_plural = "AI 发布门禁规则"

    def __str__(self):
        return f"{self.product.name} · {self.name}"


class AIIterationReport(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_iteration_reports",
        verbose_name="所属账号",
    )
    title = models.CharField(max_length=255, verbose_name="迭代报告标题")
    product = models.ForeignKey(
        "management.Product",
        on_delete=models.CASCADE,
        related_name="ai_iteration_reports",
        verbose_name="产品",
    )
    version = models.ForeignKey(
        "management.Version",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_iteration_reports",
        verbose_name="版本",
    )
    runs = models.ManyToManyField(
        "testruns.TestRun", related_name="ai_iteration_reports", verbose_name="测试运行"
    )
    metrics_snapshot = models.JSONField(default=dict, verbose_name="聚合指标快照")
    run_snapshots = models.JSONField(default=list, verbose_name="各运行快照")
    gate_result = models.JSONField(default=dict, blank=True, verbose_name="发布门禁结果")
    conclusion = models.TextField(blank=True, verbose_name="测试结论")
    release_decision = models.CharField(
        max_length=24,
        choices=AITestReport.RELEASE_CHOICES,
        default="conditional_go",
        verbose_name="发布结论",
    )
    snapshot_hash = models.CharField(max_length=64, blank=True, verbose_name="快照校验值")
    created = models.DateTimeField(auto_now_add=True, verbose_name="生成时间")

    class Meta:
        ordering = ("-created",)
        verbose_name = "AI 迭代测试报告"
        verbose_name_plural = "AI 迭代测试报告"


class AIRequirementVersion(models.Model):
    request = models.ForeignKey(
        AIRequest,
        on_delete=models.CASCADE,
        related_name="versions",
        verbose_name="需求",
    )
    version = models.PositiveIntegerField(verbose_name="版本")
    title = models.CharField(max_length=200, verbose_name="需求标题快照")
    @property
    def document_blocks(self):
        from .document_sections import blocks
        return blocks(self)

    requirement = models.TextField(verbose_name="需求描述快照")
    document_sections = models.JSONField(default=dict, blank=True, verbose_name="需求分区快照")
    attributes = models.JSONField(default=dict, blank=True, verbose_name="需求属性快照")
    change_summary = models.CharField(max_length=255, blank=True, verbose_name="变更说明")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_requirement_versions",
        verbose_name="修改人",
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name="记录时间")

    class Meta:
        ordering = ("-version",)
        constraints = [
            models.UniqueConstraint(
                fields=("request", "version"), name="unique_ai_requirement_version"
            )
        ]
        verbose_name = "AI 需求版本"
        verbose_name_plural = "AI 需求版本"


class AIJob(models.Model):
    OPERATION_CHOICES = (
        ("api_case_generation", "AI 生成接口用例"),
        ("web_case_generation", "AI 生成 Web 用例"),
        ("requirement_analysis", "需求分析"),
        ("test_case_generation", "生成测试用例"),
        ("dev_task_breakdown", "拆分开发任务"),
        ("coverage_analysis", "覆盖率分析"),
        ("coverage_supplement", "补充覆盖缺口"),
        ("test_case_review", "测试用例评审"),
        ("test_run_analysis", "测试运行分析"),
        ("defect_draft_generation", "生成缺陷草稿"),
        ("test_report_generation", "生成测试报告"),
        ("connection_test", "模型连接测试"),
    )
    STATUS_CHOICES = (
        ("queued", "排队中"),
        ("running", "执行中"),
        ("cancel_requested", "正在取消"),
        ("completed", "已完成"),
        ("failed", "失败"),
        ("cancelled", "已取消"),
        ("interrupted", "中断"),
    )
    ACTIVE_STATUSES = ("queued", "running", "cancel_requested")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_jobs",
        verbose_name="所属账号",
    )
    model_config = models.ForeignKey(
        AIModelConfig,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
        verbose_name="模型配置",
    )
    operation = models.CharField(
        max_length=40, choices=OPERATION_CHOICES, verbose_name="任务类型"
    )
    payload = models.JSONField(default=dict, verbose_name="任务参数")
    result = models.JSONField(default=dict, blank=True, verbose_name="任务结果摘要")
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default="queued", verbose_name="状态"
    )
    progress = models.PositiveSmallIntegerField(default=5, verbose_name="真实进度")
    stage = models.CharField(max_length=120, default="等待后台处理", verbose_name="当前阶段")
    error_message = models.TextField(blank=True, verbose_name="错误摘要")
    result_url = models.CharField(max_length=500, blank=True, verbose_name="结果地址")
    dedupe_key = models.CharField(max_length=255, blank=True, db_index=True, verbose_name="防重复键")
    attempts = models.PositiveSmallIntegerField(default=1, verbose_name="尝试次数")
    created = models.DateTimeField(auto_now_add=True, verbose_name="提交时间")
    started = models.DateTimeField(blank=True, null=True, verbose_name="开始时间")
    heartbeat = models.DateTimeField(blank=True, null=True, verbose_name="心跳时间")
    completed = models.DateTimeField(blank=True, null=True, verbose_name="完成时间")

    class Meta:
        ordering = ("-created",)
        indexes = [
            models.Index(fields=("status", "created"), name="ai_job_status_created"),
            models.Index(fields=("owner", "-created"), name="ai_job_owner_created"),
            models.Index(fields=("status", "heartbeat"), name="ai_job_heartbeat"),
        ]
        verbose_name = "AI 后台任务"
        verbose_name_plural = "AI 后台任务"

    def __str__(self):
        return f"{self.get_operation_display()} {self.id}"

    @property
    def is_terminal(self):
        return self.status in {"completed", "failed", "cancelled", "interrupted"}


class AIUsageLog(models.Model):
    OPERATION_CHOICES = (
        ("api_case_generation", "AI 生成接口用例"),
        ("web_case_generation", "AI 生成 Web 用例"),
        ("requirement_analysis", "需求分析"),
        ("test_case_generation", "生成测试用例"),
        ("dev_task_breakdown", "拆分开发任务"),
        ("coverage_analysis", "覆盖率分析"),
        ("coverage_supplement", "补充覆盖缺口"),
        ("test_case_review", "测试用例评审"),
        ("test_run_analysis", "测试运行分析"),
        ("defect_draft_generation", "生成缺陷草稿"),
        ("test_report_generation", "生成测试报告"),
        ("connection_test", "模型连接测试"),
        ("other", "其他"),
    )
    STATUS_CHOICES = (("success", "成功"), ("error", "失败"))

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_usage_logs",
        verbose_name="所属账号",
    )
    model_config = models.ForeignKey(
        AIModelConfig,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="usage_logs",
        verbose_name="模型配置",
    )
    config_name = models.CharField(max_length=100, verbose_name="配置名称快照")
    model_name = models.CharField(max_length=200, verbose_name="模型名称快照")
    operation = models.CharField(
        max_length=40, choices=OPERATION_CHOICES, verbose_name="操作类型"
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, verbose_name="调用状态"
    )
    duration_ms = models.PositiveIntegerField(default=0, verbose_name="耗时（毫秒）")
    prompt_tokens = models.PositiveIntegerField(
        blank=True, null=True, verbose_name="输入 Token"
    )
    completion_tokens = models.PositiveIntegerField(
        blank=True, null=True, verbose_name="输出 Token"
    )
    total_tokens = models.PositiveIntegerField(
        blank=True, null=True, verbose_name="总 Token"
    )
    error_message = models.TextField(blank=True, verbose_name="错误摘要")
    created = models.DateTimeField(auto_now_add=True, verbose_name="调用时间")

    class Meta:
        ordering = ("-created",)
        indexes = [
            models.Index(fields=("owner", "-created"), name="ai_usage_owner_created"),
        ]
        verbose_name = "AI 调用记录"
        verbose_name_plural = "AI 调用记录"

    def __str__(self):
        return f"{self.get_operation_display()} - {self.get_status_display()}"


class ProjectResourceFolder(models.Model):
    RESOURCE_TYPES = (
        ("requirement", "需求"),
        ("case", "测试用例"),
        ("plan", "测试计划"),
        ("run", "执行任务"),
        ("case_group", "共享用例业务目录"),
        ("web_case", "Web 自动化用例"),
        ("api_case", "接口自动化用例"),
    )

    product = models.ForeignKey(
        "management.Product",
        on_delete=models.CASCADE,
        related_name="shared_resource_folders",
        verbose_name="项目",
    )
    resource_type = models.CharField(
        max_length=20, choices=RESOURCE_TYPES, verbose_name="资源类型"
    )
    name = models.CharField(max_length=120, verbose_name="目录名称")
    parent = models.ForeignKey(
        "self",
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="children",
        verbose_name="上级目录",
    )
    position = models.PositiveIntegerField(default=0, verbose_name="排序")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_project_resource_folders",
        verbose_name="创建人",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="updated_project_resource_folders",
        verbose_name="最后修改人",
    )
    created = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ("product__name", "resource_type", "position", "name", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("product", "resource_type", "parent", "name"),
                name="unique_shared_resource_folder",
            )
        ]
        indexes = [
            models.Index(
                fields=("product", "resource_type", "parent", "position"),
                name="shared_folder_tree_idx",
            )
        ]
        verbose_name = "项目共享资源目录"
        verbose_name_plural = "项目共享资源目录"

    def __str__(self):
        return f"{self.product} / {self.name}"


class ProjectResourceAssignment(models.Model):
    folder = models.ForeignKey(
        ProjectResourceFolder,
        on_delete=models.CASCADE,
        related_name="resource_assignments",
        verbose_name="共享目录",
    )
    resource_type = models.CharField(
        max_length=20,
        choices=ProjectResourceFolder.RESOURCE_TYPES,
        verbose_name="资源类型",
    )
    object_id = models.PositiveBigIntegerField(verbose_name="资源编号")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="project_resource_assignments",
        verbose_name="归档人",
    )
    updated = models.DateTimeField(auto_now=True, verbose_name="归档时间")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("resource_type", "object_id"),
                name="unique_shared_resource_assignment",
            )
        ]
        indexes = [
            models.Index(
                fields=("resource_type", "object_id"),
                name="shared_resource_lookup_idx",
            )
        ]
        verbose_name = "项目共享资源归档"
        verbose_name_plural = "项目共享资源归档"

    def __str__(self):
        return f"{self.get_resource_type_display()} #{self.object_id} -> {self.folder}"

class SavedCaseView(models.Model):
    """Personal filter preferences, never a grant of case or folder access."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_case_views"
    )
    name = models.CharField("视图名称", max_length=80)
    filters = models.JSONField("筛选条件", default=dict)
    revision = models.PositiveIntegerField("修订号", default=1)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "pk")
        constraints = [
            models.UniqueConstraint(fields=("owner", "name"), name="unique_saved_case_view_name")
        ]
        verbose_name = "个人用例筛选视图"
        verbose_name_plural = "个人用例筛选视图"

    def __str__(self):
        return self.name
