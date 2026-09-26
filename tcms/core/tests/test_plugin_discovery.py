# Copyright (c) 2019-2026 Alexander Todorov <atodorov@otb.bg>

# Licensed under the GPL 2.0: https://www.gnu.org/licenses/old-licenses/gpl-2.0.html

from importlib.metadata import entry_points

from django.conf import settings
from django.test import TestCase
from django.urls.resolvers import URLResolver
from django.utils.translation import gettext_lazy as _

from tcms.core.templatetags.extra_filters import markdown2html
from tcms.telemetry.tests.plugin import menu as plugin_menu
from tcms.tests import LoggedInTestCase
from tcms.urls import urlpatterns


class PluginDiscoveryTestCase(TestCase):
    def test_installed_apps_is_updated(self):
        """
        Given there are some plugins installed
        Then validate the plugin module is added to INSTALLED_APPS
        """
        assertions_count = 0
        for plugin in entry_points().select(group="kiwitcms.plugins"):
            self.assertIn(plugin.value, settings.INSTALLED_APPS)
            assertions_count += 1

        self.assertGreater(assertions_count, 0)


class UrlDiscoveryTestCase(TestCase):
    def test_urlpatterns_is_updated(self):
        """
        Given there are some plugins installed
        Then validate urlpatterns:

            - ^<plugin-name>/ includes(<plugin-module-urls>)
        """
        for plugin in entry_points().select(group="kiwitcms.plugins"):
            for url_resolver in urlpatterns:
                if isinstance(url_resolver, URLResolver) and (
                    str(url_resolver.pattern) == f"^{plugin.name}/"
                    and url_resolver.urlconf_module.__name__ == f"{plugin.value}.urls"
                ):
                    return

        self.fail("No plugins found or urlpatterns not valid")


class MenuDiscoveryTestCase(LoggedInTestCase):
    def test_menu_is_updated(self):
        """
        Given there are some plugins installed
        Then navigation menu under MORE will be extended
        """
        for name, target in settings.MENU_ITEMS:
            if name == _("MORE"):
                for menu_item in plugin_menu.MENU_ITEMS:
                    self.assertIn(menu_item, target)

                return

        self.fail("MORE not found in settings.MENU_ITEMS")

    def test_menu_rendering(self):
        """
        Given there are some plugins installed
        Then the plugin entries are rendered inside the ``平台管理`` section of
            the Chinese sidebar, under a ``插件`` caption.

        This fork replaces the upstream horizontal menu with a Chinese sidebar and
        no longer projects ``settings.MENU_ITEMS`` onto the page, so plugin entries
        have to be collected at runtime instead. Plugin sub-menus keep one level of
        caption while deeper levels are flattened, because the sidebar is only
        232px wide. The request has to be authenticated because the sidebar only
        exists for logged-in users.
        """
        response = self.client.get("/", follow=True)
        self.assertContains(response, '<li class="kiwi-nav-caption">插件</li>', html=True)
        self.assertContains(response, "Fake Telemetry plugin")
        self.assertContains(response, "Fake Plugin sub-menu")
        self.assertContains(response, "Example")
        self.assertContains(response, "Go to Dashboard")
        self.assertContains(response, "Go to kiwitcms.org")
        self.assertContains(
            response,
            '<a href="/a_fake_plugin/example/">'
            '<span class="fa fa-puzzle-piece" aria-hidden="true"></span>Example</a>',
            html=True,
        )
        # 第三层分组标题（"3rd level menu"）被有意拍平，侧边栏不做三层缩进
        self.assertNotContains(response, "3rd level menu")


class MarkdownPluginTestCase(TestCase):
    def test_markdown_extension_works(self):
        result = markdown2html("""
This is the beginning of the text
```test-me
this is going to be discarded
```
this is the end of the text
            """)

        self.assertEqual(
            result,
            """<p>This is the beginning of the text<br>
Eat, Sleep, Test, Repeat<br>
this is the end of the text</p>""",
        )
