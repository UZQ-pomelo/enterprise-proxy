# 企业代理服务器(流量审计 + 访问控制)

《网络工程项目实施》选题四的完整实现:**HTTP 正向代理**。面向企业内网场景,
客户端必须经由本代理上网;代理逐请求解析、按自定义规则精准拦截违规流量,
把每次访问写入 SQLite 审计库,并按角色分配权限。

- **语言/依赖**:Python 3.11+ · **零第三方依赖**(仅标准库)
- **运行形态**:单机离线演示(自带演示站点与域名映射工具,不依赖外网)
- **实现深度**:主线六项要求全部覆盖 + 额外任务(静态资源缓存)+ 自选加分
- **设计文档**:`docs/superpowers/specs/2026-09-03-enterprise-proxy-design.md`

## 一、功能总览(与题目要求逐条对应)

| 题目要求 | 实现 | 验证位置 |
|---|---|---|
| 代理服务器解析用户网络请求 | 显式正向代理:absolute-URI 明文转发 + CONNECT 隧道;线程每连接 | `forward.py` `tunnel.py` |
| 黑白名单精准拦截 | 全局黑名单为不可覆盖的红线(域名/关键词/URL 正则),管理员也拦 | `policy.py:86` |
| 特定网站类型拦截 | 域名+路径关键词分类词库(game/shopping/gambling/...),按角色禁用 | `categories.py` |
| 特定特征数据拦截 | 正则特征:**请求侧**(URL/请求头,如 `password\s*=`)与**响应侧**(响应正文违规内容) | `policy.py:122,129` |
| 日志记录 | SQLite 审计:每次访问一行(用户/角色/目标/决策/命中规则/上下行字节/耗时/缓存命中),支持日期过滤查询与排行 | `audit.py` |
| 根据角色分配权限 | 三角色(admin/manager/employee)+ 角色合并语义 | `policy.py:64` |
| **额外任务**:高频静态资源简易本地缓存 | GET 静态类型 sha1 两级目录缓存:meta+body、max-age 过期、LRU 淘汰、X-Cache 头、磁盘配额 | `cache.py` |
| 创造性加分 | ① 白名单模式(企业只允许登记域名)+ 运行中**热加载**配置不重启;② 运维管理 CLI(报表/排行/拦截明细);③ 离线演示站点矩阵 + hosts 一键工具;④ 拦截页品牌化提示原因 | 见各模块 |

## 二、快速开始(单机离线演示)

> 完整分步操作手册(含预期输出与 FAQ):`docs/deliverables/run-guide.md`。

```bat
:: 1. 启动代理(默认监听 127.0.0.1:18080,读取 config.toml)
python run_proxy.py

:: 2. 另开终端,先以管理员运行一次 hosts 映射(把演示域名指向 127.0.0.1)
powershell -ExecutionPolicy Bypass -File tools\setup_hosts.ps1

:: 3. 启动 5 个离线演示站点(18001–18005)
python demo_sites.py
```

浏览器/curl 通过代理访问演示域名即走通全流程:

```bat
:: 认证失败 → 407
curl -x http://127.0.0.1:18080 http://corp-doc.com:18001/ -i
:: 黑名单红线(任何角色,含 admin)→ 403
curl -x http://127.0.0.1:18080 -U employee:emp123 http://blocked-site.example:18004/ -i
:: 角色权限:employee 禁游戏站;manager 仅禁赌博;admin 全放行
curl -x http://127.0.0.1:18080 -U employee:emp123 http://game-site.com:18002/ -i   :: 403
curl -x http://127.0.0.1:18080 -U manager:mng123  http://game-site.com:18002/ -i   :: 200
curl -x http://127.0.0.1:18080 -U admin:admin123 http://game-site.com:18002/ -i   :: 200
:: 请求特征拦截(密码明文外泄场景)
curl -x http://127.0.0.1:18080 -U employee:emp123 "http://corp-doc.com:18001/login?password=1" -i   :: 403
:: 响应内容特征:正文含违规特征词 → 转发前就地拦截(employee),admin 显式绕过
curl -x http://127.0.0.1:18080 -U employee:emp123 http://content-check.example:18005/ -i  :: 403
curl -x http://127.0.0.1:18080 -U admin:admin123   http://content-check.example:18005/ -i  :: 200
```

清理演示映射:`powershell -ExecutionPolicy Bypass -File tools\cleanup_hosts.ps1`

> 演示账号密码见 `config.toml`(`[users]`,employee: emp123 / manager: mng123 / admin: admin123,
> 正式使用请修改)。浏览器手动代理设置:HTTP 代理 `127.0.0.1`、端口 `18080`,勾选"为 HTTPS 使用同一代理"。

## 三、自动化验收(一条命令跑完全部演示矩阵)

```bat
python e2e_proxy.py      :: 端到端 10 步:407/黑名单/类别/角色矩阵/请求与响应特征/缓存往返/热加载/审计落库
python -m unittest discover -s tests -p "test_*.py"   :: 单元测试 104 项
```

端到端脚本在临时目录装配完整规则与三角色,真实起服务、以真实 HTTP 逐条断言
(演示域名经 `getaddrinfo` 映射到 127.0.0.1,免管理员权限即可运行)。

## 四、运维管理 CLI

```bat
python admin.py report            :: 审计概览:总量/决策分布(默认库 data\audit.db)
python admin.py top-users         :: 按下行流量给用户排行
python admin.py top-hosts         :: 目标主机排行
python admin.py blocked           :: 最近被拦截明细(命中规则+原因)
python admin.py cache-stats       :: 缓存目录统计
:: 均支持 --day 2026-09-03 按日过滤;如:
python admin.py report --day 2026-09-03
```

## 五、规则与角色语义(设计要点)

判定优先级(一次判定只出一个结果,**黑名单为全局红线**):

```
白名单模式 → 黑名单(域名/关键词/正则) → 站点类别(角色禁用) → 请求特征 → 放行
响应侧单独检查:仅 text/* 且 ≤2MB 的响应体做内容特征检查(拦截后不转发给客户端)
```

角色合并语义:角色表**缺失**某键 → 回退全局策略;显式给出(空数组或 false)→
以角色为准。故 admin/manager 显式 `request_signatures=[]`、`response_signatures=[]`
即"绕过全局特征检查",employee 不写该键则回退跟随全局(响应特征演示即依此:
content-check 站正文命中全局词条,employee 403;admin/manager 因显式空表收到
原文 200)。

热加载:每请求开始时以 `mtime+size` 探测配置目录,变更即重载全部规则与账号,
运行中改 `whitelist_mode` 立即可见,配置损坏时保留旧规则并告警。

审计口径:每条记录含 决策(allow/block_* /auth_fail/error)、命中规则与原因、
`bytes_up/bytes_down`(客户端↔代理全量字节,隧道场景含 CONNECT 握手与隧道负载)、
`cache_hit`;SQLite WAL 模式,写入失败仅告警不阻断转发。

## 六、目录结构

```
run_proxy.py        启动入口(argparse/信号/横幅)
proxy_server.py     accept 循环 + 线程池信号量限流 + 优雅停机(先排空 worker 再关库)
client_handler.py   每连接请求循环:热加载 → 解析 → 认证 → 分派 → 审计落库
forward.py          HTTP 明文转发(绝对 URI 解析、分块/定长/读到关闭三分支、缓存介入)
tunnel.py           CONNECT 隧道(双线程泵、字节计数、超时断开)
policy.py           四层规则引擎 + 角色合并 + 特征预编译
categories.py       域名/路径 → 类别标签词库
auth.py             Proxy-Authorization Basic 认证
audit.py            SQLite 审计(写入/查询/排行/汇总)
cache.py            静态资源磁盘缓存(sha1 两级目录、过期、LRU、扫描统计)
config.py           TOML 装载 + 角色缺省语义 + 热加载探测
http_message.py     报文解析/构造/上游请求重写(协议层,无任何 I/O 语义依赖)
runtime.py          运行期装配与每条审计记录的字节/耗时生命周期
admin.py            运维管理 CLI
demo_sites.py       5 站点离线演示矩阵(18001–18005)
e2e_proxy.py        端到端自动验收
config.toml*        根配置 + 用户/角色表    tools/    hosts 映射工具(管理员)
tests/              104 项单元测试            docs/     设计文档与交付素材
```

## 七、平台说明与安全边界

- Windows 控制台默认 GBK:所有入口已 `reconfigure(utf-8)`;PowerShell 脚本用
  UTF-8 BOM 保存。
- 本项目为**课程演示与内网学习**用途:Basic 认证走代理连接明文;CONNECT 隧道
  因 TLS 加密无法做响应内容特征(明文 HTTP 请求仍全量解析审计)。生产环境应
  叠加 TLS(如 mitmproxy 方式)、密码散列存储与账户锁定,并集中收审计日志。
- 已知边界:每连接一线程(教学展示清晰,量级参考 e2e/演示场景);缓存仅覆盖
  2xx 无 Cookie 无 no-store 的静态类型响应,超配额按最久未用淘汰。
