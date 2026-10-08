"""Switch to a project workbench; validation errors stay on a safe current page."""
from urllib.parse import urlencode, urlsplit, urlunsplit
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


def return_url(request, product=None, version=None, *, update_filters=False):
    if update_filters:
        return reverse("core-views-index") + "?" + urlencode({
            "product": str(product.pk) if product else "",
            "version": str(version.pk) if version else "",
        })
    target = request.POST.get("next") or request.META.get("HTTP_REFERER") or ""
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = reverse("core-views-index")
    parts = urlsplit(target)
    path = parts.path or reverse("core-views-index")
    if path == reverse("ai_assistant:set_project_context"):
        path = reverse("core-views-index")
    return urlunsplit(("", "", path, parts.query, parts.fragment))
