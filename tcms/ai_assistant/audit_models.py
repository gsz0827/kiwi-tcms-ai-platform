"""Append-only application journal; database administrators remain a trust boundary."""
from django.conf import settings
from django.db import models


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # Django's SET_NULL collector must retain records after account/project deletion.
        if kwargs and set(kwargs) <= {"actor", "product"} and all(value is None for value in kwargs.values()):
            return super().update(**kwargs)
        raise RuntimeError("审计记录不能修改。")

    def delete(self):
        raise RuntimeError("审计记录不能删除。")


class AIAuditLog(models.Model):
    ACTION_CHOICES = (
        ("permission_denied", "权限拒绝"), ("report_approve", "报告审批"),
        ("report_gate_waive", "风险放行"), ("report_gate_evaluate", "门禁评估"),
        ("release_gate_update", "门禁规则维护"), ("report_export", "报告导出"),
        ("member_add", "添加成员"), ("member_remove", "移除成员"),
        ("role_change", "角色变更"), ("folder_delete", "目录删除"),
        ("credential_update", "模型凭据维护"), ("project_rule_update", "AI 规则维护"),
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    actor_username = models.CharField(max_length=150, blank=True)
    actor_role = models.CharField(max_length=64, blank=True)
    product = models.ForeignKey("management.Product", null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64, choices=ACTION_CHOICES, db_index=True)
    result = models.CharField(max_length=16, choices=(("success", "成功"), ("denied", "拒绝"), ("failed", "失败")))
    target_kind = models.CharField(max_length=128, blank=True)
    target_id = models.CharField(max_length=128, blank=True)
    target_repr = models.CharField(max_length=255, blank=True)
    reason = models.TextField(blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip = models.CharField(max_length=45, null=True, blank=True)
    request_id = models.CharField(max_length=128, blank=True, db_index=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ("-created", "-pk")
        verbose_name = "操作审计"
        verbose_name_plural = "操作审计"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("审计记录不能修改。")
        if kwargs.get("force_update"):
            raise RuntimeError("审计记录不能修改。")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("审计记录不能删除。")
