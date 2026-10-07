"""Only rename a visible, editable case; never rewrite execution configurations."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from tcms.web_testing.models import WebCase
from . import roles
from .case_library import visible_cases
from .models import APICase
from .project_context import return_url


@login_required
@require_POST
@never_cache
def rename(request, kind, pk):
    if not request.user.is_active or roles.is_read_only(request.user):
        raise PermissionDenied
    if kind not in {"manual", "web", "api"} or pk > 9223372036854775807:
        raise Http404
    with transaction.atomic():
        query = (
            visible_cases(request.user)
            if kind == "manual"
            else (WebCase if kind == "web" else APICase).objects.filter(owner=request.user)
        )
        case = get_object_or_404(query.select_for_update(), pk=pk)
        if kind == "manual" and not (
            request.user.has_perm("testcases.change_testcase")
            or request.user.has_perm("testcases.change_testcase", case)
        ):
            raise PermissionDenied
        field = "summary" if kind == "manual" else "name"
        before = getattr(case, field)
        name = request.POST.get("name", "").strip()
        maximum = 255 if kind == "manual" else 200
        error = ""
        if not name or len(name) > maximum:
            error = f"用例名称不能为空且不能超过 {maximum} 个字符。"
        elif request.POST.get("expected_name") != before:
            error = "用例名称已在其他页面更新，请刷新后再重命名；本次没有覆盖名称。"
        if error:
            return render(
                request,
                "ai_assistant/tree_action_error.html",
                {
                    "error": error,
                    "back_url": return_url(request),
                },
                status=409,
            )
        if name != before:
            setattr(case, field, name)
            case._history_user = request.user
            case.save(update_fields=(field,) if kind == "manual" else (field, "updated"))
    messages.success(request, "用例名称已更新；步骤、参数、目录归属和历史执行记录保持不变。")
    return HttpResponseRedirect(return_url(request))
