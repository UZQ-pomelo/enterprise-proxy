"""运维管理 CLI(零第三方依赖):报表 / 排行 / 拦截明细 / 缓存统计。

用法:
    python admin.py report   <audit.db>   [--day YYYY-MM-DD]
    python admin.py top-users <audit.db>  [--day YYYY-MM-DD] [--n 10]
    python admin.py top-hosts <audit.db>  [--day YYYY-MM-DD] [--n 10]
    python admin.py blocked   <audit.db>  [--day YYYY-MM-DD] [--n 20]
    python admin.py cache-stats <cache目录>

默认 db 为 data/audit.db(与 config.toml 的默认一致);渲染为纯文本行,
输出到控制台(UTF-8)。render_* 系列是纯函数,便于脚本与测试复用。
"""
import argparse
import os
import sys

from audit import AuditLog
from cache import scan_stats

_DECISION_LABEL = {
    "allow": "放行",
    "auth_fail": "认证失败",
    "block_whitelist": "拦截·白名单模式",
    "block_blacklist": "拦截·黑名单",
    "block_category": "拦截·站点类别",
    "block_signature": "拦截·请求特征",
    "block_resp_signature": "拦截·响应内容特征",
    "error": "异常/错误",
}


def _fmt_bytes(n: int) -> str:
    n = int(n or 0)
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"


def render_report(db_path: str, day=None) -> list:
    """审计概览:总量 / 决策分布 / 用户请求数排行。"""
    log = AuditLog(db_path)
    try:
        s = log.summary(day=day)
        counts = log.decision_counts(day=day)
        lines = ["=" * 52,
                 f"流量审计报表   库: {db_path}"
                 + (f"    日期: {day}" if day else ""),
                 "=" * 52,
                 f"请求总数      {s['total']}",
                 f"上行流量      {_fmt_bytes(s['up'])}",
                 f"下行流量      {_fmt_bytes(s['down'])}"]
        lines.append("")
        lines.append("决策分布:")
        total = sum(counts.values()) or 1
        for decision, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
            label = _DECISION_LABEL.get(decision, decision)
            lines.append(f"  {label:<16} {cnt:>4}  ({cnt / total:5.1%})")
        return lines
    finally:
        log.close()


def render_top(db_path: str, field: str, day=None, n=10) -> list:
    """按下行流量排行;field ∈ {user, host}。"""
    log = AuditLog(db_path)
    try:
        title = "用户" if field == "user" else "目标主机"
        lines = [f"{title}流量排行    下行     请求数"
                 + (f"    日期: {day}" if day else "")]
        for rank, (value, bd, cnt) in enumerate(log.top(field, day=day, n=n),
                                                start=1):
            lines.append(f"{rank:>2}. {str(value or '(未知)'):<22} "
                         f"{_fmt_bytes(bd):>9}  {cnt:>4}")
        return lines
    finally:
        log.close()


def render_blocked(db_path: str, day=None, n=20) -> list:
    """最近被拦截明细(新→旧):时间/用户/目标/命中规则。"""
    log = AuditLog(db_path)
    try:
        rows = [r for r in log.query(day=day, limit=n * 4)
                if (r["decision"] or "").startswith("block_")]
        lines = ["最近被拦截的请求" + (f"    日期: {day}" if day else "")]
        for r in rows[:n]:
            target = f"{r['host']}{r['url_path']}"
            rule = r["rule_id"] or r["decision"]
            lines.append(f"{r['ts'][11:19]}  {str(r['user'] or '-'):<9} "
                         f"{target:<32} {rule}")
            if r["reason"]:
                lines.append("        ↳ " + (r["reason"] or "")[:60])
        return lines
    finally:
        log.close()


def render_cache_stats(cache_dir: str) -> list:
    """缓存目录离线统计(.body 文件数与字节总量)。"""
    s = scan_stats(cache_dir)
    return ["缓存统计  目录: " + cache_dir,
            f"缓存文件数   {s['files']}",
            f"占用空间     {_fmt_bytes(s['bytes'])}"]


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="admin.py",
        description="企业代理服务器 · 运维管理 CLI(报表/排行/拦截明细/缓存)")
    sub = p.add_subparsers(dest="cmd", metavar="命令")

    def add(cmd, help_text, default):
        sp = sub.add_parser(cmd, help=help_text)
        sp.add_argument("path", nargs="?", default=None,
                        help=f"{default} [省略则用默认路径]")
        sp.add_argument("--day", default=None,
                        help="只统计该日期(YYYY-MM-DD);省略=全部")
        return sp

    add("report", "审计概览:总量/决策分布", "SQLite 审计库")
    for cmd in ("top-users", "top-hosts"):
        sp = add(cmd, f"{cmd} 按下行流量排行", "SQLite 审计库")
        sp.add_argument("--n", type=int, default=10)
    sp = add("blocked", "最近被拦截明细", "SQLite 审计库")
    sp.add_argument("--n", type=int, default=20)
    sp = add("cache-stats", "缓存目录统计", "缓存目录")

    rest = list(sys.argv[1:] if argv is None else argv)
    known = {"report", "top-users", "top-hosts", "blocked", "cache-stats"}
    if not rest or rest[0] not in known:
        p.print_help()
        return 2

    args = p.parse_args(argv)
    if args.cmd is None:
        p.print_help()
        return 2

    defaults = {"report": os.path.join("data", "audit.db"),
                "top-users": os.path.join("data", "audit.db"),
                "top-hosts": os.path.join("data", "audit.db"),
                "blocked": os.path.join("data", "audit.db"),
                "cache-stats": "cache"}   # 与根配置 cache.dir 一致
    try:
        path = args.path or defaults[args.cmd]
        if args.cmd == "report":
            lines = render_report(path, day=args.day)
        elif args.cmd == "top-users":
            lines = render_top(path, "user", day=args.day, n=args.n)
        elif args.cmd == "top-hosts":
            lines = render_top(path, "host", day=args.day, n=args.n)
        elif args.cmd == "blocked":
            lines = render_blocked(path, day=args.day, n=args.n)
        else:  # cache-stats
            lines = render_cache_stats(path)
    except OSError as e:
        print(f"[admin] 无法读取: {e}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows 控制台 GBK 兼容
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
