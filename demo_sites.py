"""离线演示站点矩阵:为 4 个演示域名在固定端口起纯本地 HTTP 服务。

配合 tools/*.ps1 的 hosts 映射,浏览器/curl 访问演示域名时实际落在
127.0.0.1,整个演示不依赖外网。站点内容刻意与角色演示矩阵呼应:
corp-doc 是"内部文档站"(白名单候选),game/shop 是员工禁用类别站。
"""
import socketserver
import threading

SITES = {
    "corp-doc.com":        {"port": 18001, "title": "公司内部文档中心",
                            "lines": ("新员工入职手册", "网络使用规范(2026 版)",
                                      "信息安全培训材料", "项目周报模板")},
    "game-site.com":       {"port": 18002, "title": "在线游戏门户",
                            "lines": ("热门游戏推荐", "每日签到领奖励",
                                      "公会开黑房间")},
    "shop.example":        {"port": 18003, "title": "购物商城",
                            "lines": ("今日特惠", "限时秒杀",
                                      "全场包邮活动")},
    "blocked-site.example": {"port": 18004, "title": "不良信息聚合站",
                             "lines": ("恶意软件下载", "违规内容分享")},
}

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 +
       b"IDAT" + b"\x00" * 60 + b"IEND\xaeB`\x82")


def page_html(title: str, lines, host: str) -> bytes:
    lis = "".join(f"<li>{x}</li>" for x in lines)
    return (f"<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head><meta "
            f"charset=\"utf-8\"><title>{title}</title></head>\n<body>\n"
            f"<h1>{title}</h1><p>演示站点(离线) · {host}</p>\n<ul>{lis}</ul>\n"
            "</body>\n</html>\n").encode("utf-8")


def _read_head(request):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = request.recv(65536)
        if not chunk:
            break
        buf += chunk
    first = buf.split(b"\r\n", 1)[0].decode("iso-8859-1", "ignore")
    parts = first.split(" ")
    return (parts[1] if len(parts) > 1 else "/", parts[0] if parts else "GET")


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        path, method = _read_head(self.request)
        site = self.server.site
        if path.startswith("/static/logo.png"):
            head = ("HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                    "Cache-Control: max-age=3600\r\n"
                    f"Content-Length: {len(PNG)}\r\nConnection: close\r\n\r\n")
            self.request.sendall(head.encode() + PNG)
        elif path == "/":
            body = page_html(site["title"], site["lines"],
                             self.server.site_name)
            head = ("HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    "Content-Length: " + str(len(body)) +
                    "\r\nConnection: close\r\n\r\n")
            self.request.sendall(head.encode("iso-8859-1") + body)
        else:
            self.request.sendall(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n")


class DemoServer:
    """一键在 127.0.0.1 上按 SITES 表起全部演示站点。"""

    def __init__(self, port_base=None):
        self._servers = []
        self.ports = {}   # 站点名 → 实际监听端口
        for name, cfg in SITES.items():
            port = cfg["port"] if port_base is None \
                else port_base + (cfg["port"] - 18001)
            srv = socketserver.ThreadingTCPServer(("127.0.0.1", port),
                                                  _Handler)
            srv.site = cfg
            srv.site_name = name
            self._servers.append(srv)
            self.ports[name] = srv.server_address[1]

    def start(self):
        for srv in self._servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop(self):
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()


def main():
    import signal
    import sys
    print("离线演示站点矩阵(127.0.0.1):")
    for name, cfg in SITES.items():
        print(f"  http://{name}:{cfg['port']}/   ← {cfg['title']}")
    print("先以管理员运行 tools/setup_hosts.ps1 建立 hosts 映射;"
          "Ctrl+C 停止")
    demo = DemoServer()
    demo.start()
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        demo.stop()


if __name__ == "__main__":
    main()
