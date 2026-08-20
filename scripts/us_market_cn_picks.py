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

DEFAULT_SECTOR_COUNT = 4
# 每个板块推荐的标的数。按板块成组给出，比一张扁平榜单更容易看出传导路径。
PICKS_PER_SECTOR = 3
# 只保留看多方向。承压/回避方向的判断放在美股复盘的「风险提示」里，
# 不混进推荐列表——推荐列表里出现看空标的容易被误读成建议做空。
_BULLISH_TOKENS = ("看多", "多头", "利好", "受益", "偏多", "正向", "bullish", "positive")
_BEARISH_TOKENS = ("看空", "空头", "利空", "承压", "偏空", "负向", "回避", "bearish", "negative")


def _build_prompt(
    us_review: str,
    sector_count: int,
    capital_flow: str = "",
    picks_per_sector: int = PICKS_PER_SECTOR,
) -> str:
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

基于上述美股复盘{"与板块资金动向" if capital_flow_section else ""}，选出 {sector_count} 个
**受美股走势正向影响的 A 股板块**，每个板块给出 {picks_per_sector} 只该板块内的代表标的。

## 硬性要求

1. **只输出看多方向。** 仅选择受益、受正向传导的板块与标的。承压、利空、
   建议回避的方向一律不要出现在结果里——那部分判断由复盘的风险提示承担。
   每个板块必须显式标注 `"direction": "看多"`。
2. 必须是真实存在的 A 股，代码为 6 位数字（沪市 600/601/603/605、科创 688、
   深市 000/001/002、创业板 300）。不确定的标的直接不要写。
3. 不要写港股、美股、ETF、退市股、ST 股。
4. 板块级传导逻辑必须落到具体的产业链或事件关系上（如「美股 AI 算力链上涨 →
   国内光模块代工」），禁止「受大盘情绪影响」这类空话。**若已给出板块资金动向，
   传导逻辑应优先锚定到具体的领涨板块或龙头个股，而不是笼统的指数涨跌。**
5. 每只标的要写清它在该板块里的定位（龙头、弹性最大、纯度最高等），
   不要三只标的用同一句话。
6. 不要给出目标价、买入价、止损位或仓位建议。
7. {sector_count} 个板块之间要分散，不要都落在同一条产业链上。

## 输出格式

只输出 JSON，不要出现任何解释文字或 markdown 代码块标记：

{{
  "us_summary": "一句话概括美股当日核心变化（40 字以内）",
  "sectors": [
    {{
      "sector": "板块名称",
      "direction": "看多",
      "linkage": "对应的美股标的或板块",
      "logic": "板块级传导逻辑，50 字以内",
      "picks": [
        {{
          "code": "600519",
          "name": "股票中文名称",
          "logic": "该标的在板块内的定位与受益点，40 字以内"
        }}
      ]
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


def _is_bullish(direction: str, logic: str = "") -> bool:
    """判断板块方向是否为看多。

    prompt 已要求只输出看多，这里是机器兜底——模型偶尔仍会带出承压方向。
    先看 direction 字段，命中看空词直接否决；字段为空时退回到逻辑描述里找线索，
    两边都没有明确信号时按看多放行（prompt 的约束是只输出看多）。
    """
    text = f"{direction} {logic}".lower()
    if any(token in text for token in _BEARISH_TOKENS):
        return False
    # 没有看空信号时放行：prompt 已要求只输出看多，缺少显式标注不作为否决理由。
    return True


def _normalize_sector_groups(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 LLM 输出归一成板块分组结构，兼容旧的扁平 picks 格式。"""
    sectors = parsed.get("sectors")
    if isinstance(sectors, list) and sectors:
        return [item for item in sectors if isinstance(item, dict)]
    # 兼容早期格式：扁平 picks 列表，按 sector 字段就地分组
    flat = parsed.get("picks")
    if not isinstance(flat, list):
        return []
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in flat:
        if not isinstance(item, dict):
            continue
        name = str(item.get("sector", "")).strip() or "综合"
        bucket = grouped.setdefault(
            name,
            {
                "sector": name,
                "direction": str(item.get("direction", "")).strip(),
                "linkage": str(item.get("linkage", "")).strip(),
                "logic": "",
                "picks": [],
            },
        )
        bucket["picks"].append(item)
    return list(grouped.values())


def _validate_picks(
    sector_groups: List[Dict[str, Any]],
    sector_count: int,
    picks_per_sector: int = PICKS_PER_SECTOR,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """校验板块分组推荐，返回 (有效分组, 丢弃说明)。

    LLM 编造代码或记错名称都很常见，这里以本地股票索引为准：
    代码查不到就丢弃，名称不一致就用索引名覆盖。
    看空方向的板块整组丢弃——推荐列表只保留看多。
    """
    from src.data.stock_index_loader import get_index_stock_name

    valid_groups: List[Dict[str, Any]] = []
    dropped: List[str] = []
    seen: set = set()

    for group in sector_groups:
        sector_name = str(group.get("sector", "")).strip() or "-"
        direction = str(group.get("direction", "")).strip()
        sector_logic = str(group.get("logic", "")).strip()

        if not _is_bullish(direction, sector_logic):
            dropped.append(f"{sector_name}（非看多方向：{direction or sector_logic[:16]}）")
            continue

        raw_picks = group.get("picks")
        if not isinstance(raw_picks, list):
            continue

        members: List[Dict[str, str]] = []
        for item in raw_picks:
            if not isinstance(item, dict):
                continue
            code = re.sub(r"[^0-9]", "", str(item.get("code", "")).strip())
            if len(code) != 6:
                dropped.append(f"{item.get('code')}（代码格式非 6 位数字）")
                continue
            if code in seen:
                continue
            if not code.startswith(
                ("600", "601", "603", "605", "688", "000", "001", "002", "300")
            ):
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
            members.append(
                {
                    "code": code,
                    "name": index_name,
                    "logic": str(item.get("logic", "")).strip() or "-",
                }
            )
            if len(members) >= picks_per_sector:
                break

        if not members:
            continue

        valid_groups.append(
            {
                "sector": sector_name,
                "direction": direction or "看多",
                "linkage": str(group.get("linkage", "")).strip() or "-",
                "logic": sector_logic or "-",
                "picks": members,
            }
        )
        if len(valid_groups) >= sector_count:
            break

    return valid_groups, dropped


def _attach_prev_change(groups: List[Dict[str, Any]]) -> str:
    """给每只标的补上最近一个交易日的涨跌幅，返回该交易日的日期字符串。

    用新浪的批量行情接口一次取回全部代码，而不是逐只走
    ``DataFetcherManager.get_daily_data``。后者在 CI 环境下首选东财，实测每只
    要等约 9 秒 ProtocolError 超时才熔断，接着切到 yfinance 用 ``.SS`` 后缀查
    A 股同样失败，12 只标的耗掉两分钟最后仍然全空。新浪批量接口是项目里既有
    的实时行情兜底源，一次请求返回所有代码，在 CI 中稳定可用。

    任务在北京时间 05:30 运行，A 股尚未开盘，此时接口返回的最新价就是上一
    交易日收盘价，算出来的正是上一交易日涨幅。返回值里的日期字段用来把口径
    显式标到表头，这样即使任务被延迟到盘中执行也不会产生"昨日/当日"的歧义。

    取不到数据时留空并继续——涨跌幅是辅助信息，不该阻断推送。
    """
    codes = [pick["code"] for group in groups for pick in group["picks"]]
    for group in groups:
        for pick in group["picks"]:
            pick["prev_change"] = ""
    if not codes:
        return ""

    try:
        import requests

        from data_provider.akshare_fetcher import (
            SINA_REALTIME_ENDPOINT,
            _to_sina_tx_symbol,
        )
    except Exception as exc:
        logger.warning("行情依赖导入失败，跳过昨日涨幅: %s", exc)
        return ""

    symbol_to_code = {}
    for code in codes:
        try:
            symbol_to_code[_to_sina_tx_symbol(code)] = code
        except Exception:
            continue
    if not symbol_to_code:
        return ""

    url = f"http://{SINA_REALTIME_ENDPOINT}={','.join(symbol_to_code)}"
    try:
        response = requests.get(
            url,
            headers={"Referer": "http://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        response.encoding = "gbk"
        raw = response.text
    except Exception as exc:
        logger.warning("[昨日涨幅] 新浪批量行情请求失败: %s", exc)
        return ""

    # 返回形如 var hq_str_sh600519="名称,今开,昨收,最新价,...,日期,时间";
    changes: Dict[str, str] = {}
    trade_date = ""
    for line in raw.splitlines():
        match = re.search(r'hq_str_([a-z]{2}\d{6})="([^"]*)"', line)
        if not match:
            continue
        code = symbol_to_code.get(match.group(1))
        fields = match.group(2).split(",")
        if not code or len(fields) < 32:
            continue
        try:
            # 字段顺序见 akshare_fetcher._get_stock_realtime_quote_sina：
            # 2=昨收 3=最新价 30=日期
            pre_close = float(fields[2])
            price = float(fields[3])
        except (TypeError, ValueError):
            continue
        if pre_close <= 0 or price <= 0:
            continue
        changes[code] = f"{(price - pre_close) / pre_close * 100:+.2f}%"
        if not trade_date:
            trade_date = fields[30].strip()

    if not changes:
        logger.warning("[昨日涨幅] 新浪返回中没有可用行情，共请求 %s 只", len(symbol_to_code))
        return ""

    for group in groups:
        for pick in group["picks"]:
            pick["prev_change"] = changes.get(pick["code"], "")

    logger.info(
        "[昨日涨幅] 取到 %s/%s 只，基准交易日 %s", len(changes), len(codes), trade_date or "未知"
    )
    return trade_date


def _render_report(
    us_review: str,
    us_summary: str,
    groups: List[Dict[str, Any]],
    dropped: List[str],
    model_name: str,
    capital_flow: str = "",
    trade_date: str = "",
) -> str:
    """拼装推送正文。美股复盘在前，按板块分组的 A 股推荐在后。"""
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
    total_picks = sum(len(group["picks"]) for group in groups)
    lines.append(f"## 🇨🇳 关联 A 股推荐（{len(groups)} 个板块 / {total_picks} 只 · 仅看多）")
    lines.append("")
    if groups:
        change_header = f"{trade_date} 涨幅" if trade_date else "上一交易日涨幅"
        for group in groups:
            lines.append(f"### 📈 {group['sector']}")
            lines.append("")
            lines.append(f"- **对标美股**: {group['linkage']}")
            lines.append(f"- **传导逻辑**: {group['logic']}")
            lines.append("")
            lines.append(f"| 代码 | 名称 | {change_header} | 板块内定位 |")
            lines.append("|------|------|----------|------------|")
            for pick in group["picks"]:
                change = pick.get("prev_change") or "-"
                lines.append(
                    f"| {pick['code']} | {pick['name']} | {change} | {pick['logic']} |"
                )
            lines.append("")
    else:
        lines.append("本次未产出通过代码校验的推荐标的。")
        lines.append("")
    if dropped:
        lines.append(f"> 已剔除 {len(dropped)} 条候选：{('；'.join(dropped))[:200]}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"⚠️ **免责说明**：A 股推荐由大模型（{model_name}）基于美股复盘推理生成，"
        "属模型观点而非事实，代码已通过本地股票索引校验但基本面未经核实。"
        "涨幅为上一交易日实际行情，仅作参考，不代表后续走势。"
        "列表只保留看多方向，不代表其余板块应做空。"
        "跨市场传导存在时滞与失效可能，本内容不构成投资建议。"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="美股复盘 + 关联 A 股推荐推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不推送通知")
    parser.add_argument(
        "--sectors",
        type=int,
        default=DEFAULT_SECTOR_COUNT,
        help=f"推荐的板块数量，每个板块 {PICKS_PER_SECTOR} 只，默认 {DEFAULT_SECTOR_COUNT}",
    )
    parser.add_argument(
        "--picks",
        type=int,
        default=None,
        help=(
            "兼容旧参数：按总只数换算板块数（总数 // "
            f"{PICKS_PER_SECTOR}）。同时给出 --sectors 时以 --sectors 为准"
        ),
    )
    parser.add_argument(
        "--force-run", action="store_true", help="跳过美股交易日检查，强制执行"
    )
    args = parser.parse_args()
    # --picks 是改版前的参数（总只数），workflow 里可能还在传，这里换算成板块数。
    if args.picks is not None and args.sectors == DEFAULT_SECTOR_COUNT:
        sector_count = max(1, min(10, int(args.picks) // PICKS_PER_SECTOR))
    else:
        sector_count = max(1, min(10, int(args.sectors)))

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
    logger.info(
        "请求 LLM 生成 %s 个板块 × %s 只关联 A 股（仅看多）...", sector_count, PICKS_PER_SECTOR
    )
    raw = analyzer.generate_text(
        _build_prompt(us_review, sector_count, capital_flow),
        max_tokens=4096,
        temperature=getattr(config, "llm_temperature", 0.7),
    )
    if not raw:
        logger.error("LLM 未返回内容，终止")
        return 3

    parsed = _extract_json(raw)
    if not parsed:
        logger.error("LLM 输出无法解析为预期 JSON：%s", (raw or "")[:300])
        return 4

    sector_groups = _normalize_sector_groups(parsed)
    if not sector_groups:
        logger.error("LLM 输出中没有可用的板块分组：%s", (raw or "")[:300])
        return 4

    groups, dropped = _validate_picks(sector_groups, sector_count)
    total_picks = sum(len(group["picks"]) for group in groups)
    logger.info(
        "校验通过 %s 个板块 / %s 只标的，剔除 %s 条", len(groups), total_picks, len(dropped)
    )
    for note in dropped:
        logger.warning("剔除候选：%s", note)
    if not groups:
        logger.error("没有任何板块通过校验，终止")
        return 4

    # --- 3.5 补上一交易日涨幅 ---
    trade_date = _attach_prev_change(groups)
    logger.info("昨日涨幅基准交易日：%s", trade_date or "未取到")

    # --- 4. 组装并推送 ---
    report = _render_report(
        us_review=us_review,
        us_summary=str(parsed.get("us_summary", "")).strip(),
        groups=groups,
        dropped=dropped,
        model_name=getattr(config, "litellm_model", "") or "LLM",
        capital_flow=capital_flow,
        trade_date=trade_date,
    )

    if args.dry_run:
        print(report)
        logger.info("dry-run 模式，未推送")
        return 0

    sent = notifier.send(report, route_type="report")
    if not sent:
        logger.error("所有通知渠道均推送失败")
        return 5
    logger.info(
        "推送成功：美股复盘 + %s 个板块 / %s 只 A 股推荐", len(groups), total_picks
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
