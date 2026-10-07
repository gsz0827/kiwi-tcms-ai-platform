"""Project-scoped personal work, with account-level AI activity explicitly separate."""

from tcms.management.models import Product, Version
from tcms.web_testing.models import WebAIDraft
from .models import APIAIDraft


def project_scope(request):
    raw = request.GET.get("product", request.session.get("ai_product_id", ""))
    product = (
        Product.objects.filter(pk=int(raw)).first()
        if str(raw).isdigit() and len(str(raw)) <= 18
        else None
    )
    # Unknown/stale selections never widen to all projects silently.
    invalid = bool(raw) and product is None
    version_raw = request.GET.get("version", request.session.get("ai_version_id", ""))
    version = (
        Version.objects.filter(pk=int(version_raw), product=product).first()
        if product and str(version_raw).isdigit() and len(str(version_raw)) <= 18
        else None
    )
    return product, version, invalid


def search_data(request, *, version_field):
    """Explicit filter submissions override project defaults, including 'all projects'."""
    data = request.GET.copy()
    if "product" not in data:
        product, version, invalid = project_scope(request)
        data["product"] = str(product.pk) if product else ("0" if invalid else "")
        if version and version_field not in data:
            data[version_field] = str(version.pk)
    elif version_field == "product_version" and "version" in data and "product_version" not in data:
        # Top-bar project switching uses 'version'; the native plan form uses this name.
        data[version_field] = data["version"]
    return data


def automation_drafts(user, product, invalid):
    web = WebAIDraft.objects.filter(request__owner=user, imported_at__isnull=True)
    api = APIAIDraft.objects.filter(request__owner=user, imported_at__isnull=True)
    if invalid:
        web, api = web.none(), api.none()
    elif product:
        web, api = web.filter(request__product=product), api.filter(request__product=product)
    # Drafts refer to requirement revisions, not release Version records.
    return {
        "web_draft_count": web.count(),
        "api_draft_count": api.count(),
        "recent_web_drafts": web.select_related("request__product").order_by("-pk")[:3],
        "recent_api_drafts": api.select_related("request__product").order_by("-pk")[:3],
    }
