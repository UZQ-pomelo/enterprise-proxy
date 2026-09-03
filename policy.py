"""策略引擎:四层规则(白名单模式/黑名单/类别/特征)的装载、判定与热加载。

判定优先级(一次判定只出一个结果,见规格书 §6.3):
    白名单模式 → 黑名单(域名/关键词/正则) → 类别(角色禁用) → 请求特征 → 放行
响应侧单独走 check_response(仅 text/* 内容,由调用方保证尺寸与类型前提)。
"""
import re
from dataclasses import dataclass

import categories as categories_mod
from config import Config, load_config_dir


@dataclass(frozen=True)
class Decision:
    action: str    # "allow" | "block"
    rule_type: str  # "none"|"whitelist"|"blacklist"|"category"|"signature"|"resp_signature"
    rule_id: str = ""
    reason: str = ""


ALLOW = Decision("allow", "none")


def _match_entry(host: str, entry: str) -> bool:
    """域名条目匹配:'*.x' 匹配 x 及其任意子域;普通条目精确匹配。"""
    e = entry.strip().lower()
    if not e:
        return False
    if e.startswith("*."):
        rest = e[2:]
        return host == rest or host.endswith("." + rest)
    return host == e


class PolicyEngine:
    """持有 Config,负责规则判定;reload_if_changed() 在每个请求开始时被调用。"""

    def __init__(self, cfg: Config):
        self.config_dir = cfg.config_dir
        self.cfg = cfg
        self._recompile()

    def _recompile(self):
        p = self.cfg.policy
        self._black_re = [re.compile(x, re.I) for x in p.blacklist_url_regex]
        # 全局与每个角色的合并特征表 → 预编译
        self._req_sigs = {}
        self._resp_sigs = {}
        for name in list(self.cfg.roles) + [None]:
            merged = self._merged(name, "request_signatures")
            self._req_sigs[name] = [re.compile(x, re.I) for x in merged]
            merged = self._merged(name, "response_signatures")
            self._resp_sigs[name] = [re.compile(x, re.I) for x in merged]

    def reload_if_changed(self):
        if self.cfg.is_changed():
            try:
                self.cfg = load_config_dir(self.config_dir)
                self._recompile()
            except Exception as e:  # 配置损坏时保留旧规则并告警
                print(f"[policy] 配置重载失败,沿用旧规则: {e}", file=__import__("sys").stderr)

    def _merged(self, role_name, attr):
        """角色合并语义:角色显式给出(非 None)→ 用之;缺失 → 回退全局。"""
        rp = self.cfg.roles.get(role_name) if role_name else None
        if rp is not None:
            v = getattr(rp, attr)
            if v is not None:
                return v
        return getattr(self.cfg.policy, attr)

    @staticmethod
    def _sigs(table, role_name):
        """特征表查找:角色未定义 → 回退全局;定义角色显式空数组 → 绕过。"""
        if role_name in table:
            return table[role_name]
        return table.get(None, [])

    def _whitelist_mode(self, role_name) -> bool:
        rp = self.cfg.roles.get(role_name) if role_name else None
        if rp is not None and rp.whitelist_mode is not None:
            return rp.whitelist_mode
        return self.cfg.policy.whitelist_mode

    def classify(self, *, host, path, url_text, headers_text, role_name) -> Decision:
        """请求侧判定。url_text=完整目标串(带协议),headers_text=请求头文本。"""
        cfg = self.cfg.policy

        # 1) 白名单模式
        if self._whitelist_mode(role_name):
            if not any(_match_entry(host, e) for e in cfg.whitelist):
                return Decision("block", "whitelist", "whitelist_mode",
                                "企业白名单模式:仅允许登记域名访问")

        # 2) 黑名单(全局红线)
        for entry in cfg.blacklist_domains:
            if _match_entry(host, entry):
                return Decision("block", "blacklist", f"domain:{entry}",
                                f"域名 {host} 在全局黑名单中")
        low = url_text.lower()
        for kw in cfg.blacklist_keywords:
            if kw.lower() in low:
                return Decision("block", "blacklist", f"keyword:{kw}",
                                "URL 命中黑名单关键词")
        for i, rx in enumerate(self._black_re):
            if rx.search(url_text):
                return Decision("block", "blacklist", f"regex:{i}",
                                "URL 命中黑名单正则")

        # 3) 类别(角色禁用)
        disabled = set(self._merged(role_name, "disabled_categories"))
        if disabled:
            hit = sorted(categories_mod.tag(host, path) & disabled)
            if hit:
                cat = hit[0]
                return Decision("block", "category", f"category:{cat}",
                                f"站点类别 [{cat}] 已被角色 {role_name} 禁用")

        # 4) 请求特征
        blob = f"{url_text}\r\n{headers_text}"
        for i, rx in enumerate(self._sigs(self._req_sigs, role_name)):
            if rx.search(blob):
                return Decision("block", "signature", f"req_sig:{i}",
                                "请求特征命中(URL/头内容)")

        return ALLOW

    def check_response(self, *, role_name, content_type, body) -> Decision | None:
        """响应体特征检查。返回 Decision(block)或 None(放行)。"""
        if not (body and isinstance(content_type, str)
                and content_type.lower().startswith("text/")):
            return None
        text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
        for i, rx in enumerate(self._sigs(self._resp_sigs, role_name)):
            if rx.search(text):
                return Decision("block", "resp_signature", f"resp_sig:{i}",
                                "响应内容特征命中(违规内容)")
        return None
