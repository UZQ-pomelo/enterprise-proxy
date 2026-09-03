"""M10 管理 CLI 测试:admin 渲染函数(纯文本行)与子命令装配。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin
from audit import AuditLog

# 样本:三行跨两天,含 allow/block_category/block_blacklist/auth_fail
BASE = dict(method="GET", role="employee", port=18001,
            rule_id="", reason="", resp_status=200, cache_hit=0,
            duration_ms=10)
RECS = [
    dict(BASE, ts="2026-09-03T10:00:00", user="employee", host="corp-doc.com",
         url_path="/doc", decision="allow", bytes_up=120, bytes_down=5000),
    dict(BASE, ts="2026-09-03T10:01:00", user="employee", host="game-site.com",
         url_path="/", decision="block_category", rule_id="category:game",
         reason="站点类别 [game] 已被角色 employee 禁用", resp_status=403,
         bytes_up=120, bytes_down=5000),
    dict(BASE, ts="2026-09-03T10:02:00", user="manager", role="manager",
         host="blocked-site.example", url_path="/x", decision="block_blacklist",
         rule_id="domain:*.blocked-site.example", reason="全局黑名单",
         bytes_up=80, bytes_down=200, resp_status=403),
    dict(BASE, ts="2026-09-04T09:00:00", user=None, role=None,
         host="corp-doc.com", url_path="/", decision="auth_fail",
         bytes_up=60, bytes_down=1000, resp_status=407),
]


class AdminFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.db = os.path.join(self.dir, "audit.db")
        self.log = AuditLog(self.db)
        for r in RECS:
            self.log.add(r)
        self.log.close()

    def tearDown(self):
        self._tmp.cleanup()

    def render(self, fn, *args, **kw):
        return "\n".join(fn(*args, **kw))


class TestRenderReport(AdminFixture):
    def test_report_totals_and_decisions(self):
        out = self.render(admin.render_report, self.db)
        self.assertRegex(out, r"请求总数\s+4")
        # 上、下行合计:up=120+120+80+60=380 B;down=5000+5000+200+1000=11200
        self.assertIn("380 B", out)
        self.assertIn("10.9 KB", out)
        for label in ("放行", "认证失败", "拦截·黑名单", "拦截·站点类别"):
            self.assertIn(label, out)

    def test_report_day_filter(self):
        out = self.render(admin.render_report, self.db, day="2026-09-03")
        self.assertRegex(out, r"请求总数\s+3")
        self.assertNotIn("认证失败", out)   # 09-04 的行被过滤


class TestRenderTop(AdminFixture):
    def test_top_users(self):
        out = self.render(admin.render_top, self.db, "user")
        lines = out.splitlines()
        head = lines[1] if len(lines) > 1 else lines[0]
        self.assertTrue(head.startswith(" 1. employee"),
                        f"employee 下行 10000B 应居首,实际: {head}")
        self.assertIn("9.8 KB", head)

    def test_top_hosts_day_filtered(self):
        out = self.render(admin.render_top, self.db, "host", day="2026-09-04")
        self.assertIn("corp-doc.com", out)
        self.assertNotIn("game-site.com", out)


class TestRenderBlocked(AdminFixture):
    def test_blocked_rows_newest_first(self):
        out = self.render(admin.render_blocked, self.db)
        lines = [l for l in out.splitlines() if l.startswith("10:0")]
        self.assertEqual(len(lines), 2)
        self.assertIn("blocked-site.example", lines[0])   # 10:02 最新在前
        self.assertIn("domain:*.blocked-site.example", lines[0])
        self.assertIn("game-site.com", lines[1])
        self.assertIn("category:game", lines[1])
        self.assertIn("站点类别", out)                     # reason 原文可见
        self.assertIn("employee", out)
        self.assertIn("manager", out)

    def test_blocked_day_filter(self):
        out = self.render(admin.render_blocked, self.db, day="2026-09-03")
        self.assertIn("category:game", out)
        self.assertIn("domain:*.blocked-site.example", out)
        self.assertNotIn("auth_fail", out)


class TestRenderCacheStats(unittest.TestCase):
    def render(self, fn, *args, **kw):
        return "\n".join(fn(*args, **kw))

    def test_scans_body_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            for n, size in (("aa", 100), ("bb", 250)):
                os.makedirs(os.path.join(d, n), exist_ok=True)
                with open(os.path.join(d, n, n + ".body"), "wb") as f:
                    f.write(b"x" * size)
            with open(os.path.join(d, "aa", "aa.meta.json"), "w") as f:
                f.write("{}")     # 非 .body 不应计数
            out = self.render(admin.render_cache_stats, d)
            self.assertRegex(out, r"缓存文件数\s+2")
            self.assertIn("350 B", out)

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.render(admin.render_cache_stats, d)
            self.assertRegex(out, r"缓存文件数\s+0")


class TestCliAssembly(AdminFixture):
    def test_main_report_exits_zero(self):
        self.assertEqual(admin.main(["report", self.db]), 0)

    def test_main_top_users_exits_zero(self):
        self.assertEqual(admin.main(["top-users", self.db]), 0)

    def test_main_blocked_with_day(self):
        self.assertEqual(admin.main(["blocked", self.db, "--day",
                                     "2026-09-03"]), 0)

    def test_unknown_command_fails(self):
        self.assertEqual(admin.main(["frobnicate", self.db]), 2)


if __name__ == "__main__":
    unittest.main()
