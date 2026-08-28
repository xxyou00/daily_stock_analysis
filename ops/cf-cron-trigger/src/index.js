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

/** cron 表达式 -> 目标 workflow。键必须与 wrangler.toml 的 crons 完全一致。 */
const ROUTES = {
  // UTC 08:43 = 北京 16:43（周一至周五）
  '43 8 * * 1-5': {
    key: 'cn',
    label: 'A 股日报',
    workflow: '00-daily-analysis.yml',
    // mode 在 workflow 里是 required；显式传值，不依赖 API 对 default 的处理
    inputs: { mode: 'full' },
  },
  // UTC 21:30 = 北京次日 05:30（周二至周六）。该时刻同时晚于美股夏/冬令时收盘，
  // 又早于 A 股 09:30 开盘。
  '30 21 * * 1-5': {
    key: 'us',
    label: '美股复盘 + A 股推荐',
    workflow: '01-us-market-cn-picks.yml',
    inputs: { sectors: '4' },
  },
};

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

async function runRoute(route, env, trigger) {
  const startedAt = new Date().toISOString();
  console.log(`[run] trigger=${trigger} label=${route.label} at=${startedAt}`);
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
    // 用 waitUntil 保证异步收尾不被提前回收
    ctx.waitUntil(runRoute(route, env, `cron:${event.cron}`));
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

    const target = new URL(request.url).searchParams.get('target');
    const route = Object.values(ROUTES).find((item) => item.key === target);
    if (!route) {
      const available = Object.values(ROUTES).map((item) => item.key).join(' | ');
      return new Response(`用法：?target=${available}\n`, { status: 400 });
    }

    const result = await runRoute(route, env, 'http');
    return new Response(JSON.stringify({ label: route.label, ...result }, null, 2) + '\n', {
      status: result.ok ? 200 : 502,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  },
};
