# 《具备流量审计的企业代理服务器》设计文档

- 日期:2026-09-03
- 状态:已批准(组内评审通过)
- 对应课程任务书:2026 秋《网络工程项目实施》题目 4(主线 + 静态缓存加分项)

## 1. 背景与目标

企业场景下,员工工作电脑可能访问违规网站。本系统实现一台**显式正向代理服务器**,员工终端将 HTTP/HTTPS 代理配置指向本服务器,所有上网流量经过代理;服务器解析请求,依据自定义过滤规则(黑白名单、网站类别、特征数据)精准拦截违规流量,同时完成**流量审计**(完整日志 + 按用户/角色/时间的统计报表)与**基于角色的权限控制**。加分项:对高频静态资源提供简易本地缓存。

设计约束(组内确认):
- 语言:Python 3,核心功能仅用标准库(`socket`/`threading`/`sqlite3`/`http`/`ssl` 等),便于分工讲解与在课程环境演示;不引入第三方 Web 框架。
- 运行平台:Windows 11(亦兼容 Linux),演示为单机闭环。
- 演示不依赖外网:内置"模拟外网站点 + hosts 域名映射"的离线演示环境,同时支持真实外网代理。

## 2. 需求映射

| 任务书要求 | 本设计交付 |
|---|---|
| 客户端必须配置代理服务器才能上网 | 显式正向代理,客户端代理配置指向 8080;演示含浏览器/curl 两种客户端 |
| 服务器解析用户网络请求 | 自实现 HTTP 请求行/请求头解析 + CONNECT 解析(不依赖现成代理库) |
| 自定义过滤规则:黑白名单 | 黑名单(域名/URL/正则);白名单模式(仅放行名单内) |
| 特定网站类型 | 内置类别词库(游戏/购物/社交/赌博/新闻等),可按角色整体禁类 |
| 特定特征数据 | 正则匹配请求头/URL;明文 HTTP 响应体特征检查(限 text/*、限尺寸) |
| 精准拦截 | 拦截时浏览器收到 403 拦截页(含规则号与原因),同时记审计日志 |
| 日志记录 | 每请求一条 SQLite 记录 + 管理 CLI 报表(按天/用户/域名/拦截排行) |
| 根据角色分配权限 | Basic Auth 认证 → 用户映射角色 → 角色映射策略集合 |
| 加分:静态资源简易本地缓存 | 磁盘缓存 + LRU + Cache-Control 尊重,命中在日志标注 |

## 3. 范围与非目标

**做(主线全套 + 加分缓存):**
- 明文 HTTP 正向代理(请求解析、策略判定、上游转发、响应透传)
- HTTPS CONNECT 隧道(按目标域名做策略判定,隧道内数据不解析)
- 黑白名单 / 类别 / 特征规则四层过滤;规则与用户热加载
- Basic Auth 角色权限;SQLite 审计;管理 CLI;403 拦截页
- 静态资源缓存;单机离线演示环境;单元与端到端测试

**明确不做(记录取舍理由,写入报告"局限"节):**
- HTTPS 内容解密审计(MITM:需自建 CA 并植入客户端信任区)——列为可扩展项,域名级过滤对 HTTPS 仍然生效
- 透明代理/强制网关(需内核层,WFP 驱动签名门槛高)——课堂采用客户端显式配置
- 上游连接复用、HTTP/2、代理链——与课程评分点无关,徒增复杂度

## 4. 总体架构

```
                 ┌────────────────────── 代理服务器(单进程,线程模型) ──────────────────────┐
员工浏览器 ──TCP──▶ [client_handler 每连接一线程]                                            │
                 │    1. 认证: Proxy-Authorization Basic → user → role                     │
                 │    2. 读请求: HTTP 绝对 URI / CONNECT host:port                          │
                 │    3. 策略判定(放行/拦截)                          ┌── cache(命中直回)   │
                 │        放行 ──▶ forward/tunnel ──▶ 上游 目标服务器 ─┘                    │
                 │        拦截 ──▶ 403 拦截页(关闭或保持连接按协议)                          │
                 │    4. 每请求写 audit(SQLite,独立连接,失败不影响转发)                     │
                 └──────────────────────────────────────────────────────────────────────────┘
   管理 CLI(admin.py):报表查询 / 规则热加载触发 / 用户角色管理(演示期以直接改 YAML+热加载为主)
```

进程模型:单进程。1 个 acceptor 线程 + 每客户端连接 1 个处理线程;全局信号量限制并发 64。阻塞 socket + 读写超时。`Ctrl+C` 优雅退出(停止 accept、等待在途请求完成或超时、关闭审计连接)。

## 5. 模块设计(文件即模块)

| 文件 | 职责 | 对外接口 | 依赖 |
|---|---|---|---|
| `run_proxy.py` | 入口:解析命令行、装载配置、启动 acceptor、信号处理 | `main()` | 其余全部 |
| `runtime.py` | 运行期装配:ProxyRuntime(engine/audit/cache/cfg)与每连接 RuntimeCtx(reader/writer/用户/角色/关闭标记)的容器定义,避免模块循环依赖 | `ProxyRuntime`, `RuntimeCtx`, `new_rec(ctx, head)` | config/policy/audit/cache |
| `proxy_server.py` | acceptor 循环、线程池(信号量)、监听 socket、优雅退出 | `start(config)` / `stop()` | — |
| `client_handler.py` | 每连接:认证、请求循环(keep-alive)、超时、请求级派发 | `handle_connection(sock, config, audit)` | auth/http_message/policy/forward/tunnel/audit/cache |
| `http_message.py` | 增量解析请求行+请求头;Content-Length body 读取;响应行+响应头解析;chunked 透传(不重组);工具函数 | `read_request_head()`, `read_body_exact()`, `parse_response_head()`, `is_chunked()`, `parse_url()` | — |
| `auth.py` | Basic 认证解码校验;user→role;缓存已认证连接的用户 | `authenticate(headers, users) -> User|None` | — |
| `policy.py` | 规则装载(YAML)与判定;四层规则;热加载(60s mtime 检查);拦截原因/规则号 | `classify(req_meta, user, roles_cfg) -> Decision` | categories |
| `categories.py` | 内置类别词库(域名关键词/URL 关键词 → 类别列表) | `tag(host, path) -> set[str]` | — |
| `forward.py` | 明文 HTTP 上游转发:建连、写请求、读响应头、逐块转发响应体、字节统计、响应体特征检查钩子、缓存写入钩子 | `forward_request(...) -> stats` | http_message/cache/audit |
| `tunnel.py` | CONNECT:连上游、回 200、双向 select 转发、字节统计、关闭传播 | `relay_tunnel(client, upstream, audit_ctx)` | — |
| `audit.py` | SQLite 连接管理(每线程独立连接)、INSERT、查询函数、日报表 | `AuditLog` 类 | sqlite3 |
| `cache.py` | 磁盘缓存:sha1(url) key、LRU(容量/文件数上限)、Cache-Control 校验 | `try_get(url) / put(url, headers, body) / stats()` | — |
| `admin.py` | CLI:报表(今日/按天/用户 TOP/拦截排行)、缓存状态、规则重载提示 | 命令行子命令 | audit/policy/cache |
| `block_page.html` | 403 拦截页模板 | 静态文件 | — |
| `demo_sites.py` | 离线演示:起 3 个模拟"外网站点"(文档站/游戏站/购物站/含违规特征页) | 直接运行 | http.server |
| `config.py` | TOML 装载与配置模型(Config/ServerCfg/PolicyCfg/CacheCfg…),含热加载文件变更探测 | `load_config_dir(dir) -> Config`、`Config.is_changed() -> bool` | tomllib(标准库) |
| `config.toml` / `users.toml` / `roles.toml` | 见 §7。注:配置文件采用 **TOML**(Python ≥3.11 标准库 tomllib 解析,保持零第三方依赖);键结构与本设计 §7 一致,便于演示期手改与热加载 | — | — |
| `tests/` | `test_http_message.py`、`test_policy.py`、`test_auth.py`、`test_cache.py`、`e2e_proxy.py` | — | unittest |

设计原则:每模块单一职责、只通过函数/小类互相调用、可单独单元测试;转发主路径不放第三方依赖。

## 6. 关键流程

### 6.1 明文 HTTP 转发
1. 读请求行+请求头(超时 60s)。请求行须为绝对 URI 形式(`GET http://host[:port]/path HTTP/1.1`),否则回 400。
2. 认证失败:回 `407 Proxy Authentication Required`(仅对已启用认证且请求带认证头的处理顺序先于策略)。
3. 策略判定。拦截:回 403 + 拦截页,写审计,按 Connection 头决定关闭或读弃后续(实现:置"已拦截"标记,继续读完请求体后关闭)。
4. 放行:连上游(host:port,默认 80);转写请求头(绝对 URI 改写为 origin-form,补 `Connection: close` 后丢弃原 Connection/Proxy-* 头);按 Content-Length 或 chunked 转发请求体;读上游响应头,做响应侧处理(特征检查/缓存写),随后逐块转发响应体,统计上下行字节。
5. 上游每请求新建连接,不复用(理由:实现简单稳定、避免连接泄漏与 half-close 状态机;课堂规模无性能问题)。
6. 响应头处理:透传(去 hop-by-hop 头),Content-Length 与 chunked 均原样透传。

### 6.2 CONNECT(HTTPS)隧道
1. 读 `CONNECT host:port HTTP/1.1` → 按 host 做策略判定(域名/类别规则同样适用)。
2. 放行:连上游 host:port → 回 `200 Connection established` → 双向阻塞转发(select,读哪个写哪个),计数两向字节;任一侧 EOF/超时即双向关闭(先 shutdown 写)。
3. 拦截:记审计后直接关闭连接(浏览器呈现代理连接失败);不回 403 以免客户端混淆(文档说明)。
4. 隧道内内容不可见,不做内容过滤——域名判定在 CONNECT 明文阶段完成。

### 6.3 策略判定优先级(一次判定只出一个 Decision)
```
认证(角色) → 白名单模式?(非名单内 → 拦截,type=whitelist)
           → 黑名单命中?(域名规则/URL 规则/正则 → 拦截,type=blacklist,rule_id)
           → 类别命中?(角色禁类且 host/path 命中该类 → 拦截,type=category)
           → 请求特征正则命中?(→ 拦截,type=signature)
           → 放行(仍可能被响应侧特征检查拦截 → 拦截,type=resp_signature)
```
Decision 字段:`{action: allow|block, rule_type, rule_id, reason}`。大小写不敏感匹配 host;URL 关键词大小写不敏感;支持通配 `*.` 域名。

### 6.4 响应体特征检查(明文 HTTP)
仅当:响应 Content-Type 为 `text/*` 且存在 Content-Length 且 ≤ 2MB 时缓冲检查;命中任一响应特征正则 → 丢弃已缓冲响应、向客户端回 403 拦截页(连接关闭),记审计。chunked 或超限响应不做检查直接透传(局限写入报告)。

### 6.5 静态缓存(加分)
- 条件:GET + 明文 HTTP + 上游响应 `2xx` + Content-Type 在静态白名单(image/*, css, js, 常见字体) + 无 `Cache-Control: no-cache/no-store/private` + 无 `Set-Cookie` + 无 `Expires` 过期语义冲突(简化:有 Expires 但已过期则跳过缓存)。
- key:`sha1(method + url)`;磁盘 `cache/ab/cdef...` 两级目录;值存响应头子集 + body。
- 命中:回 200 + 缓存头 + body,上游不发请求;审计标 `cache_hit=1`,上下行字节如实计(上行 0)。
- LRU:内存维护 access 时间,超文件数上限(默认 500)或容量(默认 200MB)时按 LRU 删除。
- 校验:命中后仍满足 `cache_hit` 前提的条件函数复用。

### 6.6 认证
`Proxy-Authorization: Basic base64(user:pass)`。每连接首次请求校验通过后缓存该连接的用户/角色,后续请求不再弹认证。失败回 407(附 `Proxy-Authenticate: Basic realm="corp-proxy"`)。演示默认提供 admin/主管/员工三个预置账号。

## 7. 数据与配置模型

### 7.1 config.toml(示例)
```toml
[proxy]
listen_host = "0.0.0.0"
listen_port = 8080
max_connections = 64
recv_timeout = 60
idle_timeout = 60
auth_required = true

[audit]
db_path = "data/audit.db"

[policy]
reload_interval = 60
whitelist_mode = false
whitelist = ["corp-doc.com"]
blacklist_domains = ["*.blocked-site.example"]   # 全局合规红线:对所有角色生效,角色策略不可覆盖(见 7.2 语义)
blacklist_url_regex = []
blacklist_keywords = []
disabled_categories = []        # 全局默认;角色级优先
request_signatures = ["password\\s*=", "赌博"]
response_signatures = ["违规内容特征词"]

[cache]
enabled = true
dir = "cache"
max_files = 500
max_bytes = 209715200
static_types = ["image/*", "text/css", "application/javascript", "font/*"]

block_page = "block_page.html"
```

### 7.2 users.toml / roles.toml
```toml
# users.toml: plaintext 密码仅教学演示,生产需换哈希(写入报告局限)
[users.admin]
password = "admin123"
role = "admin"

[users.manager]
password = "mng123"
role = "manager"

[users.employee]
password = "emp123"
role = "employee"
```
```toml
# roles.toml: 角色策略。语义:
# - 黑名单(7.1 blacklist_*)为全局合规红线,对所有角色生效,角色策略不可覆盖
# - 某键在角色中缺失 → 回退全局配置(config.toml 同名项为回退默认)
# - 显式给出空数组/布尔值 → 以角色为准(如 admin/manager 用空数组绕过全局特征检查)
# 演示账号矩阵:employee 严管(game/shopping/gambling 全禁+全局特征),
#              manager 中管(仅禁 gambling,绕过特征),admin 全放行(仅受黑名单红线约束)
[roles.admin]
whitelist_mode = false
disabled_categories = []
request_signatures = []
response_signatures = []

[roles.manager]
whitelist_mode = false
disabled_categories = ["gambling"]
request_signatures = []
response_signatures = []

[roles.employee]
whitelist_mode = false
disabled_categories = ["game", "shopping", "gambling"]
# request_signatures / response_signatures 缺省 → 回退全局(config.toml 中的特征表)
```

### 7.3 SQLite 审计表
```sql
CREATE TABLE requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,            -- ISO8601 本地时间
  user TEXT, role TEXT,
  method TEXT, host TEXT, url_path TEXT, port INTEGER,
  decision TEXT,               -- allow|block_whitelist|block_blacklist|block_category|block_signature|block_resp_signature|auth_fail|error
  rule_id TEXT, reason TEXT,
  bytes_up INTEGER, bytes_down INTEGER,
  resp_status INTEGER, cache_hit INTEGER DEFAULT 0,
  duration_ms INTEGER
);
CREATE INDEX idx_ts ON requests(ts);
CREATE INDEX idx_user ON requests(user);
CREATE INDEX idx_host ON requests(host);
```

## 8. 并发、超时与可靠性

- acceptor 单线程 accept → 每连接 `threading.Thread(daemon=True)`;信号量(64)超限时新连接直接关闭并计日志(拒绝日志)。
- 所有 socket 设 `SO_RCVTIMEO`/`SO_SNDTIMEO`(60s 读、120s 写);keep-alive 空闲超时 60s。
- 异常边界:任何转发中途异常 → 尽力关闭两侧连接、当前请求记 `decision=error`,**不中断服务器**。
- 审计写入失败(磁盘满/锁)仅告警不阻断转发;每线程独立 sqlite 连接,WAL 模式。
- 隧道字节统计以转发循环计数为准;HTTP 计数含头与体(口径写入报告:字节数=代理与客户端间收发总字节)。

## 9. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 请求/响应头解析(含分块、缺行、超长行);Content-Length body;策略四层判定矩阵;认证编解码与角色映射;缓存命中与失效;LRU 淘汰 |
| 集成 | `demo_sites.py` 起模拟站 → `e2e_proxy.py` 用真实 socket/`urllib` 走代理断言:放行、黑名单 403、白名单模式、类别拦截、特征拦截、认证 407、缓存命中二次请求 |
| 演示 | hosts 映射域名(演示脚本检测/写入提示);浏览器 + curl 双客户端;视频脚本见 §10 |

离线演示站点设计(`demo_sites.py`,固定端口 18001-18004,`127.0.0.1` 监听):
- `corp-doc.com:18001` 内部文档站(白名单/普通放行目标;`/static/logo.png`、`/static/app.css` 静态资源用于缓存演示;`/login?password=…` 触发请求特征;`/internal/memo` 页面含"违规内容特征词"触发响应特征)
- `game-site.com:18002` 游戏站(类别 "game" 拦截演示)
- `shop.example:18003` 购物站(类别 "shopping";manager 可访、employee 被拦)
- `blocked-site.example:18004` 违规外站(全局黑名单红线演示,所有角色均被拦)
hosts 映射由 `tools/setup_hosts.ps1` / `tools/cleanup_hosts.ps1` 一键完成(需管理员);命令行演示可用 `curl --resolve <域名>:<端口>:127.0.0.1` 免改 hosts。

## 10. 演示场景清单(供视频脚本与答辩使用)

1. 启动代理 → employee 账号访问 corp-doc.com 正常(日志 allow)
2. 全局红线:任意角色访问 blocked-site.example → 403 黑名单拦截页(规则号、说明"企业红线")——展示黑名单
3. 类别差异:employee 访问 game-site.com / shop.example → 403(类别 game/shopping 被禁);manager 访问两者 → 200;manager 访问赌博类站点被拦(仅禁 gambling)
4. 特征拦截:employee 访问 corp-doc.com/login?password=xxx → 403(请求特征);employee 访问 /internal/memo → 403(响应特征,页面含"违规内容特征词");admin 访问同一页 → 200(admin 显式空特征表绕过)
5. admin.py 报表:按用户流量 TOP、拦截排行、按天历史查询
6. 缓存演示:二次请求 /static/logo.png 命中缓存(cache_hit=1、bytes_up=0)
7. 热加载:改 roles.toml 解除 employee 的 shopping 禁类 → 60s 内生效,employee 再访 shop.example 放行
8. (可选)真实外网:浏览器代理指向本机,访问 www.baidu.com 域名级规则演示

## 11. 分工与讲解映射(模块级,报告/PPT 素材骨架)

每个模块的讲解稿素材(做了什么/解决什么问题/关键代码/答辩要点)在实现阶段随模块交付,组员按分配模块自行整理个人讲稿。模块与文件对应见 §5 表;建议答辩人按"解析器→策略引擎→转发/隧道→认证→审计→缓存"顺序讲,每块 1-2 分钟。

## 12. 已知局限(写入报告"系统评估-不足与改进")

- HTTPS 内容不可见:仅域名级过滤(无 MITM)
- chunked/超大响应不做内容特征检查
- 明文密码存储(教学场景);无加密日志传输
- 单机演示规模,线程模型未面向千级并发
- Basic Auth 每次会话浏览器交互(演示用 curl/预填)

## 13. 里程碑(自批准日起,全天投入)

| 天 | 产出 | 验证口径 |
|---|---|---|
| D1 | 仓库+骨架+http_message+forward+黑名单,curl 跑通 | `curl -x 127.0.0.1:8080` 放行/黑名单 403 |
| D2 | policy 四层+类别词库+auth+roles | 单元测试矩阵通过 |
| D3 | audit+admin CLI+cache | 报表可用、缓存命中日志 |
| D4 | demo_sites+hosts+e2e+加固 | e2e 全绿 |
| D5 | 演示彩排+报告/PPT 素材骨架 | 按 §10 过一遍场景 |
