"""Append-only evidence for an explicit source recheck (not a formal approval)."""

from django.conf import settings
from django.db import models


class DocumentSourceReview(models.Model):
    request = models.ForeignKey(
        "ai_assistant.AIRequest", on_delete=models.CASCADE, related_name="source_reviews"
    )
    task = models.ForeignKey(
        "ai_assistant.AIDevTask",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="source_reviews",
    )
    case = models.ForeignKey(
        "testcases.TestCase",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="source_reviews",
    )
    requirement_version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created", "-pk")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(task__isnull=False, case__isnull=True)
                    | models.Q(task__isnull=True, case__isnull=False)
                ),
                name="source_review_exactly_one_target",
            )
        ]
