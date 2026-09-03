"""静态资源磁盘缓存:sha1(url) 两级目录 + meta/body 双文件 + 内存 LRU。

缓存判定(规格书 §6.5):GET 静态类型、2xx、无 Set-Cookie、
无 no-store/no-cache/private;过期信息(max-age/Expires)写入 meta,
读取时若已过期则作废。
"""
import hashlib
import json
import os
from datetime import datetime
from email.utils import parsedate_to_datetime

from config import CacheCfg

_META_NAME = ".meta.json"


def type_matches(content_type: str, patterns) -> bool:
    """'image/*' 前缀通配;'text/css' 精确(忽略参数与大小写)。"""
    ct = content_type.split(";")[0].strip().lower()
    if not ct:
        return False
    for p in patterns:
        p = p.strip().lower()
        if p.endswith("/*"):
            if ct.startswith(p[:-1]):
                return True
        elif p == ct:
            return True
    return False


def _entry_paths(cache_dir, url):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    sub = os.path.join(cache_dir, h[:2])
    return os.path.join(sub, h + _META_NAME), os.path.join(sub, h + ".body")


class DiskCache:
    def __init__(self, cfg: CacheCfg):
        self._dir = cfg.dir
        self._max_files = int(cfg.max_files)
        self._max_bytes = int(cfg.max_bytes)
        self._static_types = list(cfg.static_types)
        os.makedirs(self._dir, exist_ok=True)
        self._lru = {}            # url -> last access(单线程访问,无需锁)
        self._bytes = {}          # url -> body 文件大小
        self._hits = 0

    # ---- 判定 ----
    def _forbidden(self, headers) -> bool:
        cc = (headers.get("cache-control") or "").lower()
        if any(x in cc for x in ("no-store", "no-cache", "private")):
            return True
        if headers.get("set-cookie"):
            return True
        return False

    def _expires_ts(self, headers):
        """返回过期时间(ISO 字符串或 None=不设过期)。"""
        cc = (headers.get("cache-control") or "").lower()
        for part in cc.split(","):
            part = part.strip()
            if part.startswith("max-age="):
                try:
                    age = int(part.split("=", 1)[1])
                    return datetime.fromtimestamp(
                        datetime.now().timestamp() + age).isoformat()
                except ValueError:
                    break
        ex = headers.get("expires")
        if ex:
            try:
                dt = parsedate_to_datetime(ex)
                return dt.astimezone().isoformat()
            except (TypeError, ValueError):
                pass
        return None

    def _cacheable(self, headers) -> bool:
        ct = headers.get("content-type") or ""
        if not type_matches(ct, self._static_types):
            return False
        if self._forbidden(headers):
            return False
        return True

    # ---- 存取 ----
    def put(self, url, headers, body) -> bool:
        """满足缓存条件则落盘。headers 为已净化(hop-by-hop 剥离)的响应头。"""
        if not body or not self._cacheable(headers):
            return False
        meta_path, body_path = _entry_paths(self._dir, url)
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        meta = {
            "url": url,
            "status": 200,
            "reason": "OK",
            "expires": self._expires_ts(headers),
            "headers": [[k, v] for k, v in headers],
        }
        try:
            with open(body_path, "wb") as f:
                f.write(body)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except OSError as e:
            print(f"[cache] 写入失败: {e}", file=__import__("sys").stderr)
            return False
        self._lru[url] = datetime.now().timestamp()
        self._bytes[url] = os.path.getsize(body_path)
        self._evict()
        return True

    def get(self, url):
        """命中返回 (status, Headers, body);未命中/已过期返回 None。"""
        meta_path, body_path = _entry_paths(self._dir, url)
        if not (os.path.exists(meta_path) and os.path.exists(body_path)):
            return None
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if meta.get("expires"):
            try:
                if datetime.now() > datetime.fromisoformat(meta["expires"]):
                    self._discard(url)
                    return None
            except ValueError:
                pass
        try:
            with open(body_path, "rb") as f:
                body = f.read()
        except OSError:
            return None
        self._lru[url] = datetime.now().timestamp()
        self._hits += 1
        from http_message import Headers
        return meta.get("status", 200), Headers(meta.get("headers", [])), body

    # ---- 淘汰 ----
    def _discard(self, url):
        meta_path, body_path = _entry_paths(self._dir, url)
        for p in (meta_path, body_path):
            try:
                os.remove(p)
            except OSError:
                pass
        self._lru.pop(url, None)
        self._bytes.pop(url, None)

    def _evict(self):
        total = sum(self._bytes.values())
        while (len(self._lru) > self._max_files or total > self._max_bytes) and self._lru:
            oldest = min(self._lru, key=self._lru.get)
            total -= self._bytes.get(oldest, 0)
            self._discard(oldest)

    def stats(self) -> dict:
        return {"hits": self._hits, "files": len(self._lru),
                "bytes": sum(self._bytes.values())}


def scan_stats(cache_dir) -> dict:
    """离线统计(供 admin CLI):统计 .body 文件数量与字节数。"""
    files = 0
    bytes_total = 0
    for root, _, names in os.walk(cache_dir):
        for n in names:
            if n.endswith(".body"):
                files += 1
                try:
                    bytes_total += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
    return {"files": files, "bytes": bytes_total}
