# 演示剧本(演示视频 / 汇报现场通用)

> 每条步骤含:操作命令(照抄可跑)、预期画面、讲解要点(真实,对应源码行)。
> 全程单机离线:代理 127.0.0.1:18080、演示站点 127.0.0.1:18001–18004,
> 域名映射走 hosts(工具已备)或 e2e 的 getaddrinfo 等价方案。
> 录屏建议 1280×720 窗口 + 等宽字体;每步停顿 1–2 秒让画面跟上讲解。

## 准备(约 2 分钟)

```bat
powershell -ExecutionPolicy Bypass -File tools\setup_hosts.ps1   :: 管理员,一次性
python demo_sites.py                                             :: 终端 A:4 个演示站点
python run_proxy.py --port 18080                                 :: 终端 B:代理(读 config.toml)
python admin.py report                                           :: 终端 C:空库报表(请求总数 0)
```

讲解要点:三个进程 = 客户端要访问的"互联网"(演示站点)、企业出口代理、运维面。
当前审计库为空,先让报表在场。

## 演示 1 · 客户端必须配代理;不配代理上不了网(约 1 分钟)

```bat
curl http://game-site.com:18002/            :: 不设代理 —— 直连 127.0.0.1:18002?能通!
```
> ⚠ 本机演示站点与代理同在 127.0.0.1,无法天然体现"必须经代理"。
> 正确讲法:演示站点是"内网之外"的模拟源站,企业员工机只配置了代理,
> 由代理代访。此处改为讲**认证即第一道闸**:

```bat
curl -x http://127.0.0.1:18080 http://corp-doc.com:18001/ -i        :: 无凭据
:: 预期:HTTP/1.1 407 Proxy Authentication Required(第 0 步验收)
```

讲解要点:407 是代理专用状态码;审计落一条 auth_fail(看报表可现场切终端 C
`python admin.py report`)。

## 演示 2 · 黑名单红线:管理员也拦(约 1 分钟)

```bat
curl -x http://127.0.0.1:18080 -U admin:admin123 http://blocked-site.example:18004/ -i
:: 预期:HTTP/1.1 403 Forbidden + 品牌化拦截页(含命中规则与原因)
curl -x http://127.0.0.1:18080 -U employee:emp123 http://blocked-site.example:18004/ -i
:: 预期:同上 403
```

讲解要点:全局黑名单是**不可覆盖红线**(policy.py:96),admin 显式配置只豁免
白名单/类别/特征,黑名单不在其列——防"权限最高的人违规"。

## 演示 3 · 角色权限矩阵(约 2 分钟)

```bat
curl -x http://127.0.0.1:18080 -U employee:emp123 http://game-site.com:18002/ -i   :: 403
curl -x http://127.0.0.1:18080 -U manager:mng123  http://game-site.com:18002/ -i   :: 200
curl -x http://127.0.0.1:18080 -U employee:emp123 http://shop.example:18003/  -i   :: 403
curl -x http://127.0.0.1:18080 -U manager:mng123  http://shop.example:18003/  -i   :: 200
curl -x http://127.0.0.1:18080 -U admin:admin123 http://game-site.com:18002/  -i   :: 200
curl -x http://127.0.0.1:18080 -U admin:admin123 http://corp-doc.com:18001/   -i   :: 200
```

讲解要点:同一站点对不同角色结果不同——权限挂在**角色**而非用户;
employee 禁游戏+购物+赌博,manager 仅禁赌博(roles.toml),admin 全放行。

## 演示 4 · 类别拦截的"路径级"精度(约 30 秒)

```bat
curl -x http://127.0.0.1:18080 -U manager:mng123 http://game-site.com:18002/ -i        :: 200
curl -x http://127.0.0.1:18080 -U employee:emp123 http://game-site.com:18002/game/lobby -i :: 403
```

讲解要点:类别判定同时看**域名词与路径词**(categories.tag):游戏站首页对
manager 开放,但只要 URL 出现 /game/ 词条即命中"游戏内容"标签被拦。

## 演示 5 · 内容签名:请求特征与响应特征(约 2 分钟,核心加分点)

请求侧——"员工试图把口令带在 URL 上外泄"的检测:

```bat
curl -x http://127.0.0.1:18080 -U employee:emp123 "http://corp-doc.com:18001/login?password=1" -i   :: 403
curl -x http://127.0.0.1:18080 -U admin:admin123 "http://corp-doc.com:18001/login?password=1" -i   :: 200
```

响应侧——站点正文含违规特征词时,代理**在把响应交给员工前**就地拦截:

```bat
curl -x http://127.0.0.1:18080 -U employee:emp123 http://content-check.example:18005/ -i   :: 403
curl -x http://127.0.0.1:18080 -U manager:mng123   http://content-check.example:18005/ -i   :: 200,收到原文
curl -x http://127.0.0.1:18080 -U admin:admin123   http://content-check.example:18005/ -i   :: 200,收到原文
:: 对照:直连该站(不经代理)内容可达——说明拦截发生在代理的内容检查环节
curl http://content-check.example:18005/  | findstr 违规    :: 有正文
```

讲解要点:content-check 域名干净(不进黑名单/类别),但演示站正文含全局
`response_signatures` 词条"违规内容特征词";响应需整体缓冲(定长+text/*+≤2MB)
才可检查——命中即 403,**违规字节不出代理**。同页三角色三种结果正是合并
语义的现场版:employee 缺键→回退全局被拦;manager/admin 显式空表→绕过收原文。
`e2e_proxy.py` 的"响应内容特征"一步即此场景的自动化版。

## 演示 6 · 静态资源缓存(额外任务,约 1 分钟)

```bat
curl -x http://127.0.0.1:18080 -U employee:emp123 -D- http://corp-doc.com:18001/static/logo.png -o logo1.png
curl -x http://127.0.0.1:18080 -U employee:emp123 -D- http://corp-doc.com:18001/static/logo.png -o logo2.png
:: 预期:第一次无 X-Cache 头;第二次 X-Cache: HIT;两文件字节相同(fc logo1.png logo2.png)
```

讲解要点:命中即回,不触达上游;演示站 logo 带 `Cache-Control: max-age=3600`
才有资格入缓存(禁忌判定见 cache.py:_forbidden);`admin.py cache-stats`
看条目数。

## 演示 7 · 热加载:运行中切换白名单模式(约 1 分钟,加分点)

```bat
:: 打开 config.toml,把 [policy] whitelist_mode 改成 true 保存(别动其他)
:: 60 秒内(探测周期)依次执行:
curl -x http://127.0.0.1:18080 -U manager:mng123 http://game-site.com:18002/ -i   :: 403(白名单外)
curl -x http://127.0.0.1:18080 -U manager:mng123 http://corp-doc.com:18001/  -i   :: 200(白名单内)
curl -x http://127.0.0.1:18080 -U admin:admin123 http://game-site.com:18002/ -i   :: 200(admin 显式豁免)
:: 改回 false 保存 → manager 恢复 200(讲解:配置损坏会保留旧规则并告警)
```

讲解要点:热加载 = 每请求前 `mtime+size` 探测(config.py:is_changed),不必重启
服务;角色合并语义在此最直观——manager 未写 whitelist_mode 所以跟随全局,
admin 写了 false 所以豁免。

## 演示 8 · 审计与运维报表收尾(约 2 分钟,回归题目"日志记录")

```bat
python admin.py report          :: 总量/决策分布(可现场对上前几步的 403/407/200)
python admin.py top-users       :: 下行流量排行
python admin.py blocked         :: 拦截明细(规则号+原因)
python admin.py report --day 2026-09-03
```

讲解要点:SQLite 落盘,每请求一行,含命中规则与原因;决策分类
allow/block_*/auth_fail/error 让管理员一眼看出"拦了什么、为什么拦"。
浏览器直观版(可选):装 SwitchyOmega 之类指向 127.0.0.1:18080 访问
corp-doc.com:18001 看页面,再开 game-site.com:18002 看 403 拦截页。

## 结束语

一句话收尾:"四层规则 + 角色权限的访问控制,全程审计可追溯,静态缓存做性能
兜底——而这一切跑在 8 个模块、104 项单元测试与 10 步端到端验收之上,
`python e2e_proxy.py` 一条命令可复现全部断言。"(如时间紧张,演示 1–8 用
`e2e_proxy.py` 单命令演示 10/10 全过,再挑 2–3 步 curl 真人操作即可)
