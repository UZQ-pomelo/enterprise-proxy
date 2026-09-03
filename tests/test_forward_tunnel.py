"""forward/tunnel/runtime 单元测试:假上游 + socketpair 客户端。

测试在单线程内驱动 handle_http/handle_connect,用 ThreadingTCPServer
扮演真实上游,验证:请求改写、无 Proxy-Authorization 上送、正文转发
(定长/分块)、精确响应中继、响应特征拦截、缓存往返、502、隧道
CONNECT 与黑名单直断。
"""
import os
import re
import socket
import socketserver
import tempfile
import threading
import time
import unittest

from config import load_config_dir
from http_message import BufferedSocketReader, ByteCounter, read_request_head
from runtime import ProxyRuntime, RuntimeCtx, finish_rec, new_rec


def toml_for(*, cache_block="enabled = false\n", policy_block="[policy]\n",
             audit_db="audit.db"):
    return f"""[proxy]
listen_host = "127.0.0.1"
recv_timeout = 5.0
idle_timeout = 5.0

[audit]
db_path = "{audit_db}"

[cache]
{cache_block}

{policy_block}
"""


class _RecvMixin:
    """带内部缓冲的裸 socket 读取工具(假上游专用)。"""

    def _read_until(self, sep):
        out = bytearray(getattr(self, "_rest", b""))
        self._rest = b""
        while sep not in out:
            chunk = self.request.recv(65536)
            if not chunk:
                return bytes(out)
            out.extend(chunk)
        end = out.index(sep) + len(sep)
        self._rest = bytes(out[end:])
        return bytes(out[:end])

    def _recv_exact(self, n):
        out = bytearray()
        rest = getattr(self, "_rest", b"")
        if rest:
            take = min(n, len(rest))
            out += rest[:take]
            self._rest = rest[take:]
        while len(out) < n:
            chunk = self.request.recv(n - len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)


class FakeUpstream:
    """一次请求一条连接的假上游;记录收到的原始请求字节(含正文)。"""

    def __init__(self, responses=b""):
        self.requests = []
        self.lock = threading.Lock()

        class Handler(_RecvMixin, socketserver.BaseRequestHandler):
            def handle(self):
                srv = self.server
                self._rest = b""
                head = self._read_until(b"\r\n\r\n")
                raw = head
                if re.search(rb"(?i)\bchunked\b", head):
                    while True:  # 按原始帧结构吞掉请求体,与代理时序一致
                        line = self._read_until(b"\r\n")
                        raw += line
                        try:
                            size = int(line.split(b";", 1)[0].strip(), 16)
                        except ValueError:
                            return
                        if size == 0:
                            while not raw.endswith(b"\r\n\r\n"):
                                raw += self._read_until(b"\r\n")
                            break
                        raw += self._recv_exact(size + 2)
                else:
                    m = re.search(rb"(?i)\r\ncontent-length:\s*(\d+)\r\n", head)
                    if m:
                        raw += self._recv_exact(int(m.group(1)))
                with srv.lock:
                    srv.requests.append(raw)
                    idx = len(srv.requests)
                    if isinstance(srv.responses, bytes):
                        body = srv.responses
                    else:
                        body = srv.responses[min(idx, len(srv.responses)) - 1]
                self.request.sendall(body)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.responses = responses
        self.server.requests = self.requests
        self.server.lock = self.lock
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeEcho:
    """回显上游:收到的字节原样返回,直到对端关闭。"""

    def __init__(self):
        class EchoHandler(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    data = self.request.recv(65536)
                    if not data:
                        return
                    self.request.sendall(data)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), EchoHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


TEXT_200 = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
            b"Content-Length: 5\r\n\r\nhello")
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"0" * 128
PNG_200 = (b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
           b"Cache-Control: max-age=3600\r\nContent-Length: " +
           str(len(PNG_BODY)).encode() + b"\r\n\r\n" + PNG_BODY)
_BAD_TEXT = "违规内容特征词"
HTML_BAD = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            b"Content-Length: " + str(len(_BAD_TEXT.encode("utf-8"))).encode() +
            b"\r\n\r\n" + _BAD_TEXT.encode("utf-8"))
CHUNKED_200 = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n"
               b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
CONNECT_200 = b"HTTP/1.1 200 Connection established\r\n\r\n"


class ForwardEnv(unittest.TestCase):
    """搭临时配置目录 + runtime + socketpair 客户端通道。"""

    cache_on = False
    role = None
    policy_block = "[policy]\n"

    def setUp(self):
        self._sockets = []
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        cache_toml = "enabled = true\n" if self.cache_on else "enabled = false\n"
        db = os.path.join(self.dir, "audit.db").replace("\\", "/")
        with open(os.path.join(self.dir, "config.toml"), "w", encoding="utf-8") as f:
            f.write(toml_for(cache_block=cache_toml, policy_block=self.policy_block,
                             audit_db=db))
        for name in ("users.toml", "roles.toml"):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write("[users]\n" if name == "users.toml" else "[roles]\n")
        self.runtime = ProxyRuntime.make(load_config_dir(self.dir))

    def tearDown(self):
        self.runtime.audit.close()
        for s in self._sockets:
            try:
                s.close()
            except OSError:
                pass
        try:
            self.peer.close()
        except (OSError, AttributeError):
            pass
        self._tmp.cleanup()

    def make_ctx(self, role=None, user="tester"):
        c2p, p2c = socket.socketpair()
        self._sockets.append(c2p)
        self._sockets.append(p2c)
        self.peer = c2p
        reader = BufferedSocketReader(p2c)
        writer = ByteCounter(p2c)
        return RuntimeCtx(runtime=self.runtime, sock=p2c, reader=reader,
                          writer=writer, user=user, role=role)

    def send_request(self, request: bytes):
        ctx = self.make_ctx(role=self.role)
        rec = new_rec(ctx)            # 基线先于请求头读取 → 上行含头部字节
        self.peer.sendall(request)
        head = read_request_head(ctx.reader)
        rec["method"] = head.method
        return ctx, head, rec

    def run_forward(self, request: bytes):
        """整体驱动:发请求 → handle_http → finish_rec,返回 (ctx, rec)。"""
        ctx, head, rec = self.send_request(request)
        from forward import handle_http
        handle_http(ctx, head, rec)
        finish_rec(ctx, rec)
        return ctx, rec

    def recv_until(self, marker: bytes, sock=None):
        sock = sock or self.peer
        sock.settimeout(3)
        out = b""
        while marker not in out:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out += chunk
        return out

    def recv_n(self, n, sock=None):
        sock = sock or self.peer
        sock.settimeout(3)
        out = b""
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                break
            out += chunk
        return out


class AllowPolicy(ForwardEnv):
    policy_block = "[policy]\n"


class TestForwardRewrite(AllowPolicy):
    def test_origin_form_and_no_proxy_auth(self):
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/a/b?x=1&y=2"
            req = (f"GET {url} HTTP/1.1\r\nHost: 127.0.0.1:{up.port}\r\n"
                   "Proxy-Authorization: Basic dGVzdGVyOnh4eA==\r\n"
                   "Connection: keep-alive\r\n\r\n").encode()
            ctx, rec = self.run_forward(req)
            got = self.recv_n(len(TEXT_200))
            self.assertEqual(got, TEXT_200)
            self.assertEqual(rec["decision"], "allow")
            self.assertEqual(rec["resp_status"], 200)
            captured = up.requests[0]
            self.assertTrue(captured.startswith(b"GET /a/b?x=1&y=2 HTTP/1.1\r\n"))
            self.assertNotIn(b"Proxy-Authorization", captured)
            self.assertNotIn(b"Connection: keep-alive", captured)
            self.assertEqual(rec["bytes_down"], len(TEXT_200))
            self.assertEqual(rec["bytes_up"], len(req))
        finally:
            up.stop()


class TestRequestBodyRelay(AllowPolicy):
    def test_content_length_post(self):
        body = b"name=claude&dept=net"
        resp = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\n\r\nok")
        up = FakeUpstream(resp)
        try:
            url = f"http://127.0.0.1:{up.port}/submit"
            req = (f"POST {url} HTTP/1.1\r\nHost: 127.0.0.1:{up.port}\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
            self.run_forward(req)
            self.recv_n(len(resp))
            self.assertTrue(up.requests[0].endswith(b"\r\n\r\n" + body))
        finally:
            up.stop()

    def test_chunked_post_relayed_raw(self):
        chunks = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/upload"
            req = (f"POST {url} HTTP/1.1\r\nHost: 127.0.0.1:{up.port}\r\n"
                   "Transfer-Encoding: chunked\r\n\r\n").encode() + chunks
            self.run_forward(req)
            self.recv_n(len(TEXT_200))
            self.assertTrue(up.requests[0].endswith(chunks))
        finally:
            up.stop()


class TestForwardResponses(AllowPolicy):
    def test_chunked_response_relayed_raw(self):
        up = FakeUpstream(CHUNKED_200)
        try:
            url = f"http://127.0.0.1:{up.port}/stream"
            req = (f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode()
            self.run_forward(req)
            got = self.recv_n(len(CHUNKED_200))
            self.assertEqual(got, CHUNKED_200)  # 原始分块帧原样透传
            self.assertIn(b"Transfer-Encoding: chunked", got)
        finally:
            up.stop()

    def test_head_request_no_body(self):
        head_resp = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n"
        up = FakeUpstream(head_resp)
        try:
            url = f"http://127.0.0.1:{up.port}/f"
            req = (f"HEAD {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode()
            self.run_forward(req)
            got = self.recv_n(len(head_resp))
            self.assertEqual(got, head_resp)
        finally:
            up.stop()

    def test_no_content_length_read_to_eof(self):
        resp = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                b"streamed-data-no-cl")
        up = FakeUpstream(resp)
        try:
            url = f"http://127.0.0.1:{up.port}/s"
            req = (f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode()
            ctx, rec = self.run_forward(req)
            got = self.recv_n(len(resp))
            self.assertEqual(got, resp)
            self.assertTrue(ctx.close_after)
            self.assertEqual(rec["bytes_down"], len(resp))
        finally:
            up.stop()


class TestBlockBeforeUpstream(ForwardEnv):
    policy_block = "[policy]\nblacklist_domains = ['127.0.0.1']\n"

    def test_blocked_403_no_upstream(self):
        up = FakeUpstream(TEXT_200)
        try:
            url = f"http://127.0.0.1:{up.port}/x"
            req = (f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode()
            self.role = "employee"
            ctx, rec = self.run_forward(req)
            got = self.recv_until(b"\r\n\r\n")
            self.assertEqual(got.split(b"\r\n")[0], b"HTTP/1.1 403 Forbidden")
            self.assertEqual(rec["decision"], "block_blacklist")
            self.assertEqual(rec["rule_id"], "domain:127.0.0.1")
            self.assertEqual(rec["resp_status"], 403)
            self.assertTrue(ctx.close_after)
            time.sleep(0.1)  # 留给假上游线程记录
            self.assertEqual(len(up.requests), 0)  # 未触达上游
        finally:
            up.stop()


class TestResponseSignature(ForwardEnv):
    policy_block = "[policy]\nresponse_signatures = ['违规内容特征词']\n"

    def test_block_403_and_decision(self):
        up = FakeUpstream(HTML_BAD)
        try:
            url = f"http://127.0.0.1:{up.port}/page"
            req = (f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode()
            self.role = "ghost"  # 未在 roles.toml 定义 → 回退全局特征
            ctx, rec = self.run_forward(req)
            got = self.recv_until(b"\r\n\r\n")
            self.assertEqual(got.split(b"\r\n")[0], b"HTTP/1.1 403 Forbidden")
            self.assertEqual(rec["decision"], "block_resp_signature")
            self.assertEqual(rec["rule_id"], "resp_sig:0")
            self.assertEqual(rec["resp_status"], 403)
        finally:
            up.stop()


class TestCacheRoundtrip(ForwardEnv):
    cache_on = True

    def test_first_miss_second_hit(self):
        up = FakeUpstream(PNG_200)
        try:
            url = f"http://127.0.0.1:{up.port}/logo.png"
            req = (f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode()

            ctx, rec = self.run_forward(req)
            got = self.recv_n(len(PNG_200))
            self.assertEqual(got, PNG_200)
            self.assertNotIn(b"X-Cache", got)
            self.assertEqual(rec["cache_hit"], 0)

            ctx2, rec2 = self.run_forward(req)  # 新连接 → 新 peer
            got2 = self.recv_until(b"X-Cache: HIT\r\n")
            head_end = got2.index(b"\r\n\r\n")
            head = got2[:head_end]
            m = re.search(rb"(?i)\r\ncontent-length:\s*(\d+)", head)
            total = head_end + 4 + int(m.group(1))
            if len(got2) < total:
                got2 += self.recv_n(total - len(got2))
            body = got2[head_end + 4:]
            self.assertEqual(body, PNG_BODY)
            self.assertEqual(rec2["cache_hit"], 1)
            self.assertEqual(rec2["decision"], "allow")
            self.assertEqual(len(up.requests), 1)  # 第二次未触达上游
        finally:
            up.stop()


class TestUpstreamUnreachable(AllowPolicy):
    def test_502_bad_gateway(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        url = f"http://127.0.0.1:{dead_port}/x"
        req = (f"GET {url} HTTP/1.1\r\nHost: h\r\n\r\n").encode()
        ctx, rec = self.run_forward(req)
        got = self.recv_until(b"\r\n\r\n")
        self.assertEqual(got.split(b"\r\n")[0], b"HTTP/1.1 502 Bad Gateway")
        self.assertEqual(rec["decision"], "error")
        self.assertEqual(rec["resp_status"], 502)
        self.assertTrue(ctx.close_after)


class TestTunnel(ForwardEnv):
    def test_connect_relay_echo(self):
        echo = FakeEcho()
        try:
            ctx = self.make_ctx(role=None)
            req = (f"CONNECT 127.0.0.1:{echo.port} HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{echo.port}\r\n\r\n").encode()
            rec = new_rec(ctx)
            self.peer.sendall(req)
            head = read_request_head(ctx.reader)
            rec["method"] = head.method
            from tunnel import handle_connect
            t = threading.Thread(target=handle_connect, args=(ctx, head, rec),
                                 daemon=True)
            t.start()
            # 1) 收到 200 Connection established
            self.recv_until(CONNECT_200)
            # 2) 双向透传
            self.peer.sendall(b"ping")
            got = self.recv_n(4)
            self.assertEqual(got, b"ping")
            # 3) 关闭 → 隧道结束,决策 allow
            self.peer.close()
            t.join(timeout=3)
            finish_rec(ctx, rec)
            self.assertFalse(t.is_alive())
            self.assertEqual(rec["decision"], "allow")
            # 上行 = CONNECT 请求头字节 + ping 载荷
            self.assertEqual(rec["bytes_up"], len(req) + 4)
            # bytes_down 口径 = 发给客户端的全部字节:200 头 + 回声载荷
            self.assertEqual(rec["bytes_down"], len(CONNECT_200) + 4)
            self.assertEqual(rec["resp_status"], None)
        finally:
            echo.stop()


class TestTunnelBlock(ForwardEnv):
    policy_block = "[policy]\nblacklist_domains = ['127.0.0.1']\n"

    def test_blocked_connect_silent_close(self):
        ctx = self.make_ctx(role="employee")
        self.peer.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: h\r\n\r\n")
        head = read_request_head(ctx.reader)
        rec = new_rec(ctx, head)
        from tunnel import handle_connect
        handle_connect(ctx, head, rec)  # 同步返回,无 200
        finish_rec(ctx, rec)
        self.peer.settimeout(0.3)
        try:
            data = self.peer.recv(1024)
        except socket.timeout:
            data = None
        self.assertIn(data, (None, b""))  # 未回 200,直接静默断开
        self.assertEqual(rec["decision"], "block_blacklist")
        self.assertTrue(ctx.close_after)


if __name__ == "__main__":
    unittest.main()
