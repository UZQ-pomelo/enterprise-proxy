"""网站类别词库:按域名关键词与路径关键词给 URL 打类别标签。

演示词库刻意克制(避免把真实域名误伤);生产部署可扩充词表。
类别维度与 roles.toml 中 disabled_categories 取值一致。
"""
from collections import OrderedDict

CATEGORY_LABELS = ("game", "shopping", "social", "gambling", "news", "video")

# 域名关键词(子串匹配,不区分大小写)
DOMAIN_KEYWORDS = OrderedDict([
    ("game", ("game", "play", "steam", "uplay", "epicgames")),
    ("shopping", ("shop", "mall", "taobao", "tmall", "jd.com", "buy")),
    ("social", ("weibo", "tieba", "douban", "bbs", "facebook", "twitter", "reddit")),
    ("gambling", ("bet", "casino", "lottery", "poker", "baccarat", "gambl")),
    ("news", ("news", "sina", "163.com", "sohu")),
    ("video", ("video", "bilibili", "youtube", "iqiyi", "douyin", "tv")),
])

# 路径关键词(子串匹配 path)
PATH_KEYWORDS = OrderedDict([
    ("game", ("/game", "/play", "/lobby")),
    ("shopping", ("/cart", "/mall", "/buy", "/checkout")),
    ("video", ("/video", "/live")),
])


def tag(host: str, path: str) -> set:
    """返回 host/path 命中的全部类别标签集合。"""
    tags = set()
    hl = host.lower()
    for cat, kws in DOMAIN_KEYWORDS.items():
        if any(kw in hl for kw in kws):
            tags.add(cat)
    pl = path.lower()
    for cat, kws in PATH_KEYWORDS.items():
        if any(kw in pl for kw in kws):
            tags.add(cat)
    return tags
