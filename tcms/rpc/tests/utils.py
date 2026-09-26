# -*- coding: utf-8 -*-
# pylint: disable=attribute-defined-outside-init

import tcms_api
from django import test
from django.core.management import call_command

from tcms.tests import PermissionsTestMixin
from tcms.tests.factories import UserFactory
from tcms.utils.permissions import initiate_user_with_default_setups


def empty_database_before_snapshot_restore():
    """
    Empty the database before the migration snapshot is restored.

    ``APITestCase`` and ``APIPermissionsTestCase`` do not run migrations for
    every test, they share a snapshot of the data created by ``migrate`` plus
    ``post_migrate``. Transactional test cases which do *not* use
    ``serialized_rollback`` flush the database in ``_fixture_teardown()`` and
    that flush emits ``post_migrate`` again, so everything the signal handlers
    create - ``django_content_type`` rows, ``auth_permission`` rows and
    django-guardian's ``AnonymousUser`` - is inserted a second time, this time
    with new auto-increment primary keys.

    Deserialization saves objects by primary key and the re-created rows also
    occupy the unique natural keys of the snapshot, so restoring a snapshot on
    top of them aborts: the first collision is the ``name`` of our own role
    groups (``Duplicate entry 'AI 测试经理' for key 'name'``, SQLite says
    ``UNIQUE constraint failed``) because ``auth.Group`` is deserialized before
    ``auth.User``, whose ``AnonymousUser`` row collides next. This is the reason
    the RPC tests and the transactional tests could not run in one process.

    Only the default database is emptied: this project has a single database,
    which is also all ``_databases_names(include_mirrors=False)`` yields here.

    Flushing here - exactly what ``_fixture_teardown()`` does, but *without*
    emitting ``post_migrate`` again - makes the restore start from an empty
    database. It is cheap because the previous teardown already emptied it.
    """
    call_command(
        "flush",
        verbosity=0,
        interactive=False,
        database="default",
        reset_sequences=False,
        allow_cascade=False,
        inhibit_post_migrate=True,
    )


class APITestCase(test.LiveServerTestCase):
    # preserves data created via migrations
    serialized_rollback = True

    # NOTE: we create the required DB records here because
    # this method is executed *BEFORE* each test scenario!
    @classmethod
    def _fixture_setup(cls):
        empty_database_before_snapshot_restore()

        # restore the serialized data from initial migrations
        # this includes default groups and permissions
        super()._fixture_setup()

        cls.api_user = UserFactory()
        cls.api_user.set_password("api-testing")
        initiate_user_with_default_setups(cls.api_user)

    @property
    def rpc_client(self):
        return tcms_api.TCMS(
            f"{self.live_server_url}/xml-rpc/",
            self.api_user.username,
            "api-testing",
        ).exec


class APIPermissionsTestCase(PermissionsTestMixin, test.LiveServerTestCase):
    http_method_names = ["api"]
    permission_label = None
    serialized_rollback = True

    # NOTE: see comment in APITestCase._fixture_setup()
    @classmethod
    def _fixture_setup(cls):
        empty_database_before_snapshot_restore()

        # restore the serialized data from initial migrations
        # this includes default groups and permissions
        super()._fixture_setup()

        cls.check_mandatory_attributes()

        cls.tester = UserFactory()
        cls.tester.set_password("password")
        cls.tester.save()

    @property
    def rpc_client(self):
        return tcms_api.TCMS(
            f"{self.live_server_url}/xml-rpc/",
            self.tester.username,
            "password",
        ).exec

    def verify_api_with_permission(self):
        """
        Call your RPC method under test here and assert the results
        """
        self.fail("Not implemented")

    def verify_api_without_permission(self):
        """
        Call your RPC method under test here and assert the results
        """
        self.fail("Not implemented")
