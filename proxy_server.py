"""监听套接字与连接接纳:一连接一线程(信号量限流)。

ProxyServer 独立线程 accept;stop() 关闭监听使 accept 返回。
启动脚本与测试都用它(测试以端口 0 获取随机端口)。
"""
import socket
import threading

from client_handler import handle_client
from config import load_config_dir
from runtime import ProxyRuntime


class ProxyServer:
    def __init__(self, config_dir=".", *, port=None, max_connections=None):
        self.config_dir = config_dir
        self.cfg = load_config_dir(config_dir)
        if port is not None:
            self.cfg.server.listen_port = port
        self.runtime = ProxyRuntime.make(self.cfg)
        self.ready = threading.Event()
        self._running = False
        self._listener = None
        self._sema = threading.BoundedSemaphore(
            max_connections or int(self.cfg.server.max_connections))
        self._workers = []
        self._workers_lock = threading.Lock()
        self.port = int(self.cfg.server.listen_port)

    # ---- 生命周期 ----
    def start(self):
        self._running = True
        t = threading.Thread(target=self._serve,
                             name=f"proxy-accept-{self.port}", daemon=True)
        t.start()

    def _serve(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((self.cfg.server.listen_host, self.port))
        ls.listen(128)
        self._listener = ls
        self.port = ls.getsockname()[1]  # 端口 0 时回填实际端口
        self.ready.set()
        try:
            while self._running:
                try:
                    conn, addr = ls.accept()
                except OSError:
                    break
                if not self._sema.acquire(blocking=False):  # 过载直接拒绝
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                t = threading.Thread(target=self._worker, args=(conn, addr),
                                     daemon=True)
                with self._workers_lock:
                    self._workers.append(t)
                t.start()
        finally:
            try:
                ls.close()
            except OSError:
                pass

    def _worker(self, conn, addr):
        try:
            handle_client(self.runtime, conn, addr)
        finally:
            self._sema.release()
            try:
                conn.close()
            except OSError:
                pass
            with self._workers_lock:
                self._workers.remove(threading.current_thread())

    def stop(self, drain=1.0):
        """停止:不再 accept → 排干存量连接线程 → 关审计库(Windows 文件锁)。"""
        self._running = False
        if self._listener is not None:
            try:
                self._listener.close()   # accept 随即返回
            except OSError:
                pass
        deadline = __import__("time").time() + drain
        while __import__("time").time() < deadline:
            with self._workers_lock:
                if not self._workers:
                    break
            __import__("time").sleep(0.02)
        self.runtime.audit.close()
