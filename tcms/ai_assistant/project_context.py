"""Keep project switching on the current, same-site page."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


def return_url(request, product=None, version=None, *, update_filters=False):
    target = request.POST.get("next") or request.META.get("HTTP_REFERER") or ""
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = reverse("core-views-index")
    parts = urlsplit(target)
    path = parts.path or reverse("core-views-index")
    if path == reverse("ai_assistant:set_project_context"):
        path = reverse("core-views-index")
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if update_filters:
        for key in ("page", "case_page", "folder", "category", "plan", "scope"):
            params.pop(key, None)
        params["product"] = str(product.pk) if product else ""
        params["version"] = str(version.pk) if version else ""
    return urlunsplit(("", "", path, urlencode(params), parts.fragment))
