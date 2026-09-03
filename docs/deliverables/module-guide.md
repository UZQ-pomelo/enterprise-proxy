# 模块技术指南(个人报告素材 · 真实,可直接引用)

> 本文档逐模块给出**真实**的技术实现要点:职责、关键算法、文件/行号入口、
> 测试点。供小组写各阶段报告与个人报告时按需取用——每节的"负责该模块的成员"
> 留空由组长按组员数分配,切勿照抄虚构分工。凡标注 `-> 行号` 均可直接
> 在源码中核对。整体数据流见文末图。

---

## 模块 A · 报文层 http_message.py(协议基础)

**职责**:HTTP 报文(请求/响应)的解析与构造,是转发/隧道/认证共同的底层。

- 头解析:`read_request_head` 按 `\r\n\r\n` 分界读请求行+头,上限 64KB;
  行内以首个 `:` 切分(值允许含冒号);大小写不敏感存取(`Headers` 包装)。
- 定界体长度:`content_length` 处理重复/非法 Content-Length;分块传输按帧
  结构逐块读(`read_chunked`)。
- 构造侧:`make_simple/make_407/make_407_deny` 生成代理自身响应;
  `build_upstream_request` 把客户端请求重写为上游可接受形式:
  去代理认证头(Proxy-Authorization 绝不转发)、剥离 hop-by-hop 头
  (Connection/Keep-Alive/Proxy-*)。
- 计数:`BufferedSocketReader/ByteCounter` 分别统计读/写字节——审计的
  bytes_up/bytes_down 全部由这两个计数器差值得出(统一口径:客户端↔代理全量字节)。
- 字节缓冲的坑:读满超量字节时保留在 `_rest`,下一轮解析先消费它。

**要点提炼**(报告"关键算法"可用):一个 reader 同时承担"解析"与"精确计数";
hop-by-hop 头剥离清单 + Connection 令牌一致性(转发时若上游响应带
`Connection: close` 则同步置 close_after)。

**测试**:tests/test_http_message.py —— 请求行/头/分块/大小写/超长头/407 构造
与重写规则。

---

## 模块 B · 配置与热加载 config.py

**职责**:TOML 配置装载(标准库 `tomllib`)+ 角色缺省语义 + 运行中热加载探测。

- 三类文件:`config.toml`(server/proxy/audit/cache/policy 五表)、`users.toml`、
  `roles.toml`;`load_config_dir` 任一文件变更即整体重载。
- 角色缺省语义:`RolePolicy` 用 `None` 区分"该键缺失(回退全局)"与"显式给出
  (角色胜出)"。
- 热加载探测:`is_changed()` 比较每文件 `(mtime_ns, size)`;
  **size 必不可少**——NTFS 时间戳粒度约 100ns,同毫秒内两次写入可能
  mtime 相同(见 tests/test_config.py 的回写测试)。
- 相对路径基准:`db_path/cache.dir/block_page` 均相对配置文件所在目录解析,
  便于整目录拷贝部署。

**测试**:test_config.py —— 缺省/合并/损坏文件报错/目录相对路径/热加载命中与
miss。

---

## 模块 C · 类别词库 categories.py

**职责**:把 `(host, url_path)` 打上类别标签,供"按站点类型禁用"使用。

- 词库分两类:域名关键词(`game`→site.com 命中)与路径关键词(`/game/`、
  `/casino/`);域名后缀精确匹配(shop.example 不误伤 example.shop 之类)。
- `tag()` 返回集合;策略侧只取与角色禁用集之交非空即拦。
- 词库覆盖与演示站点一一对应:corp-doc(无标签)/game/shopping/gambling/
  social/streaming 等,扩展只需加词条(测试 test_policy 里 `tag` 断言)。

---

## 模块 D · 策略引擎 policy.py(核心)

**职责**:四层规则判定 + 特征预编译 + 角色合并;一次判定只出一个结果。

判定优先级与规则表(报告可画决策树):

```
白名单模式(host ∈ whitelist?否→拦 whitelist)
  → 黑名单域名/关键词/正则(命中→拦 blacklist,全局红线,任何角色)
  → 类别(角色 disabled_categories ∩ tag() 非空→拦 category)
  → 请求特征(role 的签名表扫描 url+headers→拦 signature)
  → 放行 allow
```

- **黑名单红线不可覆盖**:优先级最高层之下、且角色配置无任何开关可豁免——
  演示中 admin 访问 blocked-site.example 照样 403。
- **角色合并实现**:`_merged()`(缺失键回退)+ `_sigs()`(角色显式空表=绕过,
  未定义=回退全局);`_whitelist_mode()` 同语义。admin 的
  `whitelist_mode=false`/`request_signatures=[]` 因此等价"全放行"。
- **响应内容检查** `check_response`:仅 text/*、非空正文、UTF-8 容错解码后
  跑角色响应签名表;返回 block 决策由 forward 在**转发给客户端之前**落地
  (拦截内容不出网)。
- 特征在装载时预编译 `re.compile(..., re.I)`,热加载后整表重建。

**测试**:test_policy.py —— 优先级顺序/角色回退/显式空表/黑名单不可豁免/
热加载切换 whitelist_mode/响应特征仅 text。

---

## 模块 E · 认证 auth.py

**职责**:HTTP 代理认证(Proxy-Authorization: Basic)。

- 只认代理认证头(与源站 Authorization 无关);base64 严格解码
  (`validate=True`,拒绝非法字符)后按第一个 `:` 切分用户名与口令,查用户表
  比对;缺头/格式错/无此用户/口令错一律返回 None → 上层发 407 且
  **不触达任何策略与上游**。课程演示为明文口令比对,生产应改为散列存储
  (见 README 安全边界)。

**测试**:test_auth_audit.py 头部六例(口令错/未知用户/无头/非 Basic/坏 base64)。

---

## 模块 F · 审计 audit.py(题目"日志记录")

**职责**:SQLite 持久化 + 报表查询;WAL 模式、单锁、写失败仅告警不阻断转发。

- 表结构:每次访问一行(ts/user/role/method/host/url_path/port/decision/
  rule_id/reason/bytes_up/bytes_down/resp_status/cache_hit/duration_ms);
  `add()` 只取固定列集合,容忍调用方附带内部键(字节基线 _r0/_w0/_t0 在
  落库前由 runtime 弹出,不入库)。
- 查询面:`query`(user/decision/day 过滤,id 倒序)、`decision_counts`(GROUP BY
  决策,支撑报表)、`top(user|host)`(SUM(bytes_down) 排行)、`summary`
  (COUNT/SUM 聚合);日期过滤用 `ts LIKE '2026-09-03%'`(当天整天)。
- Windows 文件锁教训:审计连接常驻进程;**必须先排空 worker 线程再
  close()**,否则 sqlite 文件被活动连接锁住、TemporaryDirectory 清理报
  PermissionError(proxy_server.stop 的 drain 设计)。

**测试**:test_auth_audit.py 后半 —— 写入/过滤/计数/排行/汇总/缺键容忍。

---

## 模块 G · 运行期装配 runtime.py

**职责**:每请求审计记录的字节/耗时生命周期,避免模块循环依赖的容器层。

- `new_rec` 在任何读取**之前**建记录并记三基线:`_r0=reader.bytes_read`、
  `_w0=writer.bytes`、`_t0=monotonic`——保证 bytes_up 涵盖请求头本身;
- `finish_rec` 用 `bytes - 基线` 差值回填;`user/role` 在此刻从 ctx 刷新
  (认证发生在 new_rec 之后,M9 曾因此漏记用户——修正见 git 提交 a31338d);
  tunnel 的隧道负载通过 `_extra_up/_extra_down` 两个私有键并入;
- 决策空值 → `error`(协议异常/上游不可达等),resp_status 由转发层保留。

---

## 模块 H · HTTP 转发 forward.py

**职责**:明文 HTTP 请求的中转主干:解析目标 → 策略判定 → 缓存 → 连上游 →
改写重发 → 响应按三分支回传。

- 上游 URL 解析:absolute-URI(host[:port]/path);`Connection: close`
  每请求一上游连接,简单且可审计。
- 拦截(403)与 502/400 一律 `ctx.close_after`——响应无定界体,连接必须关,
  否则客户端会挂起等下一个响应。
- 客户端请求体中继:定长按 CL、分块按帧中继(Chunked 编码原样转发)。
- 响应四分支(报告可画状态图):
  1. no_body(HEAD/204/304)→ 只回头不回体;
  2. 分块 → 头经 hop-by-hop 剥离后**帧级原样泵送**(保留 Transfer-Encoding);
  3. 无 CL 又非分块 → 读到上游关闭再回(close_after);
  4. 定长可缓冲 → 全文缓冲后依次做:响应内容特征检查 →(通过则)静态缓存
     put → 转发。可缓冲判定=CL 且 ≤2MB(text/* 或缓存类型 4MB),超限走流式。
- `started` 标志:已写出任何响应字节前遇错可补 502/400;已开始则静默断连,
  决策记 error。

**测试**:test_forward_tunnel.py —— 缓存命中转 304 语义/HTTP 101 升级直通/
正文可缓冲与流式/HEAD/CHUNKED/上游断开/`X-Cache` 头等 30+ 断言(含假上游
FakeUpstream 记录收到原始字节,验证改写正确性)。

---

## 模块 I · CONNECT 隧道 tunnel.py

**职责**:HTTPS 等加密流量的代理隧道(CONNECT)。

- 解析 `CONNECT host:port`,策略判定先行——被拦**静默断开**(与浏览器直连
  失败一致的体验),放行回 `HTTP/1.1 200 Connection established`;
- 双泵线程:`recv → sendall`,各自在 rec 私有键上累加隧道负载字节;
- 超时取自 `server.recv_timeout`(默认 60s):任一方向静默超时即断双泵并
  关闭连接;隧道内不解析内容(TLS),但 CONNECT 请求行与握手字节照常计入
  审计,故审计行中 HTTPS 会话 method=CONNECT。

---

## 模块 J · 缓存 cache.py(题目额外任务)

**职责**:高频静态资源的简易本地缓存。

- 键 = sha1(完整 URL)hex;两级目录 `dir/前2位/<hash>`;meta(.meta.json:
  url/status/headers/expires)与 body 分文件;
- 可缓存判定:GET、Content-Type ∈ static_types(image/* 等)、2xx、
  无 Set-Cookie、Cache-Control 无 no-store/no-cache/private;
- 过期:max-age(秒)或 Expires 解析成 ISO 落 meta,读取时超期即作废删除
  (缓存保活性优先于陈旧提供);
- 淘汰:内存 LRU(单线程访问,无锁),超 max_files / max_bytes 按最久未用
  逐出;`hits/files/bytes` 统计供管理面;
- 命中路径:转发层提前命中则**不回源**——以缓存体实际字节数重写
  Content-Length 并加 `X-Cache: HIT` 本地应答(meta 中存的是上游响应头,
  其中 Content-Length 是当初的定界信息,不能当作当前实体长度使用,
  故命中时必须重设),审计行 cache_hit=1;缓存写入只发生在整响应体
  成功缓冲并放行之后(404/304 等无体响应自然不进缓存);仅新增成功条目
  才触发 LRU 淘汰;
- 离线扫描 `scan_stats` 供 admin cache-stats(不依赖进程内状态)。

**测试**:test_cache.py —— 放行/禁忌/过期/淘汰/命中 304 语义等。

---

## 模块 K · 服务器装配(server / handler / 入口)

- `client_handler.handle_client`:每连接一个守护线程上的请求循环——**每请求**
  开始前 `engine.reload_if_changed()`(热加载生效点),读头,认证,按
  method 分派,收尾 finish_rec + audit.add;空闲超时/对端关闭即退。
- `proxy_server.ProxyServer`:独立 accept 守护线程;`BoundedSemaphore` 把并发
  连接压到 max_connections;worker 登记表 + `stop(drain)` 先关 listener、
  轮询等 worker 排空(≤1s),**最后**才关审计库(Windows sqlite 文件锁)。
- `run_proxy.py`:argparse(--config/--port)+ 启动横幅 + Ctrl+C 优雅停机;
  演示默认 127.0.0.1:18080。

---

## 模块 L · 演示与验收资产(加分面)

- `demo_sites.py`:5 站点 × 固定端口 18001–18005 纯本地 HTTP
  (ThreadingTCPServer),内容与演示矩阵一一呼应:corp-doc(白名单候选)/
  game(员工禁用类别)/shop(同上)/blocked-site(全局黑名单)/content-check
  (域名干净但正文含全局响应特征词,专供"内容签名就地拦截"演示),logo.png
  带 `Cache-Control: max-age=3600` 用于缓存演示;
- `tools/setup_hosts.ps1` / `cleanup_hosts.ps1`:管理员 PowerShell 写 hosts
  演示映射并只清理自己加的行(UTF-8 BOM + CRLF 规避 PowerShell 5.1 把无 BOM
  UTF-8 当 GBK 的解析坑);
- `e2e_proxy.py`:10 步端到端验收——临时配置目录+三角色真实请求逐条断言
  (407→黑名单红线→employee 类别→manager 仅禁赌博→admin 全放行→请求特征
  角色回退→响应内容特征→缓存往返 X-Cache→**运行中热加载白名单切换**→
  审计库统计/排行/缓存命中列);演示域名经 `socket.getaddrinfo` 补丁解析到
  127.0.0.1(等价 hosts,免管理员);退出码 0/1 可入 CI;
- `admin.py`:报表/排行/拦截明细/缓存统计五子命令(见 README 四)。

---

## 数据流总览(报告架构图可直接画)

```
浏览器/curl ──(代理配置)──▶ ProxyServer(accept)
   │ 每连接一线程
   ▼
client_handler: 热加载 → 读请求头 → 认证(失败=407)
   ▼ ctx.user/role 已定
forward/tunnel: 策略判定(白名单→黑名单→类别→请求特征)
   │ block ──▶ 403 拦截页(HTML 读自 block_page.html,原因/规则号入页)
   │ allow ──▶ 缓存早命中?▶ 是→X-Cache HIT 直发
   │           否→连上游转发;响应:特征检查 → 缓存 put → 回客户端
   ▼
finish_rec(字节=计数器差值+隧道负载)──▶ audit.add ──▶ SQLite(requests)
   ▲                                                      │
   └── 运维: admin.py 报表/排行/拦截明细 ◀────────────────┘
```

**里程碑与测试演进**(git log 可查,报告"实施阶段"素材):
M1 报文层(21 测试)→ M2 配置(7)→ M3 规则引擎(12)→ M4 认证审计(11)→
M5 缓存(12)→ M6 转发/隧道/装配 → M7 服务器+curl 冒烟 → M8 演示站点 →
M9 端到端 10/10 → M10 管理 CLI。每里程碑独立 commit、全绿后推进。
