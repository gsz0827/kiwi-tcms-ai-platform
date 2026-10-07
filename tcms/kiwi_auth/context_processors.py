def ui_preferences(request):
    preference = getattr(request, "ui_preference", None)
    return {
        "UI_PREFERENCES": {
            "language": preference.language if preference else getattr(request, "LANGUAGE_CODE", "zh-hans"),
            "time_zone": preference.time_zone if preference else "Asia/Shanghai",
        }
    }
