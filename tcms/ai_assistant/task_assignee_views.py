"""Read-only assignee options scoped exactly like development-document authoring."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import roles


@login_required
@require_GET
@never_cache
def choices(request):
    requirement_id = request.GET.get("request", "")
    task_id = request.GET.get("task", "")
    if not requirement_id.isdigit() or (task_id and not task_id.isdigit()):
        return JsonResponse({"error": "参数无效"}, status=400)
    try:
        requirement_id = int(requirement_id)
        task_id = int(task_id) if task_id else None
    except ValueError:
        return JsonResponse({"error": "参数无效"}, status=400)
    if not 0 < requirement_id <= 9223372036854775807 or (
        task_id is not None and not 0 < task_id <= 9223372036854775807
    ):
        return JsonResponse({"error": "参数无效"}, status=400)
    requirement = get_object_or_404(
        roles.visible_requests(request.user).select_related("category__product"),
        pk=requirement_id,
    )
    if task_id:
        task = get_object_or_404(roles.visible_dev_tasks(request.user), pk=task_id)
        allowed = roles.can_edit_dev_task(request.user, task)
    else:
        allowed = roles.can_split_dev_tasks(request.user, requirement)
    if not request.user.is_active or not allowed:
        raise PermissionDenied
    product = requirement.category.product if requirement.category_id else None
    members = roles.assignable_users(product).order_by("username")
    return JsonResponse(
        {
            "request_id": requirement.pk,
            "product_id": product.pk if product else None,
            "assignees": [{"id": user.pk, "name": user.username} for user in members],
            'versions': [{'id':version.pk,'name':version.value} for version in product.version.all()] if product else [],
        }
    )
