"""运行期装配与每请求审计记录生命周期(避免模块循环依赖的容器层)。"""
import socket
import time
from dataclasses import dataclass

from audit import AuditLog
from cache import DiskCache
from config import Config
from http_message import BufferedSocketReader, ByteCounter
from policy import PolicyEngine


@dataclass
class ProxyRuntime:
    """一次进程运行期的全局装配(所有连接共享)。"""
    cfg: Config
    engine: PolicyEngine
    audit: AuditLog
    cache: DiskCache | None

    @classmethod
    def make(cls, cfg: Config) -> "ProxyRuntime":
        engine = PolicyEngine(cfg)
        audit = AuditLog(cfg.audit.db_path)
        cache = DiskCache(cfg.cache) if cfg.cache.enabled else None
        return cls(cfg=cfg, engine=engine, audit=audit, cache=cache)


@dataclass
class RuntimeCtx:
    """每连接上下文:认证缓存与读写通道。"""
    runtime: ProxyRuntime
    sock: socket.socket
    reader: BufferedSocketReader
    writer: ByteCounter
    user: str | None = None
    role: str | None = None
    close_after: bool = False


def new_rec(ctx: RuntimeCtx, head=None) -> dict:
    """新建一条审计记录;字节/耗时在 finish_rec 时按计数器差值计算。"""
    method = getattr(head, "method", None) or "-"
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": ctx.user, "role": ctx.role,
        "method": method, "host": "", "url_path": "", "port": None,
        "decision": None, "rule_id": "", "reason": "",
        "resp_status": None, "cache_hit": 0,
        "_r0": ctx.reader.bytes_read,      # 内部键:审计落库时忽略
        "_w0": ctx.writer.bytes,
        "_t0": time.monotonic(),
    }


def apply_decision(rec: dict, d) -> None:
    """把策略 Decision 落到审计记录(decision/rule_id/reason)。"""
    if d.action == "block":
        rec["decision"] = f"block_{d.rule_type}"
        rec["rule_id"] = d.rule_id
        rec["reason"] = d.reason


def finish_rec(ctx: RuntimeCtx, rec: dict) -> None:
    """请求结束:回填字节计数(口径:客户端↔代理全部字节)与耗时。

    user/role 在 new_rec 之后(认证通过时)才落 ctx,故在此刻刷新;
    未认证/认证失败的行保持 None(审计口径:未知用户)。
    """
    rec["user"] = ctx.user
    rec["role"] = ctx.role
    rec["bytes_up"] = ctx.reader.bytes_read - rec.pop("_r0", 0) \
        + rec.pop("_extra_up", 0)
    rec["bytes_down"] = ctx.writer.bytes - rec.pop("_w0", 0) \
        + rec.pop("_extra_down", 0)
    rec["duration_ms"] = int((time.monotonic() - rec.pop("_t0", 0)) * 1000)
    if rec["decision"] is None:
        rec["decision"] = "error"
