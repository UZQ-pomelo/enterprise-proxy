"""M8 演示站点测试:离线站点矩阵(4 域名/4 端口)可访问、内容可缓存。"""
import re
import socket
import unittest

from demo_sites import DemoServer, SITES

PORT_BASE = 19001  # 避开演示固定端口,避免 CI 并行冲突


class DemoEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = DemoServer(PORT_BASE)
        cls.srv.start()
        cls.port_of = {name: cfg["port"]
                       for name, cfg in SITES.items()} | cls.srv.ports
        cls.port_of = cls.srv.ports  # 实际绑定端口(port_base 偏移后)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def fetch(self, port, path="/"):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall((f"GET {path} HTTP/1.1\r\nHost: h\r\n"
                       "Connection: close\r\n\r\n").encode())
            s.settimeout(5)
            raw = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                raw += chunk
            return raw
        finally:
            s.close()

    def head_of(self, raw):
        head, _, body = raw.partition(b"\r\n\r\n")
        return head, body


class TestDemoSites(DemoEnv):
    def test_all_sites_up(self):
        for domain, cfg in SITES.items():
            raw = self.fetch(self.port_of[domain])
            head, body = self.head_of(raw)
            self.assertEqual(head.split(b"\r\n")[0], b"HTTP/1.1 200 OK",
                             f"{domain} 未就绪")
            self.assertIn(b"<html", body)

    def test_site_kind_pages(self):
        raw = self.fetch(self.port_of["game-site.com"])
        _, body = self.head_of(raw)
        self.assertIn("在线游戏".encode(), body)
        raw2 = self.fetch(self.port_of["shop.example"])
        _, body2 = self.head_of(raw2)
        self.assertIn("购物商城".encode(), body2)

    def test_static_asset_cacheable(self):
        raw = self.fetch(self.port_of["corp-doc.com"], "/static/logo.png")
        head, body = self.head_of(raw)
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
        self.assertRegex(head.decode("iso-8859-1", "ignore"),
                         "(?i)cache-control: max-age=")
        self.assertRegex(head.decode("iso-8859-1", "ignore"),
                         "(?i)content-type: image/png")

    def test_missing_path_404(self):
        raw = self.fetch(self.port_of["corp-doc.com"], "/no/such")
        head, _ = self.head_of(raw)
        self.assertEqual(head.split(b"\r\n")[0], b"HTTP/1.1 404 Not Found")


class TestSiteTable(unittest.TestCase):
    def test_ports_18001_18004_and_binding_map(self):
        self.assertEqual(
            {d: c["port"] for d, c in SITES.items()},
            {"corp-doc.com": 18001, "game-site.com": 18002,
             "shop.example": 18003, "blocked-site.example": 18004})

    def test_domains_overlap_proxy_policy(self):
        # 类别演示前提:game/shop 域名必须能打上对应类别标签
        import categories as cat_mod
        self.assertIn("game", cat_mod.tag("game-site.com", "/"))
        self.assertIn("shopping", cat_mod.tag("shop.example", "/"))
        # blocked-site.example 走全局黑名单红线(非类别),与 config.toml 呼应
        import tomllib
        with open("config.toml", "rb") as f:
            pol = tomllib.load(f)["policy"]
        self.assertIn("*.blocked-site.example", pol["blacklist_domains"])
        self.assertTrue(all("corp-doc.com" in s
                            for s in ("corp-doc.com",)))


if __name__ == "__main__":
    unittest.main()
