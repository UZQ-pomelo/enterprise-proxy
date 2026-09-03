"""HTTP 报文解析与构造(纯函数/无状态,仅依赖标准库)。

本模块是代理的"协议地基":字节流读取、请求/响应头解析、
URL 拆分、请求体重写与响应序列化。所有网络读写均为 bytes。
"""
import re
import urllib.parse
from dataclasses import dataclass

CRLF = b"\r\n"

# hop-by-hop 头(转发时剥离;Content-Length 不属于 hop-by-hop,必须保留)
HOP_BY_HOP = frozenset({
    "connection", "proxy-connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
})


class ProtocolError(Exception):
    """协议层错误(格式非法、不支持的目标等)。"""


class ConnectionClosed(Exception):
    """对端关闭且无可用缓冲数据。"""


class TooLongLine(ProtocolError):
    """单行/单块超过长度上限。"""


class Headers(list):
    """有序 (name, value) 列表;查找不区分大小写,保留原始写法。"""

    def get(self, name):
        low = name.lower()
        for k, v in self:
            if k.lower() == low:
                return v
        return None

    def set(self, name, value):
        low = name.lower()
        for i, (k, v) in enumerate(self):
            if k.lower() == low:
                self[i] = (name, value)
                return
        self.append((name, value))

    def remove(self, name):
        low = name.lower()
        self[:] = [(k, v) for k, v in self if k.lower() != low]


class BufferedSocketReader:
    """带缓冲的阻塞 socket 读取器;bytes_read 记录已消费字节(审计口径)。"""

    def __init__(self, sock, buf_size=65536):
        self._sock = sock
        self._buf = bytearray()
        self._buf_size = buf_size
        self.bytes_read = 0

    def _recv(self):
        chunk = self._sock.recv(self._buf_size)
        if not chunk:
            raise ConnectionClosed()
        self._buf.extend(chunk)

    def read_until(self, sep=CRLF, limit=65536):
        """读到分隔符(含)返回;EOF 且有半行数据时返回半行;空缓冲 EOF 抛异常。"""
        while True:
            idx = self._buf.find(sep)
            if idx != -1:
                end = idx + len(sep)
                data = bytes(self._buf[:end])
                del self._buf[:end]
                self.bytes_read += len(data)
                return data
            if len(self._buf) > limit:
                raise TooLongLine(f"line too long (> {limit} bytes)")
            try:
                self._recv()
            except ConnectionClosed:
                if self._buf:
                    data = bytes(self._buf)
                    self._buf.clear()
                    self.bytes_read += len(data)
                    return data
                raise

    def read_some(self, max_n=65536):
        """读到任意数量(≤max_n)字节;EOF 返回 b""。"""
        if not self._buf:
            try:
                self._recv()
            except ConnectionClosed:
                return b""
        take = min(max_n, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        self.bytes_read += len(data)
        return data

    def read_exact(self, n):
        out = bytearray()
        while len(out) < n:
            if not self._buf:
                self._recv()
            take = min(n - len(out), len(self._buf))
            out += self._buf[:take]
            del self._buf[:take]
        self.bytes_read += n
        return bytes(out)


class ByteCounter:
    """包装 socket 的计数写端;发给客户端的字节统一经由此对象。"""

    def __init__(self, sock):
        self._sock = sock
        self.bytes = 0

    def send(self, data):
        self._sock.sendall(data)
        self.bytes += len(data)


@dataclass
class RequestHead:
    method: str
    target: str
    version: str
    headers: Headers


@dataclass
class ResponseHead:
    version: str
    status: int
    reason: str
    headers: Headers


def _parse_head_block(raw: bytes, is_request: bool):
    """解析请求头/响应头原始字节块,返回 (首行, 行字段, Headers)。"""
    if not raw.endswith(CRLF + CRLF):
        raise ProtocolError("头部未以空行结束")
    lines = raw.split(CRLF)
    if len(lines) < 2:
        raise ProtocolError("空的报文头")
    first = lines[0].decode("iso-8859-1")
    parts = first.split(" ", 2)
    if len(parts) != 3:
        raise ProtocolError(f"起始行格式错误: {first!r}")
    if is_request:
        method, target, version = parts
        if not re.fullmatch(r"HTTP/\d(\.\d)?", version):
            raise ProtocolError(f"非法协议版本: {version!r}")
        head_type = (method, target, version)
    else:
        version, status_s, reason = parts
        if not re.fullmatch(r"HTTP/\d(\.\d)?", version) or not status_s.isdigit():
            raise ProtocolError(f"非法状态行: {first!r}")
        head_type = (version, int(status_s), reason.strip())

    headers = Headers()
    for line in lines[1:-1]:  # 跳过首行与末尾空串
        if not line:
            continue
        text = line.decode("iso-8859-1")
        if text[:1] in " \t":  # obs-fold 续行(防御性拼接)
            if not headers:
                raise ProtocolError("续行前无字段")
            k, v = headers[-1]
            headers[-1] = (k, v + " " + text.strip())
            continue
        k, sep, v = text.partition(":")
        if not sep:
            raise ProtocolError(f"字段缺少冒号: {text!r}")
        headers.append((k.strip(), v.strip()))
    return head_type, headers


def read_request_head(reader):
    raw = reader.read_until(CRLF + CRLF, limit=81920)
    (method, target, version), headers = _parse_head_block(raw, True)
    return RequestHead(method, target, version, headers)


def read_response_head(reader):
    """读取响应头;跳过 1xx 信息性响应(如 100 Continue)。"""
    for _ in range(5):
        raw = reader.read_until(CRLF + CRLF, limit=81920)
        (version, status, reason), headers = _parse_head_block(raw, False)
        if status in (100, 103):
            continue
        return ResponseHead(version, status, reason, headers)
    raise ProtocolError("1xx 信息性响应过多")


def parse_http_url(target):
    """绝对形式 URL 'http://host[:port]/path?query' → (host, port, path)。"""
    u = urllib.parse.urlsplit(target)
    if u.scheme != "http":
        raise ProtocolError(f"不支持的目标协议: {u.scheme or '无'}")
    host = (u.hostname or "").lower()
    if not host:
        raise ProtocolError("目标缺少主机名")
    port = u.port if u.port is not None else 80
    if not (0 < port < 65536):
        raise ProtocolError(f"非法端口: {port}")
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return host, port, path


def parse_authority(target):
    """CONNECT 目标 'host:port'(无端口时默认 443)→ (host, port)。"""
    host, _, port_s = target.rpartition(":")
    if port_s.isdigit():
        port = int(port_s)
        if not (0 < port < 65536):
            raise ProtocolError(f"非法端口: {port}")
        return host.lower(), port
    return target.lower(), 443


def content_length(headers):
    v = headers.get("content-length")
    if v is None:
        return None
    v = v.strip()
    if not v.isdigit():
        return None
    return int(v)


def is_chunked(headers):
    te = headers.get("transfer-encoding")
    return te is not None and "chunked" in te.lower()


def iter_raw_chunked(reader):
    """逐段产出 chunked 请求体原始字节(含帧结构),用于原样转发。"""
    while True:
        line = reader.read_until(CRLF)
        yield line
        head = line.split(b";", 1)[0].strip()
        try:
            size = int(head, 16)
        except ValueError:
            raise ProtocolError(f"chunk 长度行非法: {line!r}") from None
        if size == 0:
            while True:  # trailers 直到空行
                t = reader.read_until(CRLF)
                yield t
                if t in (CRLF, b""):
                    return
        yield reader.read_exact(size)
        yield reader.read_exact(2)  # 帧尾 CRLF


def strip_hop_headers(headers, keep_transfer_encoding=False):
    """剥离 hop-by-hop 头;Connection 头点名的字段一并剥离。"""
    removed_tokens = set()
    conn = headers.get("connection")
    if conn:
        removed_tokens |= {t.strip().lower() for t in conn.split(",") if t.strip()}
    out = Headers()
    for k, v in headers:
        kl = k.lower()
        if kl in removed_tokens:
            continue
        if kl == "transfer-encoding" and keep_transfer_encoding:
            out.append((k, v))
            continue
        if kl in HOP_BY_HOP:
            continue
        out.append((k, v))
    return out


def build_upstream_request(head):
    """把客户端绝对形式请求改写为上游 origin-form 请求(HTTP/1.1)。"""
    u = urllib.parse.urlsplit(head.target)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    keep_te = is_chunked(head.headers)
    h = strip_hop_headers(head.headers, keep_transfer_encoding=keep_te)
    h.set("Connection", "close")  # 上游不复用,每请求新建
    lines = [f"{head.method} {path} HTTP/1.1\r\n".encode("iso-8859-1")]
    for k, v in h:
        lines.append(f"{k}: {v}\r\n".encode("iso-8859-1"))
    lines.append(CRLF)
    return b"".join(lines)


def serialize_response_head(version, status, reason, headers):
    out = [f"{version} {status} {reason}\r\n".encode("iso-8859-1")]
    for k, v in headers:
        out.append(f"{k}: {v}\r\n".encode("iso-8859-1"))
    out.append(CRLF)
    return b"".join(out)


def build_response(status, reason, headers, body=b""):
    """构造完整响应(自动补 Content-Length)。headers 为 [(k, v), ...]。"""
    h = Headers(headers)
    if h.get("content-length") is None:
        h.set("Content-Length", str(len(body)))
    return serialize_response_head("HTTP/1.1", status, reason, h) + body


def make_407(realm="corp-proxy"):
    return build_response(407, "Proxy Authentication Required", [
        ("Proxy-Authenticate", f'Basic realm="{realm}"'),
        ("Connection", "close"),
    ])


def make_simple(status, reason, text):
    body = text.encode("utf-8", "replace")
    return build_response(status, reason, [
        ("Connection", "close"),
        ("Content-Type", "text/plain; charset=utf-8"),
    ], body)


def render_block_page(template: bytes, rule_id: str, reason: str) -> bytes:
    """用规则信息填充拦截页模板(__RULE_ID__ / __REASON__ 占位)。"""
    text = template.decode("utf-8")
    text = text.replace("__RULE_ID__", rule_id or "-")
    text = text.replace("__REASON__", reason or "")
    return text.encode("utf-8")
