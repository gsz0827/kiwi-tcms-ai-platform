"""Bounded, permission-checked case inventory export and atomic directory moves."""

import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from tcms.management.models import Product
from tcms.web_testing.models import WebCase
from . import roles
from .case_directories import COMMON, visible_folders
from .case_library import visible_cases
from .case_hub import _row
from .models import APICase, ProjectResourceAssignment as Assignment
from .project_context import return_url


def _selection(request, *, writable=False, lock=False):
    tokens = request.POST.getlist("selected")
    if not 1 <= len(tokens) <= 100:
        raise ValueError("请勾选 1–100 条用例；全选仅针对当前页。")
    parsed = []
    for token in tokens:
        kind, sep, value = token.partition(":")
        if not sep or kind not in {"manual", "web", "api"} or not value.isdigit() or len(value) > 18:
            raise ValueError("用例选择参数无效，请刷新后重试。")
        pair = (kind, int(value))
        if pair not in parsed:
            parsed.append(pair)
    if writable and (not request.user.is_active or roles.is_read_only(request.user)):
        raise PermissionDenied
    result = {}
    models = {
        "manual": visible_cases(request.user)
        .annotate(has_api=Exists(APICase.objects.filter(test_case_id=OuterRef("pk"))))
        .filter(is_automated=False, has_api=False)
        .select_related("category__product", "case_status"),
        "web": WebCase.objects.filter(owner=request.user).select_related("product"),
        "api": APICase.objects.filter(owner=request.user).select_related("product"),
    }
    # Validate the entire selection before any mutation. Raw IDs do not confer access.
    for kind, query in models.items():
        ids = [pk for selected_kind, pk in parsed if selected_kind == kind]
        if lock:
            query = query.select_for_update()
        objects = {obj.pk: obj for obj in query.filter(pk__in=ids)}
        if len(objects) != len(ids):
            from django.http import Http404

            raise Http404
        for pk, obj in objects.items():
            if (
                writable
                and kind == "manual"
                and not (
                    request.user.has_perm("testcases.change_testcase")
                    or request.user.has_perm("testcases.change_testcase", obj)
                )
            ):
                raise PermissionDenied
            result[(kind, pk)] = _row(kind, obj, request.user)
    return [result[pair] for pair in parsed]


def _csv_safe(value):
    value = str(value or "")
    # CSV quoting alone does not prevent spreadsheet formula execution.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


@login_required
@require_POST
def batch(request):
    action = request.POST.get("action")
    if action not in {"move", "unfile", "export"}:
        return HttpResponseBadRequest("不支持的用例操作。")
    try:
        if action == "export":
            rows = _selection(request)
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
            writer.writerow(("用例类型", "编号", "用例名称", "项目", "分类或测试内容", "详情路径"))
            labels = {"manual": "手工测试", "web": "Web 自动化测试", "api": "接口自动化测试"}
            for row in rows:
                writer.writerow(
                    [
                        _csv_safe(value)
                        for value in (
                            labels[row["kind"]],
                            row["number"],
                            row["name"],
                            row["product"].name,
                            row["detail"],
                            row["url"],
                        )
                    ]
                )
            # Inventory only: never decrypt/export request bodies, headers, variables or steps.
            response = HttpResponse(
                "\ufeff" + stream.getvalue(), content_type="text/csv; charset=utf-8"
            )
            response["Content-Disposition"] = 'attachment; filename="case-inventory.csv"'
            response["Cache-Control"] = "no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        with transaction.atomic():
            rows = _selection(request, writable=True, lock=True)
            product_ids = {row["product"].pk for row in rows}
            if len(product_ids) != 1:
                raise ValueError("批量整理请只勾选同一项目的用例。")
            product_id = product_ids.pop()
            Product.objects.select_for_update().get(pk=product_id)
            folder = None
            if action == "move":
                folder = get_object_or_404(
                    visible_folders(request.user).select_for_update(),
                    pk=request.POST.get("folder") if request.POST.get("folder", "").isdigit() else 0,
                )
                if folder.product_id != product_id or folder.resource_type != COMMON:
                    raise PermissionDenied
            for row in rows:
                identity = dict(resource_type=row["resource_type"], object_id=row["pk"])
                if folder:
                    Assignment.objects.update_or_create(
                        **identity, defaults=dict(folder=folder, assigned_by=request.user)
                    )
                else:
                    Assignment.objects.filter(**identity).delete()
        messages.success(request, f"已整理 {len(rows)} 条用例；未修改用例内容或执行结果。")
        return HttpResponseRedirect(return_url(request))
    except ValueError as error:
        messages.error(request, str(error))
        return HttpResponseRedirect(return_url(request))
