from django.conf import settings
from django.db import models


class UserPreference(models.Model):
    LANGUAGE_CHOICES = (("zh-hans", "简体中文"), ("en", "English"))
    TIME_ZONE_CHOICES = (("Asia/Shanghai", "北京时间（UTC+8）"), ("Etc/UTC", "协调世界时（UTC）"))

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ui_preference",
        verbose_name="账号",
    )
    language = models.CharField(max_length=16, choices=LANGUAGE_CHOICES, default="zh-hans", verbose_name="界面语言")
    time_zone = models.CharField(max_length=64, choices=TIME_ZONE_CHOICES, default="Asia/Shanghai", verbose_name="显示时区")
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} · {self.get_language_display()} · {self.get_time_zone_display()}"
