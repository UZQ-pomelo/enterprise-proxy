"""企业代理启动入口:python run_proxy.py [--config 配置目录] [--port N]

零第三方依赖,仅标准库;Ctrl+C 优雅退出。演示矩阵见 README。
"""
import argparse
import signal
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(description="企业代理服务器(流量审计 + 访问控制)")
    ap.add_argument("--config", default=".", help="配置目录(含 config/users/roles.toml)")
    ap.add_argument("--port", type=int, default=None,
                    help="覆盖监听端口(config.toml 的 [proxy] listen_port)")
    args = ap.parse_args(argv)

    from proxy_server import ProxyServer
    server = ProxyServer(args.config, port=args.port)
    cfg = server.cfg
    print("=" * 64)
    print("企业代理服务器 Enterprise Proxy v1.0  (零第三方依赖,纯标准库)")
    print("=" * 64)
    print(f"  监听       http://{cfg.server.listen_host}:{server.port}")
    print(f"  认证      {'开启(Basic)' if cfg.server.auth_required else '关闭'}")
    print(f"  策略目录   {server.config_dir}")
    print(f"  审计库     {cfg.audit.db_path}")
    print(f"  静态缓存   {'开启 → ' + cfg.cache.dir if cfg.cache.enabled else '关闭'}")
    print(f"  白名单模式 {'开启' if cfg.policy.whitelist_mode else '关闭'}")
    print("  提示       Ctrl+C 停止;配置热加载无需重启")
    print("=" * 64)

    server.start()
    stopping = []

    def _on_sigint(sig, frame):
        stopping.append(1)

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        while not stopping:
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n正在停止…")
        server.stop()
        print("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
