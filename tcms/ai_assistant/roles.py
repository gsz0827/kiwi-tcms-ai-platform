"""角色与权限：本平台谁能看什么、谁能改什么。

规则集中在这里，视图与查询集只调用本模块的函数，不再各自写 ``filter(owner=...)``。

两个维度分开：

* **角色**（Django Group + ``has_perm``）：决定「能做什么动作」——拆分开发任务、指派、
  审批报告、管理成员。角色的权限由每次 ``migrate`` 之后的 ``create_role_groups``
  按 ``ROLE_PERMISSION_MATRIX`` 对齐，不需要数据迁移；矩阵就是这个文件里的那张表，
  管理员在 Django admin 里的手工调整会在下次 migrate 时被覆盖回矩阵。
* **项目成员**（django-guardian 的对象权限）：决定「能看谁的东西」。给某人授予某个
  项目的 ``management.view_product`` 对象权限，他就是该项目的成员；项目成员互相可见
  需求、开发任务、报告、缺陷与迭代报告。

有意保持私有的数据：``AIModelConfig``（含 API Key）、``AIUsageLog``、``AIJob``、
接口自动化的环境与凭据。这些永远只属于创建者本人，不随角色或成员关系放开。
"""

from django.contrib.auth import get_user_model
from django.db.models import Q
from guardian.shortcuts import (
    assign_perm,
    get_objects_for_user,
    get_users_with_perms,
    remove_perm,
)

from tcms.management.models import Product

from .models import (
    AIDefectDraft,
    AIDevTask,
    AIIterationReport,
    AIRegressionVerification,
    AIRequest,
    AITestCaseReview,
    AITestReport,
    AITestRunAnalysis,
)

# --- 角色组 -----------------------------------------------------------------

ROLE_MANAGER = "AI 测试经理"
ROLE_ENGINEER = "AI 测试工程师"
ROLE_DEVELOPER = "AI 开发"
ROLE_VIEWER = "AI 只读"

ROLE_GROUPS = (ROLE_MANAGER, ROLE_ENGINEER, ROLE_DEVELOPER, ROLE_VIEWER)

# 表单与模板都按 (值, 显示名) 的二元组遍历，这里两者相同，保留结构是为了将来
# 若把角色名改成英文组名时不必再改一遍模板。
ROLE_CHOICES = tuple((name, name) for name in ROLE_GROUPS)

ROLE_DESCRIPTIONS = {
    ROLE_MANAGER: "提交与拆分需求、指派开发任务、审批项目内的测试报告、管理项目成员与角色。",
    ROLE_ENGINEER: "提交需求、拆分与编辑自己需求下的开发任务、维护用例草稿与测试报告。",
    ROLE_DEVELOPER: "查看所在项目的需求与开发任务，更新指派给自己的开发任务状态。",
    ROLE_VIEWER: "只读：可以查看所在项目的需求与开发任务，不能提交或修改任何内容。",
}

# --- 能力（权限字符串） -----------------------------------------------------

PERM_SPLIT_DEV_TASK = "ai_assistant.split_devtask"
PERM_ASSIGN_DEV_TASK = "ai_assistant.assign_devtask"
PERM_APPROVE_REPORT = "ai_assistant.approve_aireport"
PERM_MANAGE_MEMBERS = "ai_assistant.manage_members"
PERM_MANAGE_REQUIREMENT = "ai_assistant.manage_requirement"

# 项目成员判定所用的对象权限：Product 是上游模型，本平台直接借用它的 view 权限，
# 好处是不需要额外的成员表，且 guardian 的授权记录在 admin 里可以直接看到。
MEMBERSHIP_PERMISSION = "management.view_product"
# guardian 的 get_users_with_perms(only_with_perms_in=...) 只认 codename，传全名会
# 静默返回空集合（没有报错），所以这里单列一个常量，别再把全名传进去。
MEMBERSHIP_CODENAME = MEMBERSHIP_PERMISSION.split(".", 1)[1]

# 通用原则：**共享资产**（需求、开发任务、报告）按角色判定，但一律保留「本人」这条
# 后路——创建者/作者对自己的东西始终有权限。单人使用平台时不会被自己的权限体系
# 锁在门外；团队使用时角色决定他能碰别人多少东西。个人资产（模型配置、调用记录、
# 作业、接口自动化凭据）不参与角色判定，永远只属于本人。


def roles_of(user):
    """返回用户当前所属的角色组名集合。"""
    if not user.is_authenticated:
        return set()
    return set(user.groups.filter(name__in=ROLE_GROUPS).values_list("name", flat=True))


def is_read_only(user):
    """只属于「只读」角色：没有任何写入能力。"""
    return roles_of(user) == {ROLE_VIEWER}


def can_submit_requirement(user):
    """能不能提交/分析需求。未分配角色的用户与今天行为一致（允许）。"""
    return user.is_authenticated and not is_read_only(user)


def can_split_dev_tasks(user, ai_request=None):
    """能不能把需求拆成开发任务：测试经理可拆项目内任何需求，提出人可拆自己的需求。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if user.has_perm(PERM_SPLIT_DEV_TASK):
        return True
    if ai_request is None:
        return True
    return ai_request.created_by_id == user.pk


def can_generate_cases(user, ai_request=None):
    """能不能给需求生成测试用例草稿：需求提出人，或拥有「新增测试用例」的角色。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if user.has_perm("testcases.add_testcase"):
        return True
    if ai_request is None:
        return False
    return ai_request.created_by_id == user.pk


def can_assign_dev_tasks(user, ai_request=None):
    """能不能指派开发任务给他人：经理可指派项目内开发任务，需求提出人可指派自己需求下的。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if user.has_perm(PERM_ASSIGN_DEV_TASK):
        return True
    if ai_request is None:
        return False
    return ai_request.created_by_id == user.pk


def can_edit_requirement(user, ai_request):
    """能不能编辑或删除需求：本人，或拥有需求管理权限的角色。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if ai_request.created_by_id == user.pk:
        return True
    return user.has_perm(PERM_MANAGE_REQUIREMENT)


def can_edit_dev_task(user, task):
    """能不能改动开发任务的全部字段（标题、模块、验收标准、工时、优先级、负责人）。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if user.has_perm(PERM_ASSIGN_DEV_TASK):
        return True
    if task.owner_id == user.pk:
        return True
    return task.request.created_by_id == user.pk


def can_update_dev_task_status(user, task):
    """能不能更新开发任务状态：负责人对自己名下的开发任务有这个最小能力。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if can_edit_dev_task(user, task):
        return True
    return task.assignee_id == user.pk


def can_delete_dev_task(user, task):
    """能不能删除开发任务：经理可以删任何一条，其他人只能删自己建的那条。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if user.has_perm(PERM_ASSIGN_DEV_TASK):
        return True
    return task.owner_id == user.pk


def can_approve_report(user, report=None):
    """能不能审批测试报告：经理可审批项目内任何报告，作者始终可以审批自己的。"""
    if not user.is_authenticated or is_read_only(user):
        return False
    if user.has_perm(PERM_APPROVE_REPORT):
        return True
    if report is None:
        return False
    return report.owner_id == user.pk


def can_manage_members(user):
    """能不能维护项目成员与角色分配。"""
    return user.has_perm(PERM_MANAGE_MEMBERS) or user.is_staff


def can_manage_release_gate(user):
    """能不能维护发布门禁规则、以及给未通过门禁的报告做风险放行。

    门禁是项目的发布政策，只有测试经理（``PERM_APPROVE_REPORT``）能改。这里**有意
    不留「本人」后路**：报告作者不能给自己放行，否则硬门禁等于没有。单人部署时
    管理员是 superuser/staff，仍然进得去。
    """
    if not user.is_authenticated:
        return False
    return user.has_perm(PERM_APPROVE_REPORT) or user.is_staff


# --- 项目成员 ---------------------------------------------------------------


def member_products(user):
    """用户作为成员加入的项目集合。"""
    if not user.is_authenticated:
        return Product.objects.none()
    if user.is_superuser:
        return Product.objects.all()
    return get_objects_for_user(
        user, MEMBERSHIP_PERMISSION, klass=Product, accept_global_perms=True
    )


def is_product_member(user, product):
    if product is None:
        return False
    return member_products(user).filter(pk=product.pk).exists()


def add_product_member(user, product, granted_by=None):
    assign_perm(MEMBERSHIP_PERMISSION, user, product)
    return user


def remove_product_member(user, product):
    remove_perm(MEMBERSHIP_PERMISSION, user, product)
    return user


def set_user_roles(user, role_names):
    """整体替换用户的角色组（只动 ROLE_GROUPS 里的组，不碰其他组）。"""
    from django.contrib.auth.models import Group

    wanted = [name for name in role_names if name in ROLE_GROUPS]
    stale = [group for group in user.groups.filter(name__in=ROLE_GROUPS) if group.name not in wanted]
    if stale:
        user.groups.remove(*stale)
    if wanted:
        # add() 只认 Group 实例（或主键），传组名会 AttributeError。
        user.groups.add(*Group.objects.filter(name__in=wanted))
    return user


def assignable_users(product):
    """某个项目里可以被指派开发任务的人：该项目的成员（含超管）。"""
    user_model = get_user_model()
    if product is None:
        return user_model.objects.none()
    return user_model.objects.filter(
        Q(pk__in=member_user_ids_for_product(product)) | Q(is_superuser=True)
    ).distinct()


def member_user_ids_for_product(product):
    """某个项目的成员 id 集合（与 ``member_products`` 方向相反的查询）。"""
    return get_users_with_perms(
        product, only_with_perms_in=[MEMBERSHIP_CODENAME], with_superusers=False
    ).values_list("pk", flat=True)


def members_of_product(product):
    """某个项目的成员用户列表（按用户名排序）。"""
    if product is None:
        return get_user_model().objects.none()
    return get_user_model().objects.filter(
        pk__in=member_user_ids_for_product(product)
    ).order_by("username")


# --- 可见范围 ---------------------------------------------------------------


def visible_requests(user):
    """需求：自己提的 + 所在项目的全部需求。"""
    if not user.is_authenticated:
        return AIRequest.objects.none()
    if user.is_superuser:
        return AIRequest.objects.all()
    return AIRequest.objects.filter(
        Q(created_by=user) | Q(category__product__in=member_products(user))
    )


def visible_dev_tasks(user):
    """开发任务：可见需求下的全部开发任务 + 指派给自己的（即使需求不可见）。"""
    if not user.is_authenticated:
        return AIDevTask.objects.none()
    if user.is_superuser:
        return AIDevTask.objects.all()
    return AIDevTask.objects.filter(
        Q(request__in=visible_requests(user)) | Q(assignee=user)
    )


def visible_analyses(user):
    if not user.is_authenticated:
        return AITestRunAnalysis.objects.none()
    return AITestRunAnalysis.objects.filter(
        Q(owner=user) | Q(test_run__plan__product__in=member_products(user))
    )


def visible_reports(user):
    if not user.is_authenticated:
        return AITestReport.objects.none()
    return AITestReport.objects.filter(
        Q(owner=user) | Q(test_run__plan__product__in=member_products(user))
    )


def visible_defects(user):
    # 注意：缺陷草稿挂的是 TestExecution，而 TestExecution 指向执行任务的外键叫
    # ``run``（不是 ``test_run``）——写错会直接 FieldError。
    if not user.is_authenticated:
        return AIDefectDraft.objects.none()
    return AIDefectDraft.objects.filter(
        Q(owner=user) | Q(execution__run__plan__product__in=member_products(user))
    )


def visible_verifications(user):
    """缺陷复测记录：本人的，或落在自己参与的项目里的。"""
    if not user.is_authenticated:
        return AIRegressionVerification.objects.none()
    products = member_products(user)
    return AIRegressionVerification.objects.filter(
        Q(owner=user)
        | Q(source_report__test_run__plan__product__in=products)
        | Q(defect_draft__execution__run__plan__product__in=products)
    )


def visible_iterations(user):
    if not user.is_authenticated:
        return AIIterationReport.objects.none()
    return AIIterationReport.objects.filter(
        Q(owner=user) | Q(product__in=member_products(user))
    )


def visible_reviews(user):
    if not user.is_authenticated:
        return AITestCaseReview.objects.none()
    return AITestCaseReview.objects.filter(
        Q(owner=user) | Q(test_case__category__product__in=member_products(user))
    )


# --- 角色组与权限矩阵 -------------------------------------------------------


# 每个角色拿到的权限。空元组表示「没有任何写权限」，只是用来标注意图。
# ``testcases.*`` 是上游 Kiwi TCMS 的权限，用例草稿导入要它；没有这两条时
# 工程师连自己的草稿都导不进用例库。
ROLE_PERMISSION_MATRIX = {
    ROLE_MANAGER: (
        PERM_SPLIT_DEV_TASK,
        PERM_ASSIGN_DEV_TASK,
        PERM_APPROVE_REPORT,
        PERM_MANAGE_MEMBERS,
        PERM_MANAGE_REQUIREMENT,
        "testcases.add_testcase",
        "testcases.change_testcase",
    ),
    ROLE_ENGINEER: (
        # 有意不给 PERM_SPLIT_DEV_TASK：工程师拆的是「自己提的需求」，
        # 靠 can_split_dev_tasks 里的本人后路通过，而不是拿全局权限。
        "testcases.add_testcase",
        "testcases.change_testcase",
    ),
    ROLE_DEVELOPER: (),
    ROLE_VIEWER: (),
}


def ensure_role_groups(verbosity=0):
    """按矩阵对齐四个角色组，返回 (新建的组名, 缺失的权限名)。

    幂等：组用 ``get_or_create``，权限用 ``set()`` 整体覆盖，所以重复执行只会
    把角色组的权限修正回矩阵定义的样子。管理员在 admin 里手工加过的权限会被这里
    覆盖掉——矩阵是唯一事实来源，改矩阵请改这个文件。
    """
    from django.contrib.auth.models import Group, Permission

    created = []
    missing = []
    for name in ROLE_GROUPS:
        group, was_created = Group.objects.get_or_create(name=name)
        if was_created:
            created.append(name)
        wanted = []
        for label in ROLE_PERMISSION_MATRIX.get(name, ()):
            app_label, _, codename = label.partition(".")
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if permission is None:
                missing.append(label)
            else:
                wanted.append(permission)
        group.permissions.set(wanted)
        if verbosity:
            print(f"  角色 {name}: {len(wanted)} 项权限")
    return created, missing


def create_role_groups(sender=None, **kwargs):
    """``post_migrate`` 接收器：权限记录刚建好，正好按矩阵对齐角色组。"""
    return ensure_role_groups()
