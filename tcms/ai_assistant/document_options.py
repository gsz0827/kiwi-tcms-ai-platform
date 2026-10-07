"""Read-only scoped choices for requirement attributes."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET
from django.views.decorators.cache import never_cache
from tcms.testcases.models import Category
from . import roles


@login_required
@require_GET
@never_cache
def choices(request):
    category_id = request.GET.get("category", "")
    if (
        not category_id.isascii()
        or not category_id.isdigit()
        or len(category_id) > 18
        or int(category_id) < 1
    ):
        return JsonResponse({"error": "参数无效"}, status=400)
    category = get_object_or_404(Category.objects.select_related("product"), pk=int(category_id))
    if not request.user.is_active or not roles.can_submit_requirement(request.user):
        raise PermissionDenied
    visible_product = roles.member_products(request.user).filter(pk=category.product_id).exists() or roles.visible_requests(request.user).filter(category__product=category.product).exists()
    members = roles.assignable_users(category.product if visible_product else None).order_by('username')
    return JsonResponse(
        {
            "category_id": category.pk,
            "assignees": [
                {"id": user.pk, "name": user.username}
                for user in members
            ],
            "versions": [
                {"id": version.pk, "name": version.value}
                for version in category.product.version.all()
            ],
        }
    )
