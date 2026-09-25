from django.contrib import admin

from .models import (
    AIDefectDraft,
    AIDefectStatusHistory,
    AIIterationReport,
    AIInstructionProfile,
    AIJob,
    AIModelConfig,
    AIReleaseGateRule,
    AIRegressionVerification,
    AIRequest,
    AIRequirementVersion,
    AITestCaseDraft,
    AITestCaseReview,
    AITestReport,
    AITestReportRevision,
    AITestRunAnalysis,
    AIUsageLog,
)


class AITestCaseDraftInline(admin.TabularInline):
    model = AITestCaseDraft
    extra = 0
    readonly_fields = (
        "case_number",
        "summary",
        "priority",
        "test_type",
        "imported_case",
        "created",
    )


@admin.register(AIRequest)
class AIRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "created_by",
        "category",
        "has_analysis",
        "analyzed_at",
        "has_coverage",
        "coverage_analyzed_at",
        "created",
    )
    list_filter = ("category__product", "category")
    search_fields = ("title", "requirement")
    readonly_fields = (
        "analysis_raw",
        "analyzed_at",
        "coverage_raw",
        "coverage_analyzed_at",
        "created",
    )
    inlines = (AITestCaseDraftInline,)


@admin.register(AITestCaseDraft)
class AITestCaseDraftAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "case_number",
        "summary",
        "priority",
        "request",
        "imported_case",
    )
    list_filter = ("priority", "test_type")
    search_fields = ("case_number", "summary", "request__title")


@admin.register(AIModelConfig)
class AIModelConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "owner",
        "model",
        "is_active",
        "updated",
    )


@admin.register(AIInstructionProfile)
class AIInstructionProfileAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "owner",
        "product",
        "version",
        "is_active",
        "updated",
    )
    list_filter = ("is_active", "product")
    search_fields = ("name", "description", "instructions", "owner__username")
    readonly_fields = ("version", "created", "updated")


@admin.register(AITestCaseReview)
class AITestCaseReviewAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "test_case",
        "owner",
        "score",
        "model_config",
        "created",
        "applied_at",
    )
    list_filter = ("created", "applied_at", "model_config")
    search_fields = (
        "test_case__summary",
        "owner__username",
        "optimized_summary",
    )
    readonly_fields = (
        "raw_result",
        "original_summary",
        "original_text",
        "created",
        "applied_at",
        "applied_by",
    )


@admin.register(AITestRunAnalysis)
class AITestRunAnalysisAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "test_run",
        "owner",
        "model_config",
        "created",
    )
    list_filter = ("created", "model_config")
    search_fields = ("test_run__summary", "owner__username")
    readonly_fields = (
        "owner",
        "test_run",
        "model_config",
        "execution_snapshot",
        "result",
        "raw_result",
        "created",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AIDefectDraft)
class AIDefectDraftAdmin(admin.ModelAdmin):
    list_display = ("id", "execution", "owner", "status", "severity", "priority", "linked_reference", "updated")
    list_filter = ("status", "severity", "priority", "created")
    search_fields = ("title", "owner__username", "execution__case__summary")
    readonly_fields = ("raw_result", "created", "updated")


@admin.register(AITestReport)
class AITestReportAdmin(admin.ModelAdmin):
    list_display = ("id", "test_run", "owner", "version", "is_current", "approval_status", "release_decision", "updated")
    list_filter = ("is_current", "approval_status", "release_decision", "created")
    search_fields = ("title", "test_run__summary", "owner__username")
    readonly_fields = ("metrics_snapshot", "raw_result", "created", "updated")


@admin.register(AIRegressionVerification)
class AIRegressionVerificationAdmin(admin.ModelAdmin):
    list_display = ("id", "source_report", "defect_draft", "regression_run", "owner", "status", "created")
    list_filter = ("status", "created")
    search_fields = ("owner__username", "source_report__title")
    readonly_fields = ("owner", "source_report", "regression_run", "status", "result", "created")


admin.site.register(AIDefectStatusHistory)
admin.site.register(AITestReportRevision)
admin.site.register(AIReleaseGateRule)
admin.site.register(AIIterationReport)
admin.site.register(AIRequirementVersion)


@admin.register(AIJob)
class AIJobAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "operation", "status", "progress", "attempts", "created", "completed")
    list_filter = ("operation", "status", "created")
    search_fields = ("id", "owner__username", "dedupe_key")
    readonly_fields = (
        "id", "owner", "model_config", "operation", "payload", "result", "status",
        "progress", "stage", "error_message", "result_url", "dedupe_key", "attempts",
        "created", "started", "heartbeat", "completed",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AIUsageLog)
class AIUsageLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "owner",
        "operation",
        "config_name",
        "model_name",
        "status",
        "duration_ms",
        "total_tokens",
        "created",
    )
    list_filter = ("operation", "status", "created")
    search_fields = ("owner__username", "config_name", "model_name")
    readonly_fields = (
        "owner",
        "model_config",
        "config_name",
        "model_name",
        "operation",
        "status",
        "duration_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "error_message",
        "created",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
