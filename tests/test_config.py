"""config 模块单元测试:TOML 装载、默认值、角色缺省语义、热加载探测。"""
import os
import tempfile
import unittest

from config import ConfigError, RolePolicy, load_config_dir

# 一份最小的完整配置三件套(与根目录正式样例同构)
CFG = """[proxy]
listen_host = "127.0.0.1"
listen_port = 18080

[audit]
db_path = "data/audit.db"

[policy]
whitelist_mode = false
whitelist = ["corp-doc.com"]
blacklist_domains = ["*.blocked-site.example"]
request_signatures = ["password\\\\s*="]
response_signatures = ["违规内容特征词"]

[cache]
enabled = true
dir = "cache"
max_files = 500

block_page = "block_page.html"
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


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        for name, text in (("config.toml", CFG), ("users.toml", USERS),
                           ("roles.toml", ROLES)):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)

    def tearDown(self):
        self._tmp.cleanup()

    def load(self):
        return load_config_dir(self.dir)


class TestLoad(ConfigTestCase):
    def test_server_and_audit_fields(self):
        cfg = self.load()
        self.assertEqual(cfg.server.listen_host, "127.0.0.1")
        self.assertEqual(cfg.server.listen_port, 18080)
        self.assertTrue(cfg.server.auth_required)          # 缺省 True
        self.assertEqual(cfg.server.recv_timeout, 60.0)    # 缺省 60
        self.assertEqual(cfg.audit.db_path, "data/audit.db")
        self.assertTrue(cfg.cache.enabled)
        self.assertEqual(cfg.cache.max_files, 500)

    def test_policy_and_block_page(self):
        cfg = self.load()
        self.assertEqual(cfg.policy.blacklist_domains, ["*.blocked-site.example"])
        self.assertEqual(cfg.policy.request_signatures, [r"password\s*="])
        self.assertIn(b"__REASON__", cfg.block_page)  # 缺省模板带占位符

    def test_users_loaded(self):
        cfg = self.load()
        self.assertEqual(cfg.users["employee"].role, "employee")
        self.assertEqual(cfg.users["admin"].password, "admin123")

    def test_role_missing_keys_fallback_semantics(self):
        cfg = self.load()
        admin = cfg.roles["admin"]
        # 显式空列表 = 显式覆盖(绕过全局)
        self.assertEqual(admin.request_signatures, [])
        self.assertEqual(admin.response_signatures, [])
        # 缺省键 = None(回退全局)
        self.assertIsNone(cfg.roles["employee"].request_signatures)
        self.assertIsNone(cfg.roles["employee"].response_signatures)
        # 样例中 employee 显式写了 whitelist_mode=false → 以角色值为准
        self.assertFalse(cfg.roles["employee"].whitelist_mode)


class TestLoadErrors(unittest.TestCase):
    def test_missing_files_raise(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                load_config_dir(d)

    def test_invalid_toml_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.toml"), "w") as f:
                f.write("this is not toml [[[")
            with open(os.path.join(d, "users.toml"), "w") as f:
                f.write("")
            with open(os.path.join(d, "roles.toml"), "w") as f:
                f.write("")
            with self.assertRaises(ConfigError):
                load_config_dir(d)


class TestHotReloadProbe(ConfigTestCase):
    def test_is_changed_detects_file_touch(self):
        cfg = self.load()
        self.assertFalse(cfg.is_changed())
        path = os.path.join(self.dir, "users.toml")
        # 强制 mtime 变化
        with open(path, "a", encoding="utf-8") as f:
            f.write("# touch\n")
        self.assertTrue(cfg.is_changed())
        # 重新装载后恢复
        self.assertFalse(load_config_dir(self.dir).is_changed())


if __name__ == "__main__":
    unittest.main()
