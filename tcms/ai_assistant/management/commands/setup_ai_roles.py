from django.core.management.base import BaseCommand

from tcms.ai_assistant import roles


class Command(BaseCommand):
    help = "按 roles.ROLE_PERMISSION_MATRIX 对齐 AI 角色组与权限（幂等，migrate 后也会自动执行）"

    def handle(self, *args, **options):
        created, missing = roles.ensure_role_groups(verbosity=1)
        if created:
            self.stdout.write(f"新建角色组：{'、'.join(created)}")
        else:
            self.stdout.write("四个角色组都已存在，权限已按矩阵对齐")
        if missing:
            # 不报错：矩阵里可能写了尚未安装的应用的权限，跳过即可。
            self.stdout.write(self.style.WARNING(
                f"以下权限在本库中不存在，已跳过：{'、'.join(sorted(set(missing)))}"
            ))
        self.stdout.write(self.style.SUCCESS("AI 角色组已就绪"))
