"""每连接请求循环:热加载 → 解析头 → 认证 → 按方法分派 → 审计落库。

架构(规格书 §5.1):一个连接一个守护线程;HTTP/1.1 keep-alive 循环,
每请求审计一条记录;响应要求关闭(403/502/无定界体等)时置
ctx.close_after,循环随即结束。
"""
import socket

import forward
import tunnel
from auth import authenticate
from http_message import (BufferedSocketReader, ByteCounter, ConnectionClosed,
                          ProtocolError, make_407, make_simple,
                          read_request_head)
from runtime import RuntimeCtx, finish_rec, new_rec

_MAX_REASON = 200


def handle_client(runtime, sock, addr):
    """处理一条客户端连接(直到空闲超时/关闭/策略要求断开)。"""
    cfg = runtime.cfg
    sock.settimeout(cfg.server.idle_timeout)
    ctx = RuntimeCtx(runtime=runtime, sock=sock,
                     reader=BufferedSocketReader(sock),
                     writer=ByteCounter(sock))
    try:
        while not ctx.close_after:
            runtime.engine.reload_if_changed()
            rec = new_rec(ctx)          # 基线先于本请求任何读取
            try:
                head = read_request_head(ctx.reader)
            except ConnectionClosed:
                return                  # 空闲关闭,无完整请求可记
            except socket.timeout:
                return                  # 空闲超时
            except ProtocolError as e:  # 头格式非法/超长
                rec["decision"] = "error"
                rec["reason"] = str(e)[:_MAX_REASON]
                finish_rec(ctx, rec)
                runtime.audit.add(rec)
                return
            rec["method"] = head.method

            # 认证(失败直接 407,不触达任何策略与上游)
            if cfg.server.auth_required:
                user = authenticate(head.headers, cfg.users)
                if user is None:
                    ctx.writer.send(make_407())
                    rec["decision"] = "auth_fail"
                    rec["resp_status"] = 407
                    finish_rec(ctx, rec)
                    runtime.audit.add(rec)
                    return
                ctx.user = user          # new_rec/finish_rec 由此读取
                ctx.role = cfg.users[user].role

            try:
                if head.method == "CONNECT":
                    tunnel.handle_connect(ctx, head, rec)
                else:
                    forward.handle_http(ctx, head, rec)
            except (ProtocolError, ConnectionClosed, OSError) as e:
                # 客户端中断 / 目标协议非法等:尚未写出任何字节时回 400
                if ctx.writer.bytes == rec.get("_w0", ctx.writer.bytes):
                    try:
                        ctx.writer.send(make_simple(400, "Bad Request",
                                                    f"代理无法处理: {e}"))
                    except OSError:
                        pass
                rec["decision"] = "error"
                rec["reason"] = f"{type(e).__name__}: {e}"[:_MAX_REASON]
                ctx.close_after = True

            finish_rec(ctx, rec)
            runtime.audit.add(rec)
    finally:
        try:
            sock.close()
        except OSError:
            pass
