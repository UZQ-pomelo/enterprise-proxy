"""http_message 模块单元测试:读取器、报文解析、构造、上游请求重写。"""
import socket
import unittest

from http_message import (
    BufferedSocketReader, ByteCounter, ConnectionClosed, CRLF, Headers,
    ProtocolError, build_response, build_upstream_request, content_length,
    is_chunked, iter_raw_chunked, make_407, make_simple, parse_authority,
    parse_http_url, read_request_head, read_response_head, render_block_page,
    serialize_response_head, strip_hop_headers,
)


def pair():
    a, b = socket.socketpair()
    return a, b


class TestBufferedSocketReader(unittest.TestCase):
    def test_read_until_and_read_exact(self):
        a, b = pair()
        try:
            a.sendall(b"GET / HTTP/1.1\r\n\r\nTAIL")
            reader = BufferedSocketReader(b)
            data = reader.read_until(CRLF + CRLF)
            self.assertEqual(data, b"GET / HTTP/1.1\r\n\r\n")
            self.assertEqual(reader.bytes_read, 18)
            # 缓冲区里留下的 TAIL 仍然可读
            self.assertEqual(reader.read_exact(4), b"TAIL")
        finally:
            a.close(); b.close()

    def test_read_until_eof_partial_then_raise(self):
        a, b = pair()
        try:
            a.sendall(b"abc")
            a.shutdown(socket.SHUT_WR)
            reader = BufferedSocketReader(b)
            self.assertEqual(reader.read_until(b"XY"), b"abc")  # EOF 时返回已读部分
            with self.assertRaises(ConnectionClosed):
                reader.read_until(b"XY")  # 空缓冲遇 EOF 抛异常
        finally:
            a.close(); b.close()

    def test_too_long_line(self):
        a, b = pair()
        try:
            reader = BufferedSocketReader(b)
            a.sendall(b"x" * 200)
            # 缓冲区超过 limit(还未读到分隔符)即抛错
            with self.assertRaises(ProtocolError):
                reader.read_until(CRLF, limit=100)
        finally:
            a.close(); b.close()


class TestRequestHead(unittest.TestCase):
    RAW = (b"GET http://example.com/a?q=1 HTTP/1.1\r\n"
           b"Host: example.com\r\n"
           b"User-Agent: test\r\n"
           b"\r\n")

    def test_parse_request_head(self):
        a, b = pair()
        try:
            a.sendall(self.RAW)
            head = read_request_head(BufferedSocketReader(b))
            self.assertEqual(head.method, "GET")
            self.assertEqual(head.target, "http://example.com/a?q=1")
            self.assertEqual(head.version, "HTTP/1.1")
            self.assertEqual(head.headers.get("host"), "example.com")
            self.assertEqual(head.headers.get("USER-AGENT"), "test")  # 大小写不敏感
        finally:
            a.close(); b.close()

    def test_malformed_request_line(self):
        a, b = pair()
        try:
            a.sendall(b"GET /nope\r\n\r\n")
            with self.assertRaises(ProtocolError):
                read_request_head(BufferedSocketReader(b))
        finally:
            a.close(); b.close()

    def test_missing_terminator(self):
        a, b = pair()
        try:
            a.sendall(b"GET http://x/ HTTP/1.1\r\nHost: x\r\n")  # 无空行
            reader = BufferedSocketReader(b)
            a.shutdown(socket.SHUT_WR)
            with self.assertRaises(ProtocolError):
                read_request_head(reader)
        finally:
            a.close(); b.close()


class TestResponseHead(unittest.TestCase):
    def test_parse_with_reason_spaces_and_100(self):
        a, b = pair()
        try:
            raw = (b"HTTP/1.1 100 Continue\r\n\r\n"
                   b"HTTP/1.1 200 OK\r\n"
                   b"Content-Type: text/html\r\n"
                   b"Content-Length: 5\r\n"
                   b"\r\n")
            a.sendall(raw)
            resp = read_response_head(BufferedSocketReader(b))
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.reason, "OK")
            self.assertEqual(content_length(resp.headers), 5)
        finally:
            a.close(); b.close()

    def test_reason_with_spaces_kept(self):
        a, b = pair()
        try:
            a.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
            resp = read_response_head(BufferedSocketReader(b))
            self.assertEqual(resp.status, 404)
            self.assertEqual(resp.reason, "Not Found")
        finally:
            a.close(); b.close()


class TestUrlHelpers(unittest.TestCase):
    def test_parse_http_url(self):
        self.assertEqual(
            parse_http_url("http://Example.com:8080/a/b?x=1&y=2"),
            ("example.com", 8080, "/a/b?x=1&y=2"))
        self.assertEqual(parse_http_url("http://example.com"),
                         ("example.com", 80, "/"))
        with self.assertRaises(ProtocolError):
            parse_http_url("https://example.com/")

    def test_parse_authority(self):
        self.assertEqual(parse_authority("game-site.com:443"), ("game-site.com", 443))
        self.assertEqual(parse_authority("example.com"), ("example.com", 443))
        with self.assertRaises(ProtocolError):
            parse_authority("example.com:99999")


class TestHeaderHelpers(unittest.TestCase):
    def test_headers_remove_and_set(self):
        h = Headers([("A", "1"), ("b", "2")])
        h.set("a", "3")
        self.assertEqual(h.get("a"), "3")
        self.assertEqual(len(h), 2)
        h.remove("A")
        self.assertEqual(len(h), 1)

    def test_chunked_flag(self):
        self.assertTrue(is_chunked(Headers([("Transfer-Encoding", "chunked")])))
        self.assertFalse(is_chunked(Headers([])))

    def test_iter_raw_chunked_preserves_framing(self):
        a, b = pair()
        try:
            raw_body = (b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n"
                        b"X-Trailer: v\r\n\r\n")
            a.sendall(raw_body)
            segs = list(iter_raw_chunked(BufferedSocketReader(b)))
            self.assertEqual(b"".join(segs), raw_body)  # 原样透传
        finally:
            a.close(); b.close()


class TestSerialization(unittest.TestCase):
    def test_build_response_sets_content_length(self):
        resp = build_response(403, "Forbidden",
                              [("Content-Type", "text/html"), ("Connection", "close")],
                              b"<h1>no</h1>")
        self.assertTrue(resp.startswith(b"HTTP/1.1 403 Forbidden\r\n"))
        self.assertIn(b"Content-Length: 11\r\n", resp)
        self.assertTrue(resp.endswith(b"\r\n\r\n<h1>no</h1>"))

    def test_make_407(self):
        resp = make_407()
        self.assertTrue(resp.startswith(b"HTTP/1.1 407 "))
        self.assertIn(b'Proxy-Authenticate: Basic realm="corp-proxy"', resp)

    def test_make_simple_and_render_block_page(self):
        resp = make_simple(400, "Bad Request", "代理拒绝")
        self.assertIn("代理拒绝".encode(), resp)
        page = b"<p>blocked __RULE_ID__ __REASON__</p>"
        out = render_block_page(page, "category:game", "类别被禁")
        self.assertIn(b"category:game", out)
        self.assertIn("类别被禁".encode(), out)

    def test_serialize_response_head(self):
        raw = serialize_response_head("HTTP/1.1", 200, "OK",
                                      Headers([("A", "1")]))
        self.assertEqual(raw, b"HTTP/1.1 200 OK\r\nA: 1\r\n\r\n")


class TestUpstreamRewrite(unittest.TestCase):
    HEAD_RAW = (b"GET http://example.com:8080/a?x=1 HTTP/1.1\r\n"
                b"Host: example.com:8080\r\n"
                b"Proxy-Authorization: Basic YWJjOmRlZg==\r\n"
                b"Connection: keep-alive\r\n"
                b"X-Extra: v\r\n"
                b"Content-Length: 0\r\n"
                b"\r\n")

    def test_absolute_uri_becomes_origin_form_and_headers_stripped(self):
        a, b = pair()
        try:
            a.sendall(self.HEAD_RAW)
            head = read_request_head(BufferedSocketReader(b))
            out = build_upstream_request(head)
            self.assertTrue(out.startswith(b"GET /a?x=1 HTTP/1.1\r\n"))
            self.assertNotIn(b"proxy-authorization", out.lower())
            self.assertNotIn(b"Proxy-Authorization", out)
            self.assertIn(b"Host: example.com:8080\r\n", out)
            self.assertIn(b"Content-Length: 0\r\n", out)   # content-length 保留
            self.assertTrue(out.endswith(b"Connection: close\r\n\r\n"))
        finally:
            a.close(); b.close()

    def test_strip_hop_headers_named_by_connection(self):
        h = Headers([("Connection", "close, X-Hop"),
                     ("X-Hop", "secret"),
                     ("X-Keep", "1")])
        out = strip_hop_headers(h)
        self.assertEqual(out.get("X-Hop"), None)
        self.assertEqual(out.get("X-Keep"), "1")

    def test_keep_transfer_encoding_flag(self):
        h = Headers([("Transfer-Encoding", "chunked")])
        stripped = strip_hop_headers(h, keep_transfer_encoding=True)
        self.assertEqual(stripped.get("transfer-encoding"), "chunked")


class TestByteCounter(unittest.TestCase):
    def test_counting_send(self):
        a, b = pair()
        try:
            w = ByteCounter(b)
            w.send(b"123")
            w.send(b"4567")
            self.assertEqual(w.bytes, 7)
            self.assertEqual(a.recv(7), b"1234567")
        finally:
            a.close(); b.close()


if __name__ == "__main__":
    unittest.main()
