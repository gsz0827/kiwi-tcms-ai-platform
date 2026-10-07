from django.utils import translation, timezone

from .models import UserPreference


class UserDisplayPreferenceMiddleware:
    """Apply account-owned language and timezone, restoring both after each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        previous = translation.get_language()
        previous_zone = timezone.get_current_timezone()
        request.ui_preference = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            request.ui_preference = UserPreference.objects.filter(user=user).first()
            if request.ui_preference is not None:
                translation.activate(request.ui_preference.language)
                request.LANGUAGE_CODE = request.ui_preference.language
            timezone.activate(
                request.ui_preference.time_zone if request.ui_preference else "Asia/Shanghai"
            )
        try:
            response = self.get_response(request)
            if request.ui_preference is not None:
                response["Content-Language"] = request.ui_preference.language
            return response
        finally:
            translation.activate(previous)
            timezone.activate(previous_zone)
