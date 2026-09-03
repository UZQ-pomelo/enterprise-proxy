"""M7 服务器装配测试:真实监听 + 真实客户端 socket。

覆盖:407 认证、认证通过转发、黑名单 403、多个 keep-alive 请求、
认证失败审计记录。
"""
import base64
import os
import tempfile
import time
import unittest

from tests.test_forward_tunnel import FakeUpstream, TEXT_200, toml_for


class ServerEnv(unittest.TestCase):
    auth_users = "[users.admin]\npassword = \"admin123\"\nrole = \"admin\"\n"
    policy_block = "[policy]\n"
    auth_required = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        db = os.path.join(self.dir, "audit.db").replace("\\", "/")
        proxy_extra = (f"auth_required = "
                       f"{'true' if self.auth_required else 'false'}\n")
        with open(os.path.join(self.dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write(toml_for(cache_block="enabled = false\n",
                             policy_block=self.policy_block,
                             audit_db=db, proxy_extra=proxy_extra))
        with open(os.path.join(self.dir, "users.toml"), "w", encoding="utf-8") as f:
            f.write(self.auth_users)
        with open(os.path.join(self.dir, "roles.toml"), "w", encoding="utf-8") as f:
            f.write("[roles.admin]\n")

        from proxy_server import ProxyServer
        self.server = ProxyServer(self.dir)
        self.server.start()
        self.port = self.server.port
        # 等监听就绪
        for _ in range(50):
            if self.server.ready.is_set():
                break
            time.sleep(0.02)
        self.assertTrue(self.server.ready.is_set())

    def tearDown(self):
        if getattr(self, "server", None) is not None:
            self.server.stop()
        if getattr(self, "_tmp", None) is not None:
            self._tmp.cleanup()

    # -- 客户端 --
    def connect(self):
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        return s

    @staticmethod
    def auth_value(user="admin", pw="admin123"):
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return f"Basic {token}"

    def read_response(self, sock):
        """读完整响应(头 + CL 正文),返回 (status_bytes, 全部字节)。"""
        sock.settimeout(5)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
        head, _, body = raw.partition(b"\r\n\r\n")
        import re
        m = re.search(rb"(?i)\r\ncontent-length:\s*(\d+)", head)
        if m:
            need = int(m.group(1)) - len(body)
            while need > 0:
                chunk = sock.recv(need)
                if not chunk:
                    break
                body += chunk
                need = int(m.group(1)) - len(body)
        return head.split(b"\r\n")[0], head, body


class TestAuthRequired(ServerEnv):
    def test_no_credentials_gets_407(self):
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/a"
            sock = self.connect()
            try:
                sock.sendall((f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode())
                status, head, _ = self.read_response(sock)
                self.assertEqual(status, b"HTTP/1.1 407 Proxy Authentication Required")
                self.assertIn(b"Proxy-Authenticate", head)
            finally:
                sock.close()
        finally:
            up.stop()

    def test_valid_credentials_forwarded(self):
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/page?q=1"
            sock = self.connect()
            try:
                sock.sendall((f"GET {url} HTTP/1.1\r\nHost: h\r\n"
                              f"Proxy-Authorization: {self.auth_value()}\r\n\r\n").encode())
                status, _, body = self.read_response(sock)
                self.assertEqual(status, b"HTTP/1.1 200 OK")
                self.assertEqual(body, b"hello")
                self.assertEqual(up.requests[0].split(b" ")[1], b"/page?q=1")
            finally:
                sock.close()
        finally:
            up.stop()

    def test_audited_after_auth_failure(self):
        sock = self.connect()
        try:
            sock.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: h\r\n\r\n")
            self.read_response(sock)
        finally:
            sock.close()
        # 落库在 worker 线程,与客户端收到响应存在微小竞态 → 轮询
        rows = []
        for _ in range(100):
            rows = self.server.runtime.audit.query(decision="auth_fail")
            if rows:
                break
            time.sleep(0.01)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resp_status"], 407)


class TestAuthDisabled(ServerEnv):
    auth_required = False

    def test_no_auth_allowed(self):
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/open"
            sock = self.connect()
            try:
                sock.sendall((f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode())
                status, _, _ = self.read_response(sock)
                self.assertEqual(status, b"HTTP/1.1 200 OK")
            finally:
                sock.close()
        finally:
            up.stop()


class TestBlocking(ServerEnv):
    policy_block = "[policy]\nblacklist_domains = ['127.0.0.1']\n"

    def test_blocked_403_over_the_wire(self):
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/x"
            sock = self.connect()
            try:
                sock.sendall((f"GET {url} HTTP/1.1\r\nHost: h\r\n"
                              f"Proxy-Authorization: {self.auth_value()}\r\n\r\n").encode())
                status, _, body = self.read_response(sock)
                self.assertEqual(status, b"HTTP/1.1 403 Forbidden")
                self.assertIn("访问被拦截".encode("utf-8"), body)
            finally:
                sock.close()
            self.assertEqual(len(up.requests), 0)
        finally:
            up.stop()


class TestRoleEnforcement(ServerEnv):
    """ctx.role 必须传入策略判定(角色绕过/禁用类别经真实连接生效)。"""
    auth_users = ("[users.admin]\npassword = \"admin123\"\nrole = \"admin\"\n"
                  "[users.employee]\npassword = \"emp123\"\nrole = \"employee\"\n")
    roles_block = ("[roles.admin]\nrequest_signatures = []\n"
                   "response_signatures = []\n"
                   "[roles.employee]\ndisabled_categories = ['game']\n")
    policy_block = ("[policy]\nrequest_signatures = ['password\\\\s*=']\n"
                    "disabled_categories = ['video']\n")

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.dir, "roles.toml"), "w", encoding="utf-8") as f:
            f.write(self.roles_block)
        self.server.runtime.engine.reload_if_changed()

    def test_admin_bypasses_signature(self):
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/login?password=1"
            sock = self.connect()
            try:
                sock.sendall((f"GET {url} HTTP/1.1\r\nHost: h\r\n"
                              f"Proxy-Authorization: {self.auth_value()}\r\n\r\n").encode())
                status, _, _ = self.read_response(sock)
                self.assertEqual(status, b"HTTP/1.1 200 OK")  # 非 403
            finally:
                sock.close()
        finally:
            up.stop()

    def test_employee_category_blocked(self):
        # 类别判定走 URL 主机;拦截先于上游连接,主机可不存在
        url = "http://game-site.com/game/play"
        sock = self.connect()
        try:
            sock.sendall((f"GET {url} HTTP/1.1\r\nHost: game-site.com\r\n"
                          f"Proxy-Authorization: {self.auth_value('employee', 'emp123')}\r\n\r\n").encode())
            status, _, _ = self.read_response(sock)
            self.assertEqual(status, b"HTTP/1.1 403 Forbidden")
        finally:
            sock.close()


class TestKeepAlive(ServerEnv):
    def test_two_requests_one_connection(self):
        up = FakeUpstream([TEXT_200, b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                 b"Content-Length: 2\r\n\r\nok"])
        try:
            sock = self.connect()
            try:
                for path in ("/one", "/two"):
                    url = f"http://127.0.0.1:{up.port}{path}"
                    sock.sendall((f"GET {url} HTTP/1.1\r\nHost: h\r\n"
                                  f"Proxy-Authorization: {self.auth_value()}\r\n\r\n").encode())
                    status, _, _ = self.read_response(sock)
                    self.assertEqual(status, b"HTTP/1.1 200 OK")
                self.assertEqual(len(up.requests), 2)
            finally:
                sock.close()
        finally:
            up.stop()


if __name__ == "__main__":
    unittest.main()
