"""端到端自动化验收:python e2e_proxy.py(零第三方依赖,离线,免管理员)

装配链与规格书演示矩阵一致:
  临时配置目录(完整规则+三角色) → ProxyServer 真实监听 →
  演示站点(demo_sites) + getaddrinfo 映射(把演示域名解析到 127.0.0.1,
  免改 hosts) → 以三类真实角色发送真实 HTTP,逐步断言;
  中途现场演示热加载(whitelist_mode 切换);收尾校验 SQLite 审计库。

退出码:0=全部通过;1=存在失败项。
"""
import base64
import os
import re
import socket
import sys
import tempfile
import threading
import time

from config import load_config_dir
from demo_sites import DemoServer
from http_message import BufferedSocketReader, content_length, read_response_head
from proxy_server import ProxyServer
from tests.test_forward_tunnel import FakeUpstream, HTML_BAD, TEXT_200

DEMO_HOSTS = ("corp-doc.com", "game-site.com", "shop.example",
              "blocked-site.example")

CONFIG_TOML = """[proxy]
listen_host = "127.0.0.1"
max_connections = 32
recv_timeout = 10.0
idle_timeout = 10.0
auth_required = true

[audit]
db_path = "{db}"

[cache]
enabled = true
dir = "{cache}"

[policy]
whitelist_mode = false
whitelist = ["corp-doc.com"]
blacklist_domains = ["*.blocked-site.example"]
blacklist_keywords = []
blacklist_url_regex = []
disabled_categories = []
request_signatures = ["password\\\\s*="]
response_signatures = ["违规内容特征词"]

block_page = "block_page.html"
"""

USERS_TOML = """[users.admin]
password = "admin123"
role = "admin"

[users.manager]
password = "mng123"
role = "manager"

[users.employee]
password = "emp123"
role = "employee"
"""

ROLES_TOML = """[roles.admin]
whitelist_mode = false
disabled_categories = []
request_signatures = []
response_signatures = []

[roles.manager]
disabled_categories = ["gambling"]
request_signatures = []
response_signatures = []

[roles.employee]
disabled_categories = ["game", "shopping", "gambling"]
"""


def _patch_resolver():
    """把演示域名解析指向 127.0.0.1(等价 hosts 映射,免管理员)。"""
    real = socket.getaddrinfo

    def patched(host, port, *a, **kw):
        if isinstance(host, str) and host in DEMO_HOSTS:
            host = "127.0.0.1"
        return real(host, port, *a, **kw)

    socket.getaddrinfo = patched


def auth_header(user, pw):
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return f"Proxy-Authorization: Basic {token}"


class ProxyClient:
    """面向代理的真实客户端:每请求新连接,返回 (status, headers, body)。"""

    def __init__(self, proxy_addr):
        self.proxy_addr = proxy_addr

    def req(self, method, host, port, path, user=None, pw=None):
        s = socket.create_connection(self.proxy_addr, timeout=10)
        try:
            head = (f"{method} http://{host}:{port}{path} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n")
            if user:
                head += auth_header(user, pw) + "\r\n"
            head += "Connection: close\r\n\r\n"
            s.sendall(head.encode())
            s.settimeout(10)
            reader = BufferedSocketReader(s)
            resp = read_response_head(reader)
            body = b""
            if resp.status not in (204, 304) and method != "HEAD":
                cl = content_length(resp.headers)
                if cl is not None:
                    body = reader.read_exact(cl)
                else:
                    while True:
                        chunk = reader.read_some(65536)
                        if not chunk:
                            break
                        body += chunk
            return resp.status, resp.headers, body
        finally:
            s.close()


class E2E:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.passed = 0
        self.failed = 0
        self.failures = []

    def step(self, name, fn):
        try:
            fn()
            self.passed += 1
            print(f"  [PASS] {name}")
        except AssertionError as e:
            self.failed += 1
            self.failures.append((name, str(e)))
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:  # 网络层异常也记为失败
            self.failed += 1
            self.failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")

    def expect(self, cond, msg):
        if not cond:
            raise AssertionError(msg)

    # ---- 各演示段 ----
    def auth_and_matrix(self):
        c = self.client
        demo = self.demo

        def t407():
            st, _, _ = c.req("GET", "corp-doc.com", demo.ports["corp-doc.com"],
                             "/", None, None)
            self.expect(st == 407, f"无认证应 407,实际 {st}")

        def t_blacklist_redline():
            # 红线:三个角色访问黑名单域一律 403
            for u, p in (("admin", "admin123"), ("manager", "mng123"),
                         ("employee", "emp123")):
                st, _, _ = c.req("GET", "blocked-site.example",
                                 demo.ports["blocked-site.example"], "/", u, p)
                self.expect(st == 403, f"{u} 访问黑名单域应 403,实际 {st}")

        def t_employee_categories():
            for host, port in (("game-site.com", 18002), ("shop.example", 18003)):
                st, _, body = c.req("GET", host, demo.ports[host], "/",
                                    "employee", "emp123")
                self.expect(st == 403, f"employee 访问 {host} 应 403,实际 {st}")
                self.expect("拦截".encode() in body, "403 应返回拦截页")

        def t_manager_only_gambling():
            st, _, _ = c.req("GET", "game-site.com",
                             demo.ports["game-site.com"], "/",
                             "manager", "mng123")
            self.expect(st == 200, f"manager 游戏站应 200,实际 {st}")
            st, _, _ = c.req("GET", "casino.example", 443, "/",
                             "manager", "mng123")
            self.expect(st == 403, f"manager 赌博域应 403,实际 {st}")
            st, _, _ = c.req("GET", "game-site.com",
                             demo.ports["game-site.com"], "/game/lobby",
                             "employee", "emp123")
            self.expect(st == 403, f"employee /game/ 路径应 403,实际 {st}")

        def t_admin_all_access():
            st, _, body = c.req("GET", "shop.example",
                                demo.ports["shop.example"], "/",
                                "admin", "admin123")
            self.expect(st == 200, f"admin 商城应 200,实际 {st}")
            self.expect("购物商城".encode() in body, "应返回商城页面")

        def t_request_signature():
            # employee 回退全局特征 → 拦;admin/manager 显式空表 → 放行
            plain_up = self.srv._up_plain
            st, _, _ = c.req("GET", "127.0.0.1", plain_up.port,
                             "/login?password=1", "employee", "emp123")
            self.expect(st == 403, f"employee password= 应 403,实际 {st}")
            for u, p in (("admin", "admin123"), ("manager", "mng123")):
                st, _, _ = c.req("GET", "127.0.0.1", plain_up.port,
                                 "/login?password=1", u, p)
                self.expect(st == 200, f"{u} password= 应放行 200,实际 {st}")

        def t_response_signature():
            # 上游返回含违规特征词的 HTML:
            # employee(回退全局)→ 内容被拦;admin(显式空)→ 正常到达
            bad_up = self.srv._up_bad
            st, _, _ = c.req("GET", "127.0.0.1", bad_up.port,
                             "/page", "employee", "emp123")
            self.expect(st == 403, f"employee 违规内容应 403,实际 {st}")
            st, _, body = c.req("GET", "127.0.0.1", bad_up.port,
                                "/page", "admin", "admin123")
            self.expect(st == 200, f"admin 违规内容应 200(绕过),实际 {st}")
            self.expect("违规内容特征词".encode() in body, "admin 应收到原文")

        def t_cache_roundtrip():
            url_path = "/static/logo.png"
            for u, p in (("employee", "emp123"),):
                st, headers, body = c.req("GET", "corp-doc.com",
                                          demo.ports["corp-doc.com"],
                                          url_path, u, p)
                self.expect(st == 200, f"首次 logo 应 200,实际 {st}")
                self.expect(body[:8] == b"\x89PNG\r\n\x1a\n", "应为 PNG")
                self.expect(headers.get("x-cache") is None,
                            "首次不应有 X-Cache")
                st2, headers2, body2 = c.req("GET", "corp-doc.com",
                                             demo.ports["corp-doc.com"],
                                             url_path, u, p)
                self.expect(st2 == 200 and body2 == body, "二次内容一致")
                self.expect((headers2.get("x-cache") or "").upper() == "HIT",
                            "二次应 X-Cache: HIT")

        for name, fn in (("407 无认证", t407),
                         ("黑名单红线(全员 403)", t_blacklist_redline),
                         ("employee 类别拦截", t_employee_categories),
                         ("manager 仅禁赌博", t_manager_only_gambling),
                         ("admin 全放行", t_admin_all_access),
                         ("请求特征(角色回退语义)", t_request_signature),
                         ("响应内容特征(角色回退语义)", t_response_signature),
                         ("静态资源缓存往返", t_cache_roundtrip)):
            self.step(name, fn)

    def hot_reload(self):
        c = self.client
        demo = self.demo

        def t_hot_switch():
            cfg_path = os.path.join(self.dir, "config.toml")
            with open(cfg_path, encoding="utf-8") as f:
                original = f.read()
            try:
                # 现场切换为白名单模式(仅 corp-doc.com)。注意角色语义:
                # manager 未显式覆盖 whitelist_mode → 回退跟随全局;
                # admin 显式 false → 不受白名单约束。
                patched = original.replace("whitelist_mode = false",
                                           "whitelist_mode = true", 1)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(patched)
                port = demo.ports["game-site.com"]
                deadline = time.time() + 5
                st = None
                while time.time() < deadline:
                    st, _, _ = c.req("GET", "game-site.com", port, "/",
                                     "manager", "mng123")
                    if st == 403:
                        break
                    time.sleep(0.1)
                self.expect(st == 403, f"白名单模式开启后 manager 访问 "
                                       f"game-site 应 403,实际 {st}"
                                       f"(热加载未生效)")
                # 白名单内域名照常放行
                st, _, _ = c.req("GET", "corp-doc.com",
                                 demo.ports["corp-doc.com"], "/",
                                 "manager", "mng123")
                self.expect(st == 200, f"白名单内 corp-doc 应 200,实际 {st}")
                # 角色显式覆盖:admin 全局 false → 白名单外仍可访问
                st, _, _ = c.req("GET", "game-site.com", port, "/",
                                 "admin", "admin123")
                self.expect(st == 200, f"admin 显式绕过白名单应 200,实际 {st}")
            finally:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(original)
            # 切回后 manager 恢复放行
            deadline = time.time() + 5
            st = None
            while time.time() < deadline:
                st, _, _ = c.req("GET", "game-site.com",
                                 demo.ports["game-site.com"], "/",
                                 "manager", "mng123")
                if st == 200:
                    break
                time.sleep(0.1)
            self.expect(st == 200, f"关闭白名单后应恢复 200,实际 {st}")

        self.step("热加载:运行中切换白名单模式", t_hot_switch)

    def audit_checks(self):
        log = self.srv.runtime.audit

        def t_audit():
            counts = log.decision_counts()
            self.expect(counts.get("block_category", 0) >= 4,
                        f"应有 ≥4 条 block_category,实际 {counts}")
            self.expect(counts.get("block_blacklist", 0) >= 3,
                        f"应有 ≥3 条 block_blacklist,实际 {counts}")
            self.expect(counts.get("block_signature", 0) >= 1,
                        "应有 block_signature 记录")
            self.expect(counts.get("block_resp_signature", 0) >= 1,
                        "应有 block_resp_signature 记录")
            self.expect(counts.get("auth_fail", 0) == 1,
                        f"应有 1 条 auth_fail,实际 {counts}")
            self.expect(counts.get("allow", 0) >= 8, "应有 allow 记录")
            rows = log.query(decision="block_category")
            self.expect(any(r["user"] == "employee" for r in rows),
                        "block_category 应记 employee")
            self.expect(any(r["user"] == "manager" for r in rows),
                        "casino 被 manager 拦截也应入账")
            cache_hit = log.query()
            self.expect(any(r["cache_hit"] == 1 for r in cache_hit),
                        "审计应记录缓存命中")
            top = log.top("host")
            self.expect(len(top) >= 5, "主机排行应有内容")

        self.step("审计库:决策统计/排行/缓存命中列", t_audit)

    def run(self):
        _patch_resolver()
        db = os.path.join(self.dir, "audit.db").replace("\\", "/")
        cache_dir = os.path.join(self.dir, "cache").replace("\\", "/")
        with open(os.path.join(self.dir, "config.toml"), "w",
                  encoding="utf-8") as f:
            f.write(CONFIG_TOML.format(db=db, cache=cache_dir))
        for name, text in (("users.toml", USERS_TOML),
                           ("roles.toml", ROLES_TOML)):
            with open(os.path.join(self.dir, name), "w",
                      encoding="utf-8") as f:
                f.write(text)
        import shutil
        shutil.copy("block_page.html", self.dir)

        self.demo = DemoServer()
        self.demo.start()
        time.sleep(0.1)
        self.srv = ProxyServer(self.dir, port=0)
        self.srv.start()
        self.srv.ready.wait(5)
        addr = ("127.0.0.1", self.srv.port)
        self.client = ProxyClient(addr)
        # 小上游:纯文本(请求特征演示)与违规 HTML(响应特征演示)各一
        self.srv._up_plain = FakeUpstream(TEXT_200)
        self.srv._up_bad = FakeUpstream(HTML_BAD)

        print("=" * 60)
        print("端到端验收:企业代理服务器(流量审计 + 访问控制)")
        print("=" * 60)
        self.auth_and_matrix()
        self.hot_reload()
        self.audit_checks()

        for up in (self.srv._up_plain, self.srv._up_bad):
            up.stop()
        self.srv.stop()
        self.demo.stop()
        self.tmp.cleanup()

        print("=" * 60)
        print(f"结果: {self.passed} 通过 / {self.failed} 失败")
        if self.failures:
            for name, why in self.failures:
                print(f"  ✗ {name}: {why}")
            return 1
        print("全部通过 ✓")
        return 0


def main():
    for stream in (sys.stdout, sys.stderr):   # Windows 控制台 GBK 兼容
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    return E2E().run()


if __name__ == "__main__":
    sys.exit(main())
