from django.apps import AppConfig


class AiAssistantConfig(AppConfig):
    name = "tcms.ai_assistant"

    def ready(self):
        # 角色组（AI 测试经理 / 工程师 / 开发 / 只读）用 Django 用户组表达，不新建表。
        # 自定义权限要等 auth 的权限记录建好之后才存在，所以挂在 post_migrate 上：
        # 每次 migrate 结束都会按 roles.ROLE_PERMISSION_MATRIX 对齐一次，是幂等的。
        from django.db.models.signals import post_migrate

        from .roles import create_role_groups

        post_migrate.connect(
            create_role_groups,
            dispatch_uid="ai_assistant.create_role_groups",
        )
