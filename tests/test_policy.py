"""policy + categories 单元测试:四层规则判定矩阵与角色合并语义。"""
import os
import tempfile
import unittest

from categories import tag
from policy import ALLOW, PolicyEngine

CFG = """[proxy]
listen_port = 18080

[audit]
db_path = "data/audit.db"

[policy]
whitelist_mode = false
whitelist = ["corp-doc.com"]
blacklist_domains = ["*.blocked-site.example"]
blacklist_keywords = ["forbidden-path"]
blacklist_url_regex = ["/secret/[0-9]+"]
disabled_categories = ["video"]
request_signatures = ["password\\\\s*="]
response_signatures = ["违规内容特征词"]

[cache]
enabled = false
"""

USERS = """[users.admin]
password = "admin123"
role = "admin"
[users.employee]
password = "emp123"
role = "employee"
"""

ROLES = """[roles.admin]
whitelist_mode = false
disabled_categories = []
request_signatures = []
response_signatures = []

[roles.employee]
whitelist_mode = false
disabled_categories = ["game", "shopping"]
"""


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        for name, text in (("config.toml", CFG), ("users.toml", USERS),
                           ("roles.toml", ROLES)):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        from config import load_config_dir
        self.engine = PolicyEngine(load_config_dir(self.dir))

    def tearDown(self):
        self._tmp.cleanup()

    def classify(self, role, url, headers_text=""):
        from http_message import parse_http_url
        host, port, path = parse_http_url(url)
        return self.engine.classify(host=host, path=path, url_text=url,
                                    headers_text=headers_text, role_name=role)


class TestCategories(unittest.TestCase):
    def test_tag_hits_and_misses(self):
        self.assertEqual(tag("game-site.com", "/"), {"game"})
        self.assertEqual(tag("shop.example", "/cart"), {"shopping"})
        self.assertEqual(tag("corp-doc.com", "/docs"), set())
        self.assertIn("video", tag("v.bilibili.com", "/"))

    def test_case_insensitive(self):
        self.assertEqual(tag("GAME-Site.com", "/"), {"game"})


class TestClassifyOrder(PolicyTestCase):
    """判定优先级:白名单模式 > 黑名单 > 类别 > 请求特征。"""

    def test_allow_when_no_rule_matches(self):
        d = self.classify("admin", "http://corp-doc.com/doc")
        self.assertEqual(d.action, "allow")

    def test_blacklist_domain(self):
        d = self.classify("admin", "http://www.blocked-site.example/x")
        self.assertEqual(d.action, "block")
        self.assertEqual(d.rule_type, "blacklist")
        # 全局红线:admin 也不能例外
        d2 = self.classify("admin", "http://blocked-site.example/x")
        self.assertEqual(d2.rule_type, "blacklist")

    def test_wildcard_needs_subdomain_or_exact(self):
        self.assertEqual(self.classify("admin", "http://evil.example/").action, "allow")

    def test_blacklist_keyword_and_regex(self):
        d = self.classify("admin", "http://corp-doc.com/forbidden-path/1")
        self.assertEqual(d.rule_type, "blacklist")
        d = self.classify("admin", "http://corp-doc.com/secret/42")
        self.assertEqual(d.rule_type, "blacklist")

    def test_category_by_role(self):
        # 未显式定义类别的角色(role_name=None)→ 回退全局:禁 video
        self.assertEqual(
            self.classify(None, "http://video.example.com/").rule_type, "category")
        # 角色禁 game/shopping:仅 employee(显式列表,覆盖全局)
        d = self.classify("employee", "http://game-site.com/")
        self.assertEqual(d.rule_type, "category")
        self.assertEqual(d.rule_id, "category:game")
        d2 = self.classify("employee", "http://shop.example/buy")
        self.assertEqual(d2.rule_type, "category")
        # employee 显式列表不含 video → 覆盖全局禁 video
        self.assertEqual(self.classify("employee", "http://video.example.com/").action, "allow")
        # admin 显式空列表 → 全部类别放行
        self.assertEqual(self.classify("admin", "http://shop.example/buy").action, "allow")

    def test_request_signature_role_merge(self):
        url = "http://corp-doc.com/login?password=123"
        # employee 缺省键 → 回退全局特征表 → 命中
        d = self.classify("employee", url)
        self.assertEqual(d.rule_type, "signature")
        # admin 显式空表 → 绕过
        self.assertEqual(self.classify("admin", url).action, "allow")

    def test_headers_text_matching(self):
        d = self.classify("employee", "http://corp-doc.com/",
                          headers_text="User-Agent: password=abc\r\n")
        self.assertEqual(d.rule_type, "signature")

    def test_undefined_role_falls_back_to_global(self):
        # 角色表完全未定义该角色 → 等同无覆盖,沿用全局特征表
        d = self.classify("ghost", "http://corp-doc.com/login?password=1")
        self.assertEqual(d.rule_type, "signature")
        body = "违规内容特征词"
        d2 = self.engine.check_response(role_name="ghost",
                                        content_type="text/html", body=body.encode())
        self.assertIsNotNone(d2)

    def test_whitelist_mode_via_role_flag(self):
        # 角色显式开启白名单模式后,非名单域名一律拦截
        path = os.path.join(self.dir, "roles.toml")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n[roles.strict]\nwhitelist_mode = true\n")
        self.engine.reload_if_changed()
        d = self.engine.classify(host="corp-doc.com", path="/", url_text="http://corp-doc.com/",
                                 headers_text="", role_name="strict")
        self.assertEqual(d.action, "allow")
        d2 = self.engine.classify(host="other.com", path="/", url_text="http://other.com/",
                                  headers_text="", role_name="strict")
        self.assertEqual(d2.rule_type, "whitelist")


class TestResponseCheck(PolicyTestCase):
    def test_response_signature_employee_blocked_admin_bypass(self):
        body = "今日特惠 <违规内容特征词> 详情见内页"
        d = self.engine.check_response(role_name="employee",
                                       content_type="text/html", body=body.encode())
        self.assertIsNotNone(d)
        self.assertEqual(d.rule_type, "resp_signature")
        self.assertIsNone(self.engine.check_response(
            role_name="admin", content_type="text/html", body=body.encode()))

    def test_non_text_skipped(self):
        body = "违规内容特征词".encode()
        self.assertIsNone(self.engine.check_response(
            role_name="employee", content_type="image/png", body=body))


if __name__ == "__main__":
    unittest.main()
