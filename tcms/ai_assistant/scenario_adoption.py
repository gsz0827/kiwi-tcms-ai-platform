"""Explicit human confirmation of AI test designs before saving business scenarios."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from guardian.shortcuts import assign_perm
from . import roles
from .services import import_test_case_drafts


@login_required
@require_POST
@permission_required("testcases.add_testcase", raise_exception=True)
def confirm(request, pk):
    if roles.is_read_only(request.user) or not request.user.is_active:
        raise PermissionDenied
    source = get_object_or_404(roles.visible_requests(request.user), pk=pk)
    back = reverse("ai_assistant:index") + f"#request-{pk}"
    raw = request.POST.getlist("draft_ids")
    if request.POST.get("confirmed") not in {"on", "true", "1"}:
        messages.error(request, "请先核对所选用例的测试目标、步骤和预期结果，并勾选人工确认。")
        return redirect(back)
    if not raw or any(not value.isdigit() or len(value) > 18 for value in raw):
        messages.error(request, "请选择有效的本次生成草稿。")
        return redirect(back)
    if source.category_id is None:
        messages.error(request, "请先为需求选择业务分类，再确认保存用例。")
        return redirect(back)
    selected = set(map(int, raw))
    with transaction.atomic():
        source = roles.visible_requests(request.user).select_for_update().get(pk=source.pk)
        drafts = list(source.drafts.select_for_update().filter(pk__in=selected))
        if len(drafts) != len(selected) or any(
            d.needs_update or d.requirement_version != source.version for d in drafts
        ):
            messages.error(request, "所选草稿不属于此需求，或来源需求已变更；请复核更新后再保存。")
            return redirect(back)
        created = import_test_case_drafts(source, request.user, selected_draft_ids=selected)
        for case in created:
            for permission in ("view_testcase", "change_testcase"):
                assign_perm(permission, request.user, case)
    messages.success(
        request, f"已确认并保存 {len(created)} 条业务用例，后续可人工执行或按需配置自动化。"
    )
    return redirect(
        reverse("ai_assistant:scenario_library") + f"?product={source.category.product_id}"
    )
