"""Project publication pointers and immutable rule revisions; legacy profiles remain intact."""
from django.conf import settings
from django.db import models


class ProjectAIRuleBinding(models.Model):
    product = models.OneToOneField("management.Product", on_delete=models.CASCADE,
                                  related_name="ai_rule_binding")
    profile = models.OneToOneField("ai_assistant.AIInstructionProfile", on_delete=models.PROTECT,
                                  related_name="project_binding")
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL)
    updated = models.DateTimeField(auto_now=True)


class AIInstructionRevision(models.Model):
    profile = models.ForeignKey("ai_assistant.AIInstructionProfile", on_delete=models.CASCADE,
                                related_name="revisions")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-version",)
        constraints = [models.UniqueConstraint(fields=("profile", "version"),
                                               name="unique_ai_instruction_revision")]
