# 运行操作手册(组员照此即可跑通)

> 本文是「怎么把系统跑起来」的完整流程:两个常驻进程 + 一个客户端角色,
> 单机离线完成全部演示。演示/汇报的话术与节奏另见 `demo-script.md`;
> 每个模块的技术细节见 `module-guide.md`。

## 0. 结构:整个系统只有三个角色

```
终端A: python demo_sites.py     ← 扮演"互联网":5 个演示站点(18001–18005)
终端B: python run_proxy.py      ← 企业代理服务器:认证+访问控制+审计+缓存(18080)
终端C: curl / 浏览器             ← 扮演"员工电脑",必须显式配代理才能"上网"
```

代理是**唯一出口**:员工机不配代理 → 上不了"网";配了代理 → 每步都被
审计/拦截,这是题目"客户端必须配置该代理服务器才能上网"的落地形态。

## 1. 前置检查(一次性)

```bat
python --version        :: 要求 Python 3.11+(用到标准库 tomllib)
cd C:\program1\sc       :: 项目根目录(config.toml 所在处)
```

Windows 自带 curl;PowerShell/cmd 均可执行下列命令。

## 2. 域名映射(仅需一次;供浏览器与域名 curl 演示)

五个演示域名实际都解析到 127.0.0.1,靠 hosts 建立映射。
**右键 PowerShell → 以管理员身份运行**:

```powershell
powershell -ExecutionPolicy Bypass -File C:\program1\sc\tools\setup_hosts.ps1
```

预期输出:5 行"添加: 127.0.0.1 xxx"+"DNS 缓存已刷新"。
不想要管理员权限?可跳过本步——全部 curl 验证与 e2e 不依赖域名。
清理时(演示结束后)用 `tools\cleanup_hosts.ps1`。

## 3. 终端 A:启动"演示互联网"

```bat
python demo_sites.py
```

预期:打印 5 行站点清单——corp-doc.com:18001(内部文档)、game-site.com:18002(游戏)、
shop.example:18003(商城)、blocked-site.example:18004(不良站)、
content-check.example:18005(内容检查站)。此窗口保持运行,勿关。

## 4. 终端 B:启动企业代理

```bat
python run_proxy.py
```

预期横幅(关键三行):

```
  监听       http://127.0.0.1:18080
  认证      开启(Basic)
  静态缓存  开启 → cache
```

- 想换端口:`python run_proxy.py --port 8080`,后续所有 `-x` 地址同步换;
- 想用另一套配置:`python run_proxy.py --config <目录>`;
- Ctrl+C 退出;退出前会先排空在线连接再关审计库(防 SQLite 文件锁)。

## 5. 终端 C:验收矩阵(核心演示,一条一条打)

账号:employee / emp123 · manager / mng123 · admin / admin123
(改自 `config.toml` `[users]`;角色策略见 `roles.toml`)

```bat
:: 5.1 认证第一道闸:不带账号 → 407(代理专用状态码)
curl -x http://127.0.0.1:18080 http://corp-doc.com:18001/ -i
::    首行预期: HTTP/1.1 407 Proxy Authentication Required

:: 5.2 黑名单红线:admin 也拦(全局黑名单不可覆盖)→ 403 + 拦截页
curl -x http://127.0.0.1:18080 -U admin:admin123 http://blocked-site.example:18004/ -i
curl -x http://127.0.0.1:18080 -U employee:emp123 http://blocked-site.example:18004/ -i

:: 5.3 角色矩阵:同一站点,三个角色三种结果
curl -x http://127.0.0.1:18080 -U employee:emp123 http://game-site.com:18002/ -i   :: 403 员工禁游戏
curl -x http://127.0.0.1:18080 -U manager:mng123  http://game-site.com:18002/ -i   :: 200 经理仅禁赌博
curl -x http://127.0.0.1:18080 -U employee:emp123 http://shop.example:18003/  -i   :: 403 员工禁购物
curl -x http://127.0.0.1:18080 -U manager:mng123  http://shop.example:18003/  -i   :: 200
curl -x http://127.0.0.1:18080 -U admin:admin123 http://game-site.com:18002/  -i   :: 200 管理员全放行
curl -x http://127.0.0.1:18080 -U admin:admin123 http://corp-doc.com:18001/   -i   :: 200

:: 5.4 路径级类别:游戏站首页对 manager 开放,含 /game/ 词条的路径被拦
curl -x http://127.0.0.1:18080 -U manager:mng123 http://game-site.com:18002/ -i          :: 200
curl -x http://127.0.0.1:18080 -U employee:emp123 http://game-site.com:18002/game/lobby -i  :: 403

:: 5.5 请求特征:URL 带 password= 视为口令外泄(employee 回退全局特征表被拦)
curl -x http://127.0.0.1:18080 -U employee:emp123 "http://corp-doc.com:18001/login?password=1" -i  :: 403

:: 5.6 响应内容特征:页面正文含违规词,代理在交给员工前就地拦截
curl -x http://127.0.0.1:18080 -U employee:emp123 http://content-check.example:18005/ -i  :: 403
curl -x http://127.0.0.1:18080 -U manager:mng123   http://content-check.example:18005/ -i  :: 200 显式空表绕过
curl -x http://127.0.0.1:18080 -U admin:admin123   http://content-check.example:18005/ -i  :: 200

:: 5.7 静态缓存:同一资源第二次 → X-Cache: HIT
curl -x http://127.0.0.1:18080 -U employee:emp123 -D- -o NUL http://corp-doc.com:18001/static/logo.png
curl -x http://127.0.0.1:18080 -U employee:emp123 -D- -o NUL http://corp-doc.com:18001/static/logo.png
::    第二次响应头出现: X-Cache: HIT
```

> 技巧:把 5.1–5.7 存成 `demo.bat` 双击即可整段重放;想看拦截页全文,去掉
> `-i` 前的输出重定向,或加 `-o page.html` 后用浏览器打开。

## 6. 浏览器演示(录屏/汇报画面)

1. 打开浏览器设置 → 网络/代理 → 手动设置代理;
2. HTTP 代理 `127.0.0.1`,端口 `18080`(浏览器若区分 HTTPS,同值勾选);
3. 访问演示域名,浏览器会弹 Basic 认证框,输入 employee/emp123(或 manager/admin);
4. 预期:corp-doc.com:18001 正常打开;game-site.com:18002 员工账号 403 品牌拦截页
   (页内含命中规则与原因);blocked-site 任何账号 403;
5. 演示完把代理设置改回"不使用代理"。

## 7. 审计与运维报表(代理运行期间,再开终端 C')

```bat
python admin.py report            :: 概览:请求总数/上下行/决策分布(与 5.x 的 403/407/200 对上)
python admin.py blocked           :: 拦截明细:时间/用户/目标/命中规则+原因
python admin.py top-users         :: 下行流量用户排行
python admin.py top-hosts         :: 目标主机排行
python admin.py cache-stats       :: 缓存文件数与占用(5.7 后应为 1)
python admin.py report --day 2026-09-03   :: 任意命令可加 --day 按日过滤
```

库文件默认 `data\audit.db`(SQLite,WAL),直接拷走即可归档。

## 8. 热加载现场演示(可选加分)

1. 终端 C 打 `curl ... -U manager:mng123 http://game-site.com:18002/` → 200(基线);
2. 编辑 `config.toml`,把 `[policy]` 的 `whitelist_mode` 改为 `true` 保存;
3. 立即重打同一条 curl → **403**(白名单外);corp-doc.com → 200(白名单内);
   admin 访问 game-site → 200(admin 显式 `whitelist_mode=false` 豁免);
4. 改回 `false` 保存 → manager 恢复 200。全程代理进程不重启。

## 9. 一键自动化验收(演示前自检 / 报告证据)

```bat
python e2e_proxy.py
:: 预期:10 项全 PASS,退出码 0(第 0 步开始约 5 秒)
python -m unittest discover -s tests -p "test_*.py"
:: 预期:105 项 OK
```

e2e 自动装配临时规则与三角色、真实起服务并逐条断言,免管理员免 hosts。

## 10. 停止与清理

```bat
:: 三个窗口分别 Ctrl+C(demo_sites.py 打印 Ctrl+C 停止)
:: 管理员 PowerShell 清理 hosts 演示映射(可选):
powershell -ExecutionPolicy Bypass -File C:\program1\sc\tools\cleanup_hosts.ps1
```

## 常见问题(FAQ)

| 现象 | 原因与处理 |
|---|---|
| curl 秒回 `couldn't connect` | 代理没起/端口不对:看终端 B 横幅端口;换端口则 `-x` 地址同步改 |
| curl 全部 `000`/无法解析域名 | 没做 hosts 映射(2)或演示域名带 `-x` 时仍走真实 DNS:先跑 setup_hosts.ps1;若跳过映射,把 URL 里的域名换成 127.0.0.1:对应端口 |
| curl 直连演示站能开 | 正常——演示站与代理同机;演示时务必带 `-x` 经代理访问 |
| 浏览器一直转圈/白屏 | 403 拦截页按协议关闭连接:刷新一次;确认 Basic 弹窗输的是 employee/manager/admin 三组账号 |
| 修改 config.toml 不生效 | 热加载只覆盖 [policy]/[users]/[roles] 的规则类键;[proxy] 端口/超时类需重启 |
| admin.py 显示库为空 | 库路径默认 data\audit.db;若换过 --config 目录,给 admin.py 传显式路径 |
| Windows 防火墙弹窗 | 允许 python 通过(专用网络即可),仅本机演示可全不勾选 |
| 控制台中文乱码 | 本项目所有入口已做 UTF-8 处理;如用老版 cmd,先 `chcp 65001` |
