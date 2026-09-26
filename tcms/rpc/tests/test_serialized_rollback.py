# -*- coding: utf-8 -*-
"""RPC 用例与事务型用例同进程运行时的快照恢复回归用例。

机制：普通 ``TransactionTestCase`` 每个用例结束都会 ``flush`` 清库，而 ``flush``
默认会重新发出 ``post_migrate``。``create_contenttypes``、``create_permissions``、
guardian 的 ``create_anonymous_user`` 与本平台的 ``ensure_role_groups()`` 都按名字
查找、查不到就以新的自增主键重建，于是库里留下「名字相同、主键与迁移快照不同」的
内容类型、权限、角色组与匿名用户。带 ``serialized_rollback = True`` 的类随后在
``setUpClass`` 里把迁移快照反序列化回库，``deserialize_db_from_string()`` 逐行**先按
主键 UPDATE、落空才 INSERT**，主键一变就撞自然唯一键：反序列化按 ``INSTALLED_APPS``
顺序进行，``django.contrib.auth`` 里 Group 排在 User 之前，所以先撞的是本平台的角色
组名「AI 测试经理」，SQLite 上更早撞 ``auth_user.username`` 的 ``AnonymousUser``。

本模块按 A → B → C 的顺序（类名首字母决定 ``dir()`` 顺序）验证修复：A 记录快照恢复
后的库指纹，B 用普通事务用例制造残留行，C 再次恢复快照并断言指纹不变。没有
``tcms/rpc/tests/utils.py`` 里的 ``empty_database_before_snapshot_restore()`` 时，
C 会在 ``setUpClass`` 阶段直接 ``IntegrityError``。
"""
from django import test
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models import Max
from guardian.utils import get_anonymous_user

from tcms.rpc.tests.utils import APITestCase


def _anonymous_users():
    user_model = get_user_model()
    username = getattr(settings, "ANONYMOUS_USER_NAME", "AnonymousUser")
    return user_model.objects.filter(**{user_model.USERNAME_FIELD: username})


def _database_fingerprint():
    return {
        "anonymous_user_pk": get_anonymous_user().pk,
        "content_types": ContentType.objects.count(),
        "max_content_type_pk": ContentType.objects.aggregate(max_pk=Max("pk"))["max_pk"],
        "permissions": Permission.objects.count(),
    }


# 模块级缓存：由 A 写入，C 读取
_SNAPSHOT_FINGERPRINT = {}


class ASnapshotFingerprintTests(APITestCase):
    """记录迁移快照恢复后的库指纹。类名以 A 开头，保证在本模块中最先运行。"""

    def test_records_the_fingerprint_of_the_migration_snapshot(self):
        self.assertEqual(_anonymous_users().count(), 1)
        _SNAPSHOT_FINGERPRINT.update(_database_fingerprint())


class BPlainTransactionalTestCase(test.TransactionTestCase):
    """普通事务用例自己什么都不做，是它的 teardown flush 把库弄脏的。"""

    def test_leaves_recreated_rows_behind(self):
        self.assertTrue(True)


class CRestoreAfterPlainTransactionalTests(APITestCase):
    """残留行存在时快照仍能原样恢复——修复前这里会在 setUpClass 抛 IntegrityError。"""

    def test_snapshot_restores_identically_after_a_plain_transactional_test(self):
        self.assertTrue(
            _SNAPSHOT_FINGERPRINT, "ASnapshotFingerprintTests 必须先运行"
        )
        self.assertEqual(_anonymous_users().count(), 1)
        self.assertEqual(_database_fingerprint(), _SNAPSHOT_FINGERPRINT)
