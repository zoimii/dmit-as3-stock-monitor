# DMIT AS3 官方库存监控

独立于 ChatGPT 的 GitHub Actions + Python + Telegram 监控项目。只监控以下两款产品，**不读取第三方库存、不匹配 AN5、不购买服务器**。

| 官方产品名 | PID | 参考月价 | 官方购买入口 |
|---|---|---|---|
| LAX.AS3.Pro.TINY | 253 | $10.90 USD | https://www.dmit.io/cart.php?a=add&pid=253 |
| LAX.AS3.Pro.Pocket | 254 | $16.90 USD | https://www.dmit.io/cart.php?a=add&pid=254 |

参考价不是固定价格过滤条件；官方价格变化时，消息显示本次验证价格。PID 和名称必须同时对应正确产品。

## 当前交付状态

项目已具备运行脚本、workflow、测试、安全配置及部署辅助程序。是否已部署到你的 GitHub，以本次交付说明及你账号的 Actions 记录为准，**本地文件存在不等于监控已在云端运行**。

2026-10-27 本次准备时，两个官方入口经联网访问均为 HTTP 403，不能确认可下单。成功页面测试使用自建的 WHMCS 结构夹具，**不是 DMIT 真实有货页面证据**。当前严格适配 WHMCS standard_cart 结构；DMIT 模板或流程不匹配时会保持 unknown，需要人工检查后修改适配器，不会降低通知条件。

本项目不会读取、暂停或修改已有 ChatGPT 每小时监控。

## 官方确认规则

每个 PID 使用新的匿名 Cookie 会话，不使用你的 DMIT 登录信息。只有所有环节通过，才产生补货通知：

1. HTTPS 官方 `cart.php?a=add&pid=...` 返回 200；配置表单内完整名称与 PID 对应，不接受通用产品列表或“Add to Cart”字样。
2. 官方提供可用的月付选项及正数美元价格，继续按钮启用。
3. 仅向官方 `cart.php` 发送 `a=confproduct&ajax=1` 配置请求，收到 WHMCS 明确接受配置的空 200 响应。
4. 同一会话购物车只含一个正确产品，月付价格与配置页一致，并提供可用结算入口。
5. GET 官方结算页成功，出现合法结算表单及可用的完成订单按钮。**程序到此停止，不向结算接口 POST，不提交订单或付款。**

临时购物车如要求主机字段，只填写 `inventory-probe.invalid`、随机临时密码和 ns1/ns2 前缀。这些内容不会写入日志、状态或消息；会话不会保存到下次运行。未知必填自定义字段不猜测填充。

HTTP 403/429/5xx、网络/TLS 错误、验证码、登录页、模板变化、价格/名称不符、配置拒绝、结算不可达均为 **unknown（无法验证）**。只有官方可见的缺货错误才记作 sold_out。出现验证码时不绕过。该规则确认检查当时的匿名购买路径可继续，不保证最终支付、账号风控或你点击时仍有库存。

## 调度与免费额度（核查于 2026-10-27）

- 调度为 `2-57/5 * * * *`：每小时第 2、7、12…57 分钟，避开整点。GitHub 最短支持每 5 分钟；不是 3 分钟服务。
- GitHub 明确说明 schedule 可能延迟，负载高时可能丢弃排队任务；只运行默认分支上的 workflow。无法承诺每次间隔不超过 5 分钟。[调度文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- 公开仓库使用标准 GitHub runner 免费。本项目固定 `ubuntu-24.04`，不使用收费的大型 runner。私有 GitHub Free 仓库每月只有 2,000 分钟；30 天每 5 分钟计划约 8,640 次，即使每个任务只计 1 分钟也通常超额。[计费文档](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[分钟取整规则](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-job-execution-time)
- 公开仓库 60 天无仓库活动可能自动停用 schedule。状态分支会提交实际检查记录，但不要假定这能永久避免停用。建议 45 天内手动检查 Actions 最近运行时间、更新一次默认分支；停用后在 Actions 启用或修改 cron。[触发及停用规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- 代码、检查日志、状态及通知正文会公开；状态内没有 bot token、chat ID、Cookie、CSRF token、临时密码或 DMIT 账号信息。不使用 artifact/cache 保存去重状态。

## 需要你授权或提供的内容

| 内容 | 用途 | 输入位置 |
|---|---|---|
| GitHub 登录与仓库创建、代码/workflow 推送权限 | 创建你名下专用公开仓库 | GitHub 官方浏览器授权 |
| 专用仓库的 Actions、Secrets、Variables 管理权限 | 配置通知、启动/查看运行 | GitHub CLI 或该仓库设置页 |
| Telegram Bot token | 该 bot 给你发送通知 | 本机隐藏输入，或 GitHub Secret `TELEGRAM_BOT_TOKEN` |
| 目标 Telegram chat ID | 确定接收会话 | 本机隐藏输入/自动识别，或 Secret `TELEGRAM_CHAT_ID` |

**不要在聊天、Issue、源码、命令参数、截图或 `.env` 文件中填写真实 token。** 不需要 DMIT 账号、密码、支付信息或独立的运行期 GitHub PAT。云端使用 GitHub 自动提供的 `GITHUB_TOKEN`，job 申请 `contents: write` 用于状态分支。该权限在平台上是仓库范围，程序只写状态分支。

GitHub CLI 浏览器 OAuth 会显示其默认权限（可能包含 repo/read:org/gist，另需 workflow）；它可能比本专用仓库的最小权限宽，授权前应看 GitHub 显示的范围。希望限制到单一仓库时，可自行先创建专用公开仓库，用仅选该仓库的 fine-grained token，授予 Contents/Actions/Secrets/Variables 的读写及 Metadata 读取；token 仍只在你自己的终端或凭据管理器配置，不在聊天发送。此替代方式需要自行完成首次推送。

## 部署步骤

需要 Python 3.9+、Git 和 GitHub CLI。运行期 Python 只使用标准库，无需 pip 安装。

### 1. 在你本机终端完成 GitHub 浏览器登录

```sh
gh auth login --hostname github.com --git-protocol https --web --scopes workflow
gh api user --jq .login
```

如果已登录，先确认 `gh auth status` 成功，并具备推送 workflow 的权限。不要运行或分享 `gh auth token` 的输出。

### 2. 进入本项目并创建专用公开仓库

```sh
python3 scripts/deploy.py
```

默认创建 `你的账号/dmit-as3-stock-monitor`。也可用 `--repository OWNER/REPO` 指定名字。脚本先运行测试，不覆盖已有远端仓库。若创建/推送中断，请先检查 GitHub 仓库和本地 remote，避免重复创建。初始 `MONITOR_ENABLED=false`，未完成通知验证前不会执行定时库存检查。

### 3. 配置 Telegram 凭据（由你本人操作）

在 Telegram 官方 [@BotFather](https://t.me/BotFather) 用 `/newbot` 创建专用 bot。把 token 保留在自己手上，然后在项目目录执行（将 OWNER/REPO 换成上一步真实仓库）：

```sh
python3 scripts/configure_telegram.py --repository OWNER/REPO --discover
```

此脚本隐藏输入 token，让你给 bot 发送本次随机 `/start` 口令，只匹配该口令的私聊来确定 chat ID，不输出聊天内容。两个值通过标准输入交给 `gh secret set`，不存本地、不放进命令参数。建议使用新建专用 bot；不修改现有 webhook。若 bot 的更新队列很满、使用 webhook、或者需要群聊通知，省略 `--discover` 并在隐藏提示中输入已知 chat ID，群聊需先将 bot 加入并授予发消息权限。

也可在仓库 **Settings → Secrets and variables → Actions → New repository secret** 亲自创建 `TELEGRAM_BOT_TOKEN` 与 `TELEGRAM_CHAT_ID`，不必向 Codex 展示内容。[官方 Secrets 操作说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

### 4. 验证云端通知、初始化状态并启用

```sh
python3 scripts/deploy.py --repository OWNER/REPO --activate
```

程序依次等待：云端 Telegram 测试成功 → 首次创建持久状态（已有时不重置）→ 打开 `MONITOR_ENABLED=true` → 首次官方检查。测试通知明确写着“通知测试，非补货”。首次官方检查可能由于 403 失败，程序会如实报告；此时定时任务仍会继续，按退避策略重试。

也可在 GitHub 手动完成：

1. Actions → DMIT stock monitor → Run workflow，mode 选 `telegram-test`，确认收到测试消息。
2. 首次部署再选 `initialize`，确认成功；已有状态不要重新初始化。
3. Settings → Secrets and variables → Actions → Variables，把 `MONITOR_ENABLED` 设置为字符串 `true`。
4. 手动 mode=`check`，检查运行摘要和日志。`unknown` 的退出码是 2，Actions 会显示失败，避免把“程序执行了”误报为“官方验证成功”。

## 日志、去重与故障处理

- 日志为 JSON，记录 PID、三态结果、固定原因代码、价格和响应证据 SHA-256。不记录原始 HTML、请求 URL 中的凭据、Cookie、完整异常或 Telegram 响应。
- `monitor-state` 分支的 `state.json` 保存每个 PID 的状态、下一次检查时间、已通知库存轮次及 outbox。每次更新使用 GitHub Contents API 的 SHA 条件写入，冲突就停止。workflow 串行运行；不依赖可能淘汰的 Actions cache。
- 每个有货轮次最多发一次。只有连续两次官方明确 sold_out 才重新准备下一轮通知；unknown 不会重置去重。首次运行如确认有货会提醒。
- 通知意图和发送中状态都先持久化再发送。明确拒绝（如 Telegram 429）按退避重试，并要求**本次重新通过官方验证**，不发送过期的补货消息。
- Telegram 没有 sendMessage 幂等键。如果网络中断或发送后写状态失败，可能无法确认是否送达。为减少重复，本项目标记 `uncertain` 并停止该事件的自动重发；极端情况下可能漏一条。请核查你的 Telegram 会话，再在状态分支把该事件的 `status` 改为 `sent`（已收到）或 `cancelled`（放弃该条）。不要删除整个状态文件；需要再次提醒时先重新人工验证官方页面。
- 官方 unknown 的持续退避为 5、10、20、30 分钟，之后最多每 30 分钟；429 的 Retry-After 可延长至一天。正常可验证时每 5 分钟检查。手动 `force=true` 可忽略一次退避。
- 连续三次 unknown 后发送“监控异常，非补货”，每个 PID 最多一天一次；这一消息不宣称有货。状态/Secrets/Actions 运行本身失败时可能无法发 Telegram，需查看 GitHub Actions 通知及最近运行时间。
- 退出码 0：两款官方状态明确且无未解决投递；1：配置/持久化/意外错误；2：存在无法验证库存或待处理投递。暂时红色运行是可见的降级状态，不会转换成补货成功。

暂停：把仓库变量 `MONITOR_ENABLED` 设为 `false`；会让之后的定时检查跳过，不影响显式手动运行。也可在 Actions 禁用整个 workflow。更换 bot token 后重新运行安全配置及 `--activate`，已有去重状态保留。

长期 403：核查 GitHub runner 的真实访问结果；必要时联系 DMIT 确认允许的公开检查方式或改用能合法访问的云主机。此项目不破解验证码，也不以第三方数据替代官方确认。若要求严格 3–5 分钟不中断，需要独立常驻服务及独立健康检查；GitHub schedule 不提供此保证。

## 本地验证与维护

```sh
python3 -m unittest discover -s tests -v
python3 -m dmit_monitor --mode inspect
```

`inspect` 只执行官方探测，不读取通知 Secrets、不发通知、不保存状态。它仍可能建立并验证临时匿名购物车；不会提交订单。

模块：`official.py`（仅官方适配器）、`http.py`（限定域名及 TLS）、`monitor.py`（状态机/outbox）、`storage.py`（持久化）、`telegram.py`（脱敏发送）。测试夹具在 `tests/test_monitor.py`，覆盖 403、429、AN5/错误 PID、挑战页、缺货、模板/表单变化、错误价格/周期、结算失败、跨运行去重、退避与投递中断。修改适配器时必须保留这些安全回归测试，并用人工核验的官方页面补充脱敏夹具。

CI 的 PR 测试只有只读权限，不接触 Telegram Secrets；库存检查仅由 schedule 或人工触发，不使用 pull_request_target。checkout 固定到核验过的完整提交，Dependabot 每月提出更新。

实现参考：[WHMCS 官方标准配置表单](https://github.com/WHMCS/orderforms-standard_cart/blob/master/configureproduct.tpl)、[官方购物车](https://github.com/WHMCS/orderforms-standard_cart/blob/master/viewcart.tpl)、[官方表单交互](https://github.com/WHMCS/orderforms-standard_cart/blob/master/js/scripts.js)、[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)。
