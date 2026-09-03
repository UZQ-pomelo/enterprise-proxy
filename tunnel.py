"""HTTPS CONNECT 隧道:策略检查后双向字节泵,隧道载荷不走审计计数器。"""
import socket
import threading

from http_message import make_simple, parse_authority
from runtime import apply_decision


def handle_connect(ctx, head, rec):
    """处理 CONNECT 请求:允许 → 200 + 双向泵;拦截 → 静默断开。

    隧道建立后的载荷为加密字节,代理只做位置透明转发(内容检查无从
    下手,属规格书规定的设计取舍);流量字节计数累积到 rec 的
    _extra_up/_extra_down,由 finish_rec 并入审计。
    """
    engine = ctx.runtime.engine
    try:
        host, port = parse_authority(head.target)
    except Exception as e:
        rec["decision"] = "error"
        ctx.close_after = True
        return
    rec["host"], rec["port"] = host, port

    d = engine.classify(host=host, path="", url_text=head.target,
                        headers_text="", role_name=ctx.role)
    if d.action == "block":
        apply_decision(rec, d)   # 不返回 200,直接断开
        ctx.close_after = True
        return

    cfg = ctx.runtime.cfg
    try:
        up = socket.create_connection((host, port),
                                      timeout=cfg.server.recv_timeout)
    except OSError as e:
        ctx.writer.send(make_simple(502, "Bad Gateway",
                                    f"连接目标失败 {host}:{port}: {e}"))
        ctx.close_after = True
        rec["decision"] = "error"
        rec["resp_status"] = 502
        return

    ctx.sock.settimeout(cfg.server.recv_timeout)
    up.settimeout(cfg.server.recv_timeout)
    rec["_extra_up"] = 0
    rec["_extra_down"] = 0
    rec["resp_status"] = None   # 隧道无 HTTP 状态语义;审计列留空
    try:
        ctx.writer.send(b"HTTP/1.1 200 Connection established\r\n\r\n")
        stop = threading.Event()

        def pump(src, dst, key):
            """单向泵:EOF/超时/异常 → shutdown(SHUT_WR) 通知对端结束。"""
            try:
                while not stop.is_set():
                    try:
                        data = src.recv(65536)
                    except socket.timeout:
                        break          # 空闲超时视为结束,不留僵尸线程
                    if not data:
                        break
                    rec[key] = rec.get(key, 0) + len(data)
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                stop.set()
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(target=pump, args=(ctx.sock, up, "_extra_up"),
                              daemon=True)
        t2 = threading.Thread(target=pump, args=(up, ctx.sock, "_extra_down"),
                              daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        rec["decision"] = "allow"
    finally:
        up.close()
