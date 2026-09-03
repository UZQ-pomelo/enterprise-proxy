"""配置装载与配置模型。

三个 TOML 文件(config.toml / users.toml / roles.toml)构成全部策略数据,
用 Python 3.11+ 标准库 tomllib 解析,保持零第三方依赖。
角色缺省语义:某键在角色中缺失 → 回退全局;显式给出空数组/布尔 → 以角色为准。
"""
import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_BLOCK_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>访问被拦截</title>
<style>
body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#f5f6fa;display:flex;
height:100vh;margin:0;align-items:center;justify-content:center}
.card{background:#fff;padding:48px 56px;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.12);
text-align:center;max-width:560px}
h1{color:#d33;font-size:26px;margin:0 0 12px}
.desc{color:#555;line-height:1.8;margin-bottom:20px}
.rule{display:inline-block;background:#fdeaea;color:#b02a2a;border-radius:6px;
padding:6px 14px;font-family:Consolas,monospace;font-size:13px}
.foot{color:#999;font-size:12px;margin-top:24px}
</style></head>
<body><div class="card">
<h1>访问被拦截</h1>
<div class="desc">该请求违反了企业网络访问策略,已被代理服务器拦截。<br>
请勿尝试绕过,如有疑问请联系网络管理员。</div>
<div class="rule">规则: __RULE_ID__</div>
<p class="desc" style="margin-top:16px">原因: __REASON__</p>
<div class="foot">Enterprise Proxy · Traffic Audit &amp; Access Control</div>
</div></body></html>
"""


class ConfigError(Exception):
    """配置装载错误。"""


@dataclass
class ServerCfg:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    max_connections: int = 64
    recv_timeout: float = 60.0
    idle_timeout: float = 60.0
    auth_required: bool = True


@dataclass
class AuditCfg:
    db_path: str = "data/audit.db"


STATIC_TYPES_DEFAULT = ["image/*", "text/css", "application/javascript", "font/*"]


@dataclass
class CacheCfg:
    enabled: bool = True
    dir: str = "cache"
    max_files: int = 500
    max_bytes: int = 209715200  # 200MB
    static_types: list = field(default_factory=lambda: list(STATIC_TYPES_DEFAULT))


@dataclass
class PolicyCfg:
    whitelist_mode: bool = False
    whitelist: list = field(default_factory=list)
    blacklist_domains: list = field(default_factory=list)
    blacklist_keywords: list = field(default_factory=list)
    blacklist_url_regex: list = field(default_factory=list)
    disabled_categories: list = field(default_factory=list)
    request_signatures: list = field(default_factory=list)
    response_signatures: list = field(default_factory=list)


@dataclass
class UserCfg:
    name: str
    password: str
    role: str


@dataclass
class RolePolicy:
    name: str
    whitelist_mode: bool | None = None
    disabled_categories: list | None = None
    request_signatures: list | None = None
    response_signatures: list | None = None


@dataclass
class Config:
    server: ServerCfg
    audit: AuditCfg
    cache: CacheCfg
    policy: PolicyCfg
    users: dict
    roles: dict
    block_page: bytes
    config_dir: str
    _watch: list = field(default_factory=list)  # [(path, mtime_ns, size)]

    def is_changed(self) -> bool:
        """任一被监视的配置文件是否已被修改(热加载探测)。

        同时比较 mtime 与文件大小:Windows 文件系统时间戳分辨率可能较粗,
        同刻度内的增删改会体现为大小变化。
        """
        for path, old_mt, old_size in self._watch:
            try:
                st = os.stat(path)
            except OSError:
                return True
            if st.st_mtime_ns != old_mt or st.st_size != old_size:
                return True
        return False


def _str(data, key, default):
    v = data.get(key, default)
    return v if isinstance(v, str) else default


def _num(data, key, default):
    v = data.get(key, default)
    return v if isinstance(v, (int, float)) else default


def _bool(data, key, default):
    v = data.get(key, default)
    return v if isinstance(v, bool) else default


def _list(data, key, default):
    v = data.get(key, default)
    return list(v) if isinstance(v, list) else default


def _as_table(data, key):
    v = data.get(key)
    return v if isinstance(v, dict) else {}


def _load_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def _mtime_ns(path):
    return os.stat(path).st_mtime_ns


def load_config_dir(config_dir: str = ".") -> Config:
    cfg_path = os.path.join(config_dir, "config.toml")
    users_path = os.path.join(config_dir, "users.toml")
    roles_path = os.path.join(config_dir, "roles.toml")
    missing = [p for p in (cfg_path, users_path, roles_path) if not os.path.exists(p)]
    if missing:
        raise ConfigError(f"缺少配置文件: {', '.join(missing)}")
    try:
        data = _load_toml(cfg_path)
        users_data = _load_toml(users_path)
        roles_data = _load_toml(roles_path)
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"TOML 解析失败: {e}") from e

    srv = _as_table(data, "proxy")
    server = ServerCfg(
        listen_host=_str(srv, "listen_host", ServerCfg.listen_host),
        listen_port=_num(srv, "listen_port", ServerCfg.listen_port),
        max_connections=_num(srv, "max_connections", ServerCfg.max_connections),
        recv_timeout=float(_num(srv, "recv_timeout", ServerCfg.recv_timeout)),
        idle_timeout=float(_num(srv, "idle_timeout", ServerCfg.idle_timeout)),
        auth_required=_bool(srv, "auth_required", ServerCfg.auth_required),
    )
    aud = _as_table(data, "audit")
    audit = AuditCfg(db_path=_str(aud, "db_path", AuditCfg.db_path))
    cac = _as_table(data, "cache")
    cache = CacheCfg(
        enabled=_bool(cac, "enabled", CacheCfg.enabled),
        dir=_str(cac, "dir", CacheCfg.dir),
        max_files=_num(cac, "max_files", CacheCfg.max_files),
        max_bytes=_num(cac, "max_bytes", CacheCfg.max_bytes),
        static_types=_list(cac, "static_types", STATIC_TYPES_DEFAULT),
    )
    pol = _as_table(data, "policy")
    policy = PolicyCfg(
        whitelist_mode=_bool(pol, "whitelist_mode", False),
        whitelist=_list(pol, "whitelist", []),
        blacklist_domains=_list(pol, "blacklist_domains", []),
        blacklist_keywords=_list(pol, "blacklist_keywords", []),
        blacklist_url_regex=_list(pol, "blacklist_url_regex", []),
        disabled_categories=_list(pol, "disabled_categories", []),
        request_signatures=_list(pol, "request_signatures", []),
        response_signatures=_list(pol, "response_signatures", []),
    )

    users = {}
    for name, tbl in _as_table(users_data, "users").items():
        users[name] = UserCfg(name=name,
                              password=str(tbl.get("password", "")),
                              role=str(tbl.get("role", "")))

    roles = {}
    for name, tbl in _as_table(roles_data, "roles").items():
        roles[name] = RolePolicy(
            name=name,
            whitelist_mode=tbl.get("whitelist_mode"),
            disabled_categories=_opt_list(tbl.get("disabled_categories")),
            request_signatures=_opt_list(tbl.get("request_signatures")),
            response_signatures=_opt_list(tbl.get("response_signatures")),
        )

    block_page_path = os.path.join(config_dir, _str(data, "block_page", "block_page.html"))
    block_page = DEFAULT_BLOCK_PAGE.encode("utf-8")
    if os.path.exists(block_page_path):
        with open(block_page_path, "rb") as f:
            block_page = f.read()

    watch_paths = [cfg_path, users_path, roles_path]
    watch = [(p, _mtime_ns(p), os.path.getsize(p)) for p in watch_paths]

    return Config(server=server, audit=audit, cache=cache, policy=policy,
                  users=users, roles=roles, block_page=block_page,
                  config_dir=config_dir, _watch=watch)


def _opt_list(v):
    """角色表值:缺省(非列表)→ None 表示回退全局;显式列表(含空)→ 以角色为准。"""
    if not isinstance(v, list):
        return None
    return list(v)
