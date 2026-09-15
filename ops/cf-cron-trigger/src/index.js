/**
 * Cloudflare Worker：按准点用 workflow_dispatch 触发 GitHub Actions。
 *
 * 存在的原因：GitHub 的 schedule 事件在 Actions 高负载时会排队，实测本仓库
 * 2026-08 期间延迟 +32 ~ +631 分钟，最坏一次 A 股日报延到次日凌晨 03:14、
 * 美股复盘延到当天 13:34。而 workflow_dispatch 走 API 直接触发、不进 schedule
 * 队列，实测 created -> started 间隔为 0 秒。
 *
 * 注意 Cloudflare Cron Trigger 自身也非硬实时（通常秒级，高负载可能数分钟），
 * 但与 GitHub 的小时级排队不在一个量级。
 *
 * Cron 表达式一律 UTC。
 */

/**
 * cron 表达式 -> 目标 workflow。键必须与 wrangler.toml 的 crons 完全一致。
 *
 * 刻意不在 cron 里写星期（用 `* * *` 每天触发，工作日判断交给下面的
 * isUtcWeekday）。原因是 Cloudflare 的 cron 解析器对 day-of-week 有一位偏移：
 * 配 `1-5` 实测匹配到的是周日至周四，周五完全不触发（2026-09-04、09-11 的
 * A 股日报因此漏跑，只靠延迟数小时的 GitHub schedule 兜住），而周日反而多跑
 * （08-30、09-06、09-13）。社区已有同类报告：dow 写 5 被解析成周四。
 * 把星期判断放进代码可以绕开该差异，也便于本地测试。
 */
const ROUTES = {
  // UTC 08:43 = 北京 16:43
  '43 8 * * *': {
    key: 'cn',
    label: 'A 股日报（仅大盘复盘）',
    workflow: '00-daily-analysis.yml',
    // mode 在 workflow 里是 required；显式传值，不依赖 API 对 default 的处理。
    //
    // 用 market-only 而非 full：自选股已清空（Repository variable 与
    // Environment STOCK_LIST 下的同名变量都已删除），而 workflow 里有兜底
    // `elif [ -z "${STOCK_LIST:-}" ]; then export STOCK_LIST="600519"`，
    // 走 full 会变成分析贵州茅台——那不是任何人要的标的。market-only 只跑
    // 大盘复盘、完全跳过个股分析，与「没有自选股」的语义一致。
    //
    // 恢复个股分析：重新配置 STOCK_LIST，并把这里改回 mode: 'full'。
    inputs: { mode: 'market-only' },
  },
  // UTC 21:30 = 北京次日 05:30。该时刻同时晚于美股夏/冬令时收盘，又早于 A 股
  // 09:30 开盘。注意按 UTC 工作日过滤，即 UTC 周五 21:30 会触发（北京周六早上
  // 推送美股周五收盘的复盘），这与原 GitHub cron 的行为一致。
  '30 21 * * *': {
    key: 'us',
    label: '美股复盘 + A 股推荐',
    workflow: '01-us-market-cn-picks.yml',
    inputs: { sectors: '4' },
  },
};

/** 按 UTC 判断是否工作日。0=周日、6=周六。 */
function isUtcWeekday(date) {
  const dow = date.getUTCDay();
  return dow !== 0 && dow !== 6;
}

/**
 * 幂等保护：查该 workflow 最近是否已有 workflow_dispatch 触发的 run。
 *
 * Cloudflare 的 cron 并非严格 exactly-once：2026-09-13 08:43:53Z 同一秒产生了
 * 两个 run（34748404865 / 34748404972），等于当天推送了两遍。dispatch 本身不
 * 幂等，重试或重复触发都会各起一个 run，因此在发起前先查一次。
 */
async function recentlyDispatched(route, env, windowMinutes = 15) {
  const repo = requiredEnv(env, 'GH_REPO');
  const token = requiredEnv(env, 'GH_TOKEN');
  const url = `${GITHUB_API}/repos/${repo}/actions/workflows/${route.workflow}/runs`
    + '?event=workflow_dispatch&per_page=5';
  try {
    const response = await fetch(url, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'dsa-cf-cron-trigger',
      },
    });
    if (!response.ok) {
      // 查不到就放行：宁可重复也不要漏触发，重复至少有报告可看
      console.warn(`[idempotency] 查询最近 run 失败 status=${response.status}，跳过幂等检查`);
      return null;
    }
    const data = await response.json();
    const cutoff = Date.now() - windowMinutes * 60 * 1000;
    const hit = (data.workflow_runs || []).find(
      (run) => new Date(run.created_at).getTime() > cutoff,
    );
    return hit ? { id: hit.id, createdAt: hit.created_at } : null;
  } catch (error) {
    console.warn(`[idempotency] 查询最近 run 异常：${error}，跳过幂等检查`);
    return null;
  }
}

const GITHUB_API = 'https://api.github.com';
const MAX_ATTEMPTS = 3;

function requiredEnv(env, name) {
  const value = env[name];
  if (!value) throw new Error(`缺少必需的环境变量/密钥：${name}`);
  return value;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 调用 workflow_dispatch。成功返回 204 No Content。
 * 仅对 5xx 与网络异常重试；4xx 属配置错误，重试无意义。
 */
async function dispatchWorkflow(route, env) {
  const repo = requiredEnv(env, 'GH_REPO');            // 形如 owner/name
  const token = requiredEnv(env, 'GH_TOKEN');
  const ref = env.GH_REF || 'main';
  const url = `${GITHUB_API}/repos/${repo}/actions/workflows/${route.workflow}/dispatches`;

  let lastError = null;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          // GitHub 要求带 UA，缺失会被拒
          'User-Agent': 'dsa-cf-cron-trigger',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ ref, inputs: route.inputs || {} }),
      });

      if (response.status === 204) {
        console.log(`[dispatch] ok label=${route.label} workflow=${route.workflow} ref=${ref} attempt=${attempt}`);
        return { ok: true, status: 204, attempt };
      }

      // 4xx 直接失败：401/403 是凭据或权限，404 是仓库/文件名错，422 是 inputs 不匹配
      const body = await response.text();
      const detail = body.slice(0, 300);
      if (response.status < 500) {
        console.error(`[dispatch] 配置类失败 status=${response.status} label=${route.label} detail=${detail}`);
        return { ok: false, status: response.status, detail, attempt };
      }

      lastError = `status=${response.status} detail=${detail}`;
      console.warn(`[dispatch] 服务端错误，准备重试 attempt=${attempt}/${MAX_ATTEMPTS} ${lastError}`);
    } catch (error) {
      lastError = String(error);
      console.warn(`[dispatch] 网络异常，准备重试 attempt=${attempt}/${MAX_ATTEMPTS} ${lastError}`);
    }

    if (attempt < MAX_ATTEMPTS) await sleep(2000 * attempt);
  }

  return { ok: false, status: 0, detail: lastError, attempt: MAX_ATTEMPTS };
}

/**
 * 触发失败时告警。Worker 里的失败是静默的，没有告警就只能等发现「今天没收到报告」。
 * 未配置 ALERT_WEBHOOK_URL 时跳过（不视为错误）。
 */
async function alert(env, text) {
  const webhook = env.ALERT_WEBHOOK_URL;
  if (!webhook) {
    console.warn('[alert] 未配置 ALERT_WEBHOOK_URL，跳过告警');
    return;
  }
  try {
    await fetch(webhook, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msg_type: 'text', content: { text } }),
    });
    console.log('[alert] 告警已发送');
  } catch (error) {
    // 告警失败不能反过来影响主流程
    console.error(`[alert] 告警发送失败：${error}`);
  }
}

async function runRoute(route, env, trigger, { checkIdempotency = false } = {}) {
  const startedAt = new Date().toISOString();
  console.log(`[run] trigger=${trigger} label=${route.label} at=${startedAt}`);

  if (checkIdempotency) {
    const existing = await recentlyDispatched(route, env);
    if (existing) {
      console.log(
        `[run] 跳过：${route.workflow} 15 分钟内已有 dispatch run `
        + `id=${existing.id} created=${existing.createdAt}`,
      );
      return { ok: true, skipped: true, existingRunId: existing.id };
    }
  }

  const result = await dispatchWorkflow(route, env);
  if (!result.ok) {
    await alert(
      env,
      `[DSA 定时触发失败] ${route.label}\n` +
        `workflow: ${route.workflow}\n` +
        `触发源: ${trigger}\n` +
        `HTTP: ${result.status || '网络异常'}\n` +
        `详情: ${result.detail || '无'}\n` +
        `时间(UTC): ${startedAt}\n` +
        `请手动执行：gh workflow run ${route.workflow} -R ${env.GH_REPO || '<repo>'}`,
    );
  }
  return result;
}

export default {
  /** Cron 触发入口 */
  async scheduled(event, env, ctx) {
    const route = ROUTES[event.cron];
    if (!route) {
      // 说明 wrangler.toml 的 crons 与 ROUTES 不一致，属部署配置错误
      console.error(`[scheduled] 未匹配到路由，cron=${event.cron}；请核对 ROUTES 与 wrangler.toml`);
      await alert(env, `[DSA 定时触发异常] 收到未知 cron：${event.cron}`);
      return;
    }

    // 工作日过滤放在代码里，不依赖 Cloudflare 的 dow 解析（见 ROUTES 上方说明）
    const scheduledAt = new Date(event.scheduledTime);
    if (!isUtcWeekday(scheduledAt)) {
      console.log(
        `[scheduled] 跳过：UTC 周末 dow=${scheduledAt.getUTCDay()} `
        + `scheduledTime=${scheduledAt.toISOString()} label=${route.label}`,
      );
      return;
    }

    // 用 waitUntil 保证异步收尾不被提前回收；cron 路径开启幂等检查
    ctx.waitUntil(runRoute(route, env, `cron:${event.cron}`, { checkIdempotency: true }));
  },

  /**
   * HTTP 入口，仅用于手动验证。
   * 必须配置 TRIGGER_KEY 且请求头 X-Trigger-Key 匹配，否则一律拒绝——
   * Worker 的 URL 是公开的，没有这道校验等于把触发能力暴露给任何人。
   */
  async fetch(request, env) {
    const expected = env.TRIGGER_KEY;
    if (!expected) {
      return new Response('未配置 TRIGGER_KEY，HTTP 触发已禁用\n', { status: 403 });
    }
    if (request.headers.get('X-Trigger-Key') !== expected) {
      return new Response('鉴权失败\n', { status: 401 });
    }

    const url = new URL(request.url);
    const target = url.searchParams.get('target');
    const base = Object.values(ROUTES).find((item) => item.key === target);
    if (!base) {
      const available = Object.values(ROUTES).map((item) => item.key).join(' | ');
      return new Response(`用法：?target=${available}[&<input>=<value>...]\n`, { status: 400 });
    }

    // target 之外的 query 参数原样并入 inputs，便于验证时传 no_notify=true /
    // dry_run=true 而不真的推送。值一律按字符串传，GitHub 对 boolean 型 input
    // 也接受 "true"/"false"。
    const overrides = {};
    for (const [key, value] of url.searchParams) {
      if (key !== 'target') overrides[key] = value;
    }
    const route = { ...base, inputs: { ...(base.inputs || {}), ...overrides } };

    // HTTP 路径是人工操作，不做幂等拦截，避免「刚跑过就手动补一次」被误跳过
    const result = await runRoute(route, env, 'http');
    return new Response(JSON.stringify({ label: route.label, inputs: route.inputs, ...result }, null, 2) + '\n', {
      status: result.ok ? 200 : 502,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  },
};
