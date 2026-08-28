# Cloudflare Worker 定时触发器

用 Cloudflare Cron 在准点调用 GitHub `workflow_dispatch`，替代 GitHub 自带的 `schedule`。

## 为什么需要

GitHub 的 `schedule` 事件在 Actions 高负载时会排队。本仓库 2026-08 的实测延迟：

| 任务 | 名义时间 | 实际触发 | 延迟 |
| --- | --- | --- | --- |
| A 股日报 08-26 | 16:43 | 17:23 | +40 分 |
| A 股日报 08-27 | 16:43 | **08-28 03:14** | **+631 分** |
| 美股复盘 08-26 | 05:30 | 08:59 | +209 分 |
| 美股复盘 08-27 | 05:30 | **08-28 13:34** | **+484 分** |

10 次采样的平均延迟约 +47 分钟，最坏到 10.5 小时，从未准时。把 cron 名义时间提前 35 分钟只能补偿中位数，对小时级排队无效。

而 `workflow_dispatch` 走 API 直接触发、不进 schedule 队列，实测 4 次手动触发的 `created -> started` 间隔均为 **0 秒**。

Cloudflare Cron 自身并非硬实时（官方说明通常在秒级，高负载可能数分钟），但与 GitHub 的小时级排队不是一个量级。

## 触发计划

Cron 表达式一律 UTC，且必须与 `src/index.js` 的 `ROUTES` 键完全一致。

| Cron (UTC) | 北京时间 | 目标 workflow |
| --- | --- | --- |
| `43 8 * * 1-5` | 16:43 周一至周五 | `00-daily-analysis.yml`（`mode=full`） |
| `30 21 * * 1-5` | 次日 05:30 周二至周六 | `01-us-market-cn-picks.yml`（`sectors=4`） |

## 部署

前置：Node 18+、Cloudflare 账号。

### 1. 准备 GitHub Token

建 fine-grained PAT，权限给到最小：

- Repository access：只选 `xxyou00/daily_stock_analysis`
- Repository permissions → **Actions: Read and write**（仅此一项即可触发 workflow_dispatch）

设一个明确的过期时间并记好续期，过期后触发会静默失败（靠下面的告警发现）。

### 2. 安装与登录

```bash
cd ops/cf-cron-trigger
npm install -g wrangler
wrangler login
```

### 3. 写入密钥

```bash
# 必需
wrangler secret put GH_TOKEN

# 可选但强烈建议：触发失败时往飞书群告警，否则失败是静默的
wrangler secret put ALERT_WEBHOOK_URL

# 可选：配置后才允许 HTTP 手动触发，未配置则 fetch 入口直接 403
wrangler secret put TRIGGER_KEY
```

### 4. 部署

```bash
wrangler deploy
```

### 5. 验证

先用 HTTP 入口验证链路（需已配置 `TRIGGER_KEY`）：

```bash
curl -i -X POST "https://dsa-cron-trigger.<your-subdomain>.workers.dev/?target=cn" \
  -H "X-Trigger-Key: <TRIGGER_KEY>"
```

预期返回 200 且 body 里 `"ok": true, "status": 204`。同时应能看到 GitHub 上出现一次 `workflow_dispatch` 触发的 run：

```bash
gh run list --workflow=00-daily-analysis.yml -R xxyou00/daily_stock_analysis -L 1
```

看实时日志：

```bash
wrangler tail
```

本地跑 Cron 逻辑（不碰线上）：

```bash
wrangler dev --test-scheduled
# 另一个终端
curl "http://localhost:8787/__scheduled?cron=43+8+*+*+1-5"
```

## 切换顺序（重要）

**先确认 Worker 能真实触发，再移除 GitHub 的 `schedule`**，否则会出现两边都不跑的空窗。

两边同时开着会导致重复推送：`concurrency` 配的是 `cancel-in-progress: false`，第二次触发会排队然后照常执行，等于同一天推两轮。

确认 Worker 正常后，注释掉两个 workflow 里的 `schedule` 段（保留 `workflow_dispatch`）：

- `.github/workflows/00-daily-analysis.yml`
- `.github/workflows/01-us-market-cn-picks.yml`

## 回滚

把 workflow 里的 `schedule` 段取消注释即可恢复原状，Worker 可以留着不管（`wrangler delete` 也行）。恢复后延迟问题会一并回来。

## 故障排查

| 现象 | 原因 |
| --- | --- |
| HTTP 401 `鉴权失败` | 请求头 `X-Trigger-Key` 与 `TRIGGER_KEY` 不一致 |
| HTTP 403 `未配置 TRIGGER_KEY` | 没设该密钥，HTTP 入口被刻意禁用，属预期行为 |
| dispatch 返回 401/403 | `GH_TOKEN` 过期，或缺 Actions 写权限 |
| dispatch 返回 404 | `GH_REPO` 写错，或 workflow 文件名不存在于 `GH_REF` 指向的分支 |
| dispatch 返回 422 | `inputs` 与 workflow 定义不匹配（改过 inputs 后要同步 `ROUTES`） |
| 日志出现「未匹配到路由」 | `wrangler.toml` 的 `crons` 与 `src/index.js` 的 `ROUTES` 键不一致 |

4xx 不重试（属配置问题，重试无意义）；5xx 与网络异常重试 3 次，间隔 2s / 4s。
