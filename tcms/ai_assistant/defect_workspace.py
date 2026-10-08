"""A dedicated, permission-scoped defect and retest worklist."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from . import roles
from .models import AIDefectDraft
from .quality_metrics import filtered_runs


def selected_id(value):
    value = str(value or "")
    return int(value) if value.isascii() and value.isdecimal() and len(value) <= 18 else None


@login_required
def index(request):
    product_id = selected_id(request.GET.get("product", request.session.get("ai_product_id")))
    version_id = selected_id(request.GET.get("version", request.session.get("ai_version_id")))
    plan_id = selected_id(request.GET.get("plan"))
    runs = filtered_runs(request.user, product_id, version_id, plan_id)
    drafts = roles.visible_defects(request.user)
    if product_id:
        drafts = drafts.filter(execution__run__plan__product_id=product_id)
    if version_id:
        drafts = drafts.filter(execution__run__build__version_id=version_id)
    if plan_id:
        drafts = drafts.filter(execution__run__plan_id=plan_id)
    status = request.GET.get("status", "")
    if status:
        drafts = drafts.filter(status=status) if status in dict(AIDefectDraft.STATUS_CHOICES) else drafts.none()
    q = request.GET.get("q", "").strip()[:200]
    if q:
        drafts = drafts.filter(title__icontains=q)
    page = Paginator(drafts.select_related("execution__case", "execution__run", "linked_reference")
                     .prefetch_related("regression_verifications").order_by("-updated", "-pk"), 30).get_page(request.GET.get("page"))
    for draft in page:
        draft.latest_verification = max(draft.regression_verifications.all(), key=lambda row: row.created, default=None)
    params = request.GET.copy()
    params.pop("page", None)
    failures = runs.values_list("pk", flat=True)
    from tcms.testruns.models import TestExecution
    failed_executions = TestExecution.objects.filter(run_id__in=failures, status__weight__lt=0).select_related("case", "run")
    return render(request, "ai_assistant/defect_workspace.html", {
        "drafts": page, "failed_executions": failed_executions.order_by("-pk")[:30],
        "failure_count": failed_executions.count(), "statuses": AIDefectDraft.STATUS_CHOICES,
        "status_filter": status, "q": q, "pagination_query": params.urlencode(),
    })
