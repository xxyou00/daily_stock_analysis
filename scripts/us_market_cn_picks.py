#!/usr/bin/env python3
"""美股收盘复盘 + 关联 A 股标的推荐。

用途
----
在美股收盘后运行，产出两段内容并推送到已配置的通知渠道：

1. 美股大盘复盘——直接复用项目内的 ``run_market_review(override_region="us")``，
   与每日 A 股复盘走同一套指数、情报与 LLM 链路，只是区域切到美股；
2. 关联 A 股推荐——把上一步的美股复盘作为上下文交给 LLM，要求给出 10 只
   受美股走势影响的 A 股，并**逐个用本地股票索引校验代码真伪**。

设计取舍
--------
- 与每日 A 股任务完全解耦：区域通过 ``override_region`` 传入，不修改
  ``MARKET_REVIEW_REGION`` 全局配置，因此不会干扰 18:00 的 A 股复盘。
- LLM 会编造股票代码。这里对每一条推荐都用 ``get_index_stock_name`` 反查：
  代码不存在直接丢弃；代码存在但名称与 LLM 输出不一致时，以索引名称为准。
  宁可少推几只，也不推不存在的标的。
- 推荐属于模型观点而非事实，输出中显式标注来源与免责，且不给出价格与仓位建议。
- 任一环节失败都不静默：以非零退出码结束，便于 CI 发现。

用法
----
    python scripts/us_market_cn_picks.py            # 生成并推送
    python scripts/us_market_cn_picks.py --dry-run  # 只打印，不推送
    python scripts/us_market_cn_picks.py --picks 5  # 自定义推荐数量
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("us_market_cn_picks")

DEFAULT_PICK_COUNT = 10

# LLM 返回里允许出现的字段，多余字段忽略
_PICK_FIELDS = ("code", "name", "sector", "logic", "linkage")


def _build_prompt(us_review: str, pick_count: int, capital_flow: str = "") -> str:
    """构造 A 股推荐 prompt。要求严格 JSON，便于机器校验。"""
    capital_flow_section = ""
    if capital_flow.strip():
        capital_flow_section = f"""
<美股板块与龙头资金动向>
{capital_flow}
</美股板块与龙头资金动向>

注意：美股没有龙虎榜披露制度，上面这段是用板块 ETF 与权重龙头的当日表现
替代观察资金流向，请把它当作**板块级资金偏好**的证据来用。
"""

    return f"""你是一位同时覆盖美股与 A 股的跨市场策略分析师。

下面是刚刚结束的美股交易日复盘：

<美股复盘>
{us_review}
</美股复盘>
{capital_flow_section}
## 任务

基于上述美股复盘{"与板块资金动向" if capital_flow_section else ""}，挑选 {pick_count} 只
**可能受此影响的 A 股**，并说明传导逻辑。

## 硬性要求

1. 必须是真实存在的 A 股，代码为 6 位数字（沪市 600/601/603/605、科创 688、
   深市 000/001/002、创业板 300）。不确定的标的直接不要写。
2. 不要写港股、美股、ETF、退市股、ST 股。
3. 传导逻辑必须落到具体的产业链或事件关系上（如「美股 AI 算力链上涨 → 国内光模块代工」），
   禁止「受大盘情绪影响」这类空话。**若已给出板块资金动向，传导逻辑应优先锚定到
   具体的领涨/领跌板块或龙头个股，而不是笼统的指数涨跌。**
4. 不要给出目标价、买入价、止损位或仓位建议。
5. {pick_count} 只标的应分散在不同板块，不要集中在同一条产业链。

## 输出格式

只输出 JSON，不要出现任何解释文字或 markdown 代码块标记：

{{
  "us_summary": "一句话概括美股当日核心变化（40 字以内）",
  "picks": [
    {{
      "code": "600519",
      "name": "股票中文名称",
      "sector": "所属板块",
      "linkage": "对应的美股标的或板块",
      "logic": "传导逻辑，50 字以内"
    }}
  ]
}}
"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中提取 JSON 对象，容忍代码块包裹与前后噪声。"""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 退一步：抓第一个平衡的花括号块
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(cleaned)):
        if cleaned[index] == "{":
            depth += 1
        elif cleaned[index] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _validate_picks(
    raw_picks: List[Dict[str, Any]], pick_count: int
) -> Tuple[List[Dict[str, str]], List[str]]:
    """用本地股票索引校验推荐代码，返回 (有效推荐, 丢弃说明)。

    LLM 编造代码或记错名称都很常见，这里以索引为准：
    代码查不到就丢弃，名称不一致就用索引名覆盖。
    """
    from src.data.stock_index_loader import get_index_stock_name

    valid: List[Dict[str, str]] = []
    dropped: List[str] = []
    seen: set = set()

    for item in raw_picks:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        code = re.sub(r"[^0-9]", "", code)
        if len(code) != 6:
            dropped.append(f"{item.get('code')}（代码格式非 6 位数字）")
            continue
        if code in seen:
            continue
        if not code.startswith(("600", "601", "603", "605", "688", "000", "001", "002", "300")):
            dropped.append(f"{code}（非 A 股主板/创业板/科创板代码段）")
            continue

        index_name = None
        try:
            index_name = get_index_stock_name(code)
        except Exception as exc:  # pragma: no cover - 索引异常不应中断流程
            logger.debug("索引查询 %s 失败: %s", code, exc)

        if not index_name:
            dropped.append(f"{code}（本地股票索引中不存在）")
            continue

        llm_name = str(item.get("name", "")).strip()
        if llm_name and llm_name != index_name:
            logger.info("代码 %s 名称以索引为准：LLM=%s -> 索引=%s", code, llm_name, index_name)

        seen.add(code)
        valid.append(
            {
                "code": code,
                "name": index_name,
                "sector": str(item.get("sector", "")).strip() or "-",
                "linkage": str(item.get("linkage", "")).strip() or "-",
                "logic": str(item.get("logic", "")).strip() or "-",
            }
        )
        if len(valid) >= pick_count:
            break

    return valid, dropped


def _render_report(
    us_review: str,
    us_summary: str,
    picks: List[Dict[str, str]],
    dropped: List[str],
    model_name: str,
    capital_flow: str = "",
) -> str:
    """拼装推送正文。美股复盘在前，A 股推荐在后。"""
    from datetime import datetime

    lines: List[str] = []
    lines.append(f"# 🌎 {datetime.now().strftime('%Y-%m-%d')} 美股复盘 & A股关联推荐")
    lines.append("")
    if us_summary:
        lines.append(f"> {us_summary}")
        lines.append("")
    lines.append(us_review.strip())
    lines.append("")
    if capital_flow.strip():
        # 复盘正文里已含该段落时不重复输出（region=us 的复盘会自带）
        if "板块领涨" not in us_review:
            lines.append("## 💵 美股板块与龙头资金动向")
            lines.append("")
            lines.append(capital_flow.strip())
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"## 🇨🇳 关联 A 股推荐（{len(picks)} 只）")
    lines.append("")
    if picks:
        lines.append("| # | 代码 | 名称 | 板块 | 对标美股 | 传导逻辑 |")
        lines.append("|---|------|------|------|----------|----------|")
        for position, pick in enumerate(picks, 1):
            lines.append(
                f"| {position} | {pick['code']} | {pick['name']} | {pick['sector']} "
                f"| {pick['linkage']} | {pick['logic']} |"
            )
    else:
        lines.append("本次未产出通过代码校验的推荐标的。")
    lines.append("")
    if dropped:
        lines.append(f"> 已剔除 {len(dropped)} 条未通过代码校验的候选：{('；'.join(dropped))[:200]}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"⚠️ **免责说明**：A 股推荐由大模型（{model_name}）基于美股复盘推理生成，"
        "属模型观点而非事实，代码已通过本地股票索引校验但基本面未经核实。"
        "跨市场传导存在时滞与失效可能，本内容不构成投资建议。"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="美股复盘 + 关联 A 股推荐推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不推送通知")
    parser.add_argument(
        "--picks", type=int, default=DEFAULT_PICK_COUNT, help=f"推荐数量，默认 {DEFAULT_PICK_COUNT}"
    )
    parser.add_argument(
        "--force-run", action="store_true", help="跳过美股交易日检查，强制执行"
    )
    args = parser.parse_args()
    pick_count = max(1, min(30, int(args.picks)))

    from src.logging_config import setup_logging

    setup_logging()

    # --- 0. 美股交易日检查 ---
    # 本脚本直连 run_market_review，不经过 main.py 的交易日门禁，因此需自行判断。
    # 运行时点为北京 05:30，对应美东前一日收盘后，故以美东当前日期为准。
    if not args.force_run:
        try:
            from src.core.trading_calendar import get_market_now, is_market_open

            us_now = get_market_now("us")
            us_date = us_now.date()
            if not is_market_open("us", us_date):
                logger.info(
                    "美股 %s 为非交易日（美东时间 %s），跳过执行。"
                    "如需强制运行请加 --force-run。",
                    us_date.isoformat(),
                    us_now.strftime("%Y-%m-%d %H:%M %Z"),
                )
                return 0
            logger.info("美股 %s 为交易日，继续执行", us_date.isoformat())
        except Exception as exc:
            # 交易日日历不可用时 fail-open，与项目内 is_market_open 的语义一致
            logger.warning("美股交易日检查失败，按交易日继续执行: %s", exc)

    from src.analyzer import GeminiAnalyzer
    from src.config import get_config
    from src.core.market_review import run_market_review
    from src.notification import NotificationService
    from src.search_service import SearchService
    from src.storage import get_db

    config = get_config()
    get_db()  # 初始化数据库，复盘链路会写入历史

    analyzer = GeminiAnalyzer(config=config)
    notifier = NotificationService(source_message="us_market_cn_picks")
    search_service = None
    try:
        search_service = SearchService(
            bocha_keys=config.bocha_api_keys,
            tavily_keys=config.tavily_api_keys,
            anspire_keys=config.anspire_api_keys,
            brave_keys=config.brave_api_keys,
            serpapi_keys=config.serpapi_keys,
            minimax_keys=config.minimax_api_keys,
            searxng_base_urls=config.searxng_base_urls,
            searxng_public_instances_enabled=config.searxng_public_instances_enabled,
            news_max_age_days=config.news_max_age_days,
            news_strategy_profile=getattr(config, "news_strategy_profile", "short"),
        )
    except Exception as exc:
        logger.warning("搜索服务初始化失败，将以无搜索模式运行: %s", exc)

    # --- 1. 美股复盘（不在这里推送，稍后与 A 股推荐合并发送）---
    logger.info("开始美股大盘复盘 ...")
    review = run_market_review(
        notifier=notifier,
        analyzer=analyzer,
        search_service=search_service,
        config=config,
        send_notification=False,
        override_region="us",
        save_report_file=False,
        trigger_source="us_market_cn_picks",
    )
    us_review = review if isinstance(review, str) else getattr(review, "report", "") or ""
    if not us_review.strip():
        logger.error("美股复盘未产出内容，终止")
        return 2
    logger.info("美股复盘完成，长度 %s 字符", len(us_review))

    # --- 2. 美股板块与龙头资金动向（作为推荐的额外证据）---
    capital_flow = ""
    try:
        from src.services.capital_flow_overview import build_us_capital_flow_context

        capital_flow = build_us_capital_flow_context()
        if capital_flow:
            logger.info("已获取美股板块与龙头资金动向，将纳入推荐依据")
        else:
            logger.info("未获取到美股资金动向，推荐将仅基于指数复盘")
    except Exception as exc:
        logger.warning("美股资金动向获取失败（不影响推荐）: %s", exc)

    # --- 3. LLM 生成 A 股推荐 ---
    logger.info("请求 LLM 生成 %s 只关联 A 股 ...", pick_count)
    raw = analyzer.generate_text(
        _build_prompt(us_review, pick_count, capital_flow),
        max_tokens=4096,
        temperature=getattr(config, "llm_temperature", 0.7),
    )
    if not raw:
        logger.error("LLM 未返回内容，终止")
        return 3

    parsed = _extract_json(raw)
    if not parsed or not isinstance(parsed.get("picks"), list):
        logger.error("LLM 输出无法解析为预期 JSON：%s", (raw or "")[:300])
        return 4

    picks, dropped = _validate_picks(parsed.get("picks", []), pick_count)
    logger.info("代码校验通过 %s 只，剔除 %s 条", len(picks), len(dropped))
    for note in dropped:
        logger.warning("剔除候选：%s", note)

    # --- 4. 组装并推送 ---
    report = _render_report(
        us_review=us_review,
        us_summary=str(parsed.get("us_summary", "")).strip(),
        picks=picks,
        dropped=dropped,
        model_name=getattr(config, "litellm_model", "") or "LLM",
        capital_flow=capital_flow,
    )

    if args.dry_run:
        print(report)
        logger.info("dry-run 模式，未推送")
        return 0

    sent = notifier.send(report, route_type="report")
    if not sent:
        logger.error("所有通知渠道均推送失败")
        return 5
    logger.info("推送成功：美股复盘 + %s 只 A 股推荐", len(picks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
