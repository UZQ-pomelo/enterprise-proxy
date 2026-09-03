"""明文 HTTP 转发:黑名单/类别/特征检查 → 缓存 → 上游往返 → 响应侧检查。

架构要点(规格书 §5.2):
- 每个请求新建上游连接(Connection: close),不跨请求复用;
- 请求体按 Content-Length 或原始 chunked 帧原样转发;
- 响应带 Content-Length 的 text/* 且 ≤2MB 时整体缓冲做内容特征检查;
- 响应无 Content-Length → 读到 EOF(连接即结束,置 close_after)。
"""
import socket

from http_message import (BufferedSocketReader, ConnectionClosed,
                          ProtocolError, build_response,
                          build_upstream_request, content_length, is_chunked,
                          iter_raw_chunked, make_simple, parse_http_url,
                          read_response_head, render_block_page,
                          serialize_response_head, strip_hop_headers)
from runtime import apply_decision

TEXT_SIG_LIMIT = 2 * 1024 * 1024   # 特征检查的正文上限
BUFFER_LIMIT = 4 * 1024 * 1024     # 整文缓冲的上限(超限改流式)


def open_upstream(host, port, timeout):
    """连接上游;失败统一抛 ConnectionError(调用方转 502)。"""
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise ConnectionError(f"连接目标失败 {host}:{port}: {e}") from e


def _relay_client_body(reader, up, head):
    """把客户端请求体转发给上游(定长或原始分块帧)。"""
    cl = content_length(head.headers)
    if cl:
        if cl > 0:
            up.sendall(reader.read_exact(cl))
        return
    if is_chunked(head.headers):
        for seg in iter_raw_chunked(reader):
            up.sendall(seg)


def handle_http(ctx, head, rec):
    """处理一个明文 HTTP 请求(已由调用方解析好 head)。"""
    cfg = ctx.runtime.cfg
    host, port, path = parse_http_url(head.target)
    rec["host"], rec["port"], rec["url_path"] = host, port, path
    engine = ctx.runtime.engine
    headers_text = "\r\n".join(f"{k}: {v}" for k, v in head.headers)
    started = False  # 是否已向客户端写出响应首字节

    # 1) 请求侧策略(白名单/黑名单/类别/请求特征)
    d = engine.classify(host=host, path=path, url_text=head.target,
                        headers_text=headers_text, role_name=ctx.role)
    if d.action == "block":
        apply_decision(rec, d)
        rec["resp_status"] = 403
        page = render_block_page(engine.cfg.block_page, d.rule_id, d.reason)
        ctx.writer.send(build_response(403, "Forbidden", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Connection", "close"),
        ], page))
        ctx.close_after = True
        return

    cache = ctx.runtime.cache

    # 2) 缓存命中:本地直接应答,不触达上游
    if head.method == "GET" and cache is not None:
        hit = cache.get(head.target)
        if hit is not None:
            status, hdrs, body = hit
            hdrs.remove("content-length")
            hdrs.set("Content-Length", str(len(body)))
            hdrs.set("X-Cache", "HIT")
            ctx.writer.send(serialize_response_head(
                "HTTP/1.1", status, "OK", hdrs) + body)
            rec["decision"] = "allow"
            rec["resp_status"] = status
            rec["cache_hit"] = 1
            return

    # 3) 连接上游并转发
    try:
        up = open_upstream(host, port, cfg.server.recv_timeout)
    except ConnectionError as e:
        ctx.writer.send(make_simple(502, "Bad Gateway", str(e)))
        ctx.close_after = True
        rec["decision"] = "error"
        rec["resp_status"] = 502
        return
    try:
        up.settimeout(cfg.server.recv_timeout)
        up.sendall(build_upstream_request(head))
        _relay_client_body(ctx.reader, up, head)

        ureader = BufferedSocketReader(up)
        resp = read_response_head(ureader)
        rec["resp_status"] = resp.status

        chunked = is_chunked(resp.headers)
        send_h = strip_hop_headers(resp.headers,
                                   keep_transfer_encoding=chunked)
        head_bytes = serialize_response_head(resp.version, resp.status,
                                             resp.reason, send_h)
        no_body = head.method == "HEAD" or resp.status in (204, 304)
        cl = content_length(resp.headers)
        ct = (resp.headers.get("content-type") or "").lower()
        cache_ok = (head.method == "GET" and resp.status == 200
                    and cache is not None
                    and cache.is_cacheable_type(resp.headers))
        sig_eligible = cl is not None and cl <= TEXT_SIG_LIMIT \
            and ct.startswith("text/")
        buf_eligible = cl is not None and (sig_eligible
                                           or (cache_ok and cl <= BUFFER_LIMIT))

        if no_body:
            ctx.writer.send(head_bytes)
        elif chunked:
            ctx.writer.send(head_bytes)  # 分块响应:原始帧透传,不做缓冲
            started = True
            for seg in iter_raw_chunked(ureader):
                ctx.writer.send(seg)
        elif cl is None:
            ctx.writer.send(head_bytes)  # 无定界:读到 EOF,连接随之结束
            started = True
            while True:
                b = ureader.read_some(65536)
                if not b:
                    break
                ctx.writer.send(b)
            ctx.close_after = True
        elif buf_eligible:
            body = ureader.read_exact(cl)
            if sig_eligible:              # 响应内容特征检查
                d2 = engine.check_response(role_name=ctx.role,
                                           content_type=ct, body=body)
                if d2 is not None:        # 命中 → 丢弃并回 403
                    apply_decision(rec, d2)
                    rec["resp_status"] = 403
                    page = render_block_page(engine.cfg.block_page,
                                             d2.rule_id, d2.reason)
                    ctx.writer.send(build_response(403, "Forbidden", [
                        ("Content-Type", "text/html; charset=utf-8"),
                        ("Connection", "close"),
                    ], page))
                    ctx.close_after = True
                    return
            if cache_ok:
                cache.put(head.target, send_h, body)  # 内部再做完整判定
            started = True
            ctx.writer.send(head_bytes)
            ctx.writer.send(body)
        else:                             # 大正文/非常规类型:边读边转
            ctx.writer.send(head_bytes)
            started = True
            remaining = cl
            while remaining > 0:
                b = ureader.read_exact(min(65536, remaining))
                ctx.writer.send(b)
                remaining -= len(b)
        rec["decision"] = "allow"
    except (ProtocolError, ConnectionClosed, socket.timeout, OSError) as e:
        # 上游中断/非法。尚未写出任何响应字节时,尽力回 502;
        # 已开始中继则只能断开(响应流不可回退)。
        if not started:
            ctx.writer.send(make_simple(502, "Bad Gateway",
                                        f"上游错误: {e}"))
        ctx.close_after = True
        rec["decision"] = "error"
        if rec["resp_status"] is None:
            rec["resp_status"] = 502
    finally:
        up.close()
