"""大盘复盘的「资金动向」段落构造。

为什么分市场实现
----------------
龙虎榜（异动个股的营业部买卖席位公开）是 A 股特有的信息披露制度，
**美股没有对应机制**（美股可比的只有季度 13F、Form 4 内部人交易等，
披露频率与口径完全不同，无法当日使用）。因此：

- ``cn``：使用真实龙虎榜数据（当日上榜明细 + 近 5 日机构席位净额）；
- ``us``：改用「板块 ETF + 龙头个股」的当日资金流向作为等价观察维度，
  这是对 A 股产业链最有映射价值的粒度，段落标题也明确不叫龙虎榜。

数据源选择
----------
A 股龙虎榜优先走**新浪**接口。东财系接口（``stock_lhb_detail_em`` 等）在
云端 / CI 环境失败率极高（``RemoteDisconnected``），而新浪的
``stock_lhb_detail_daily_sina`` / ``stock_lhb_jgzz_sina`` 稳定可用。
美股走 yfinance，与项目既有美股数据源一致。

所有函数 fail-open：取不到数据返回空字符串，复盘照常生成，
不会因为这一段拿不到而影响主链路。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 新浪龙虎榜金额单位为万元
_WAN = 10_000.0
_YI = 100_000_000.0

# 往前回溯的最大自然日数：覆盖周末与连续假期
_MAX_LOOKBACK_DAYS = 7

# 榜单展示条数
_TOP_N = 8

# 美股观察标的：板块 ETF（SPDR Select Sector）+ 权重龙头。
# 选 ETF 而非个股汇总，是为了直接得到「资金在哪个板块」这一层信息。
US_SECTOR_ETFS: Dict[str, str] = {
    "XLK": "科技",
    "XLF": "金融",
    "XLE": "能源",
    "XLV": "医疗保健",
    "XLI": "工业",
    "XLY": "可选消费",
    "XLP": "必需消费",
    "XLU": "公用事业",
    "XLB": "原材料",
    "XLRE": "房地产",
    "SMH": "半导体",
}

US_LEADERS: Dict[str, str] = {
    "NVDA": "英伟达",
    "AAPL": "苹果",
    "MSFT": "微软",
    "GOOGL": "谷歌",
    "AMZN": "亚马逊",
    "META": "Meta",
    "TSLA": "特斯拉",
    "AVGO": "博通",
    "TSM": "台积电",
}


def _fmt_yi(value_wan: float) -> str:
    """万元 -> 亿元字符串。"""
    return f"{value_wan * _WAN / _YI:.2f}亿"


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fetch_cn_daily_lhb() -> Tuple[Optional[Any], Optional[str]]:
    """取最近一个有龙虎榜数据的交易日明细，返回 (DataFrame, 日期)。

    龙虎榜按交易日发布，且当日盘后才有数据，因此需要向前回溯。
    """
    import akshare as ak

    probe = datetime.now()
    for _ in range(_MAX_LOOKBACK_DAYS):
        if probe.weekday() < 5:
            date_str = probe.strftime("%Y%m%d")
            try:
                df = ak.stock_lhb_detail_daily_sina(date=date_str)
                if df is not None and not df.empty:
                    return df, date_str
            except Exception as exc:
                logger.debug("[龙虎榜] %s 拉取失败: %s", date_str, exc)
        probe -= timedelta(days=1)
    return None, None


def _fetch_cn_institution_flow() -> Optional[Any]:
    """近 5 日机构席位追踪（含净额）。"""
    import akshare as ak

    try:
        df = ak.stock_lhb_jgzz_sina(symbol="5")
        if df is not None and not df.empty:
            return df
    except Exception as exc:
        logger.debug("[龙虎榜] 机构席位追踪拉取失败: %s", exc)
    return None


def _build_cn_section(review_language: str) -> str:
    """A 股龙虎榜整体分析段落。"""
    detail_df, trade_date = _fetch_cn_daily_lhb()
    institution_df = _fetch_cn_institution_flow()
    if detail_df is None and institution_df is None:
        logger.info("[龙虎榜] 无可用数据，跳过该段落")
        return ""

    lines: List[str] = []
    header = "## Dragon-Tiger List (A-share disclosure)" if review_language == "en" else "## 龙虎榜资金动向"
    lines.append(header)

    # --- 当日上榜概况与上榜原因分布 ---
    if detail_df is not None:
        try:
            total = len(detail_df)
            reason_counts: Dict[str, int] = {}
            if "指标" in detail_df.columns:
                # 不能用 Series.astype(str)：object dtype 里的 NaN 不会被强制转成
                # 字符串（实测元素类型集合为 {'str', 'float'}），后续 .strip() 会抛
                # AttributeError。这里显式用 str() 转换并归一化空值。
                for reason in detail_df["指标"].tolist():
                    key = str(reason).strip()
                    if not key or key.lower() in ("nan", "none"):
                        key = "未标注"
                    reason_counts[key] = reason_counts.get(key, 0) + 1
            unique_codes = (
                detail_df["股票代码"].astype(str).nunique()
                if "股票代码" in detail_df.columns
                else total
            )
            lines.append(
                f"- 上榜日期: {trade_date or '-'}；上榜记录 {total} 条，涉及个股 {unique_codes} 只"
            )
            if reason_counts:
                ranked = sorted(reason_counts.items(), key=lambda kv: -kv[1])[:6]
                detail = "；".join(f"{name} {count}条" for name, count in ranked)
                lines.append(f"- 上榜原因分布: {detail}")

            # 成交额最大的上榜个股：反映当日资金最集中的异动标的
            if {"成交额", "股票名称", "股票代码"}.issubset(detail_df.columns):
                work = detail_df.copy()
                work["_amount"] = work["成交额"].map(_safe_float)
                work = work.dropna(subset=["_amount"]).sort_values("_amount", ascending=False)
                top_rows = work.drop_duplicates(subset=["股票代码"]).head(_TOP_N)
                if not top_rows.empty:
                    items = "；".join(
                        f"{row['股票名称']}({row['股票代码']}) {_fmt_yi(row['_amount'])}"
                        for _, row in top_rows.iterrows()
                    )
                    lines.append(f"- 上榜成交额居前: {items}")
        except Exception as exc:
            logger.debug("[龙虎榜] 明细汇总失败: %s", exc)

    # --- 近 5 日机构席位净额 ---
    if institution_df is not None:
        try:
            work = institution_df.copy()
            work["_net"] = work["净额"].map(_safe_float) if "净额" in work.columns else None
            work = work.dropna(subset=["_net"])
            if not work.empty:
                net_total = work["_net"].sum()
                buy_side = work[work["_net"] > 0].sort_values("_net", ascending=False).head(_TOP_N)
                sell_side = work[work["_net"] < 0].sort_values("_net").head(_TOP_N)
                lines.append(
                    f"- 近5日机构席位合计净额: {_fmt_yi(net_total)}"
                    f"（净买入 {len(work[work['_net'] > 0])} 只 / 净卖出 {len(work[work['_net'] < 0])} 只）"
                )
                if not buy_side.empty:
                    items = "；".join(
                        f"{row['股票名称']}({row['股票代码']}) +{_fmt_yi(row['_net'])}"
                        for _, row in buy_side.iterrows()
                    )
                    lines.append(f"- 机构席位净买入居前: {items}")
                if not sell_side.empty:
                    items = "；".join(
                        f"{row['股票名称']}({row['股票代码']}) {_fmt_yi(row['_net'])}"
                        for _, row in sell_side.iterrows()
                    )
                    lines.append(f"- 机构席位净卖出居前: {items}")
        except Exception as exc:
            logger.debug("[龙虎榜] 机构席位汇总失败: %s", exc)

    if len(lines) <= 1:
        return ""
    lines.append(
        "- 数据来源: 新浪财经龙虎榜（上榜明细为最近交易日，机构席位为近5日累计）"
    )
    return "\n".join(lines)


def _fetch_us_changes(symbols: List[str]) -> Dict[str, float]:
    """用 yfinance 批量取最近两个交易日收盘价，算当日涨跌幅（%）。"""
    import yfinance as yf

    result: Dict[str, float] = {}
    try:
        data = yf.download(
            " ".join(symbols),
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        if data is None or data.empty:
            return result
        closes = data["Close"] if "Close" in data else None
        if closes is None:
            return result
        closes = closes.dropna(how="all")
        if len(closes) < 2:
            return result
        latest = closes.iloc[-1]
        previous = closes.iloc[-2]
        for symbol in symbols:
            try:
                new_value = float(latest[symbol])
                old_value = float(previous[symbol])
                if old_value:
                    result[symbol] = (new_value - old_value) / old_value * 100.0
            except (KeyError, TypeError, ValueError):
                continue
    except Exception as exc:
        logger.debug("[美股资金动向] yfinance 拉取失败: %s", exc)
    return result


def _build_us_section(review_language: str) -> str:
    """美股板块与龙头资金动向段落（美股无龙虎榜，此为等价观察维度）。"""
    changes = _fetch_us_changes(list(US_SECTOR_ETFS) + list(US_LEADERS))
    if not changes:
        logger.info("[美股资金动向] 无可用数据，跳过该段落")
        return ""

    sector_items = [
        (US_SECTOR_ETFS[sym], sym, pct) for sym, pct in changes.items() if sym in US_SECTOR_ETFS
    ]
    leader_items = [
        (US_LEADERS[sym], sym, pct) for sym, pct in changes.items() if sym in US_LEADERS
    ]
    if not sector_items and not leader_items:
        return ""

    header = (
        "## Sector & Mega-cap Money Flow (US has no Dragon-Tiger disclosure)"
        if review_language == "en"
        else "## 板块与龙头资金动向"
    )
    lines: List[str] = [header]
    lines.append(
        "- 说明: 美股无龙虎榜披露制度，此处以板块 ETF 与权重龙头的当日表现替代观察资金流向"
    )

    if sector_items:
        sector_items.sort(key=lambda item: -item[2])
        strongest = "；".join(f"{name}({sym}) {pct:+.2f}%" for name, sym, pct in sector_items[:5])
        weakest = "；".join(f"{name}({sym}) {pct:+.2f}%" for name, sym, pct in sector_items[-5:])
        lines.append(f"- 板块领涨: {strongest}")
        lines.append(f"- 板块领跌: {weakest}")
        spread = sector_items[0][2] - sector_items[-1][2]
        lines.append(f"- 板块首尾分化度: {spread:.2f} 个百分点")

    if leader_items:
        leader_items.sort(key=lambda item: -item[2])
        items = "；".join(f"{name}({sym}) {pct:+.2f}%" for name, sym, pct in leader_items)
        lines.append(f"- 权重龙头表现: {items}")

    lines.append("- 数据来源: yfinance 日线收盘价对比（SPDR 行业 ETF 与美股权重龙头）")
    return "\n".join(lines)


def build_capital_flow_block(region: str, review_language: str = "zh") -> str:
    """构造资金动向段落，供大盘复盘 prompt 使用。

    Args:
        region: 市场区域，``cn`` 走龙虎榜，``us`` 走板块/龙头动向，其余返回空串
        review_language: ``zh`` 或 ``en``，仅影响段落标题

    Returns:
        Markdown 段落文本；无数据或异常时返回空字符串（fail-open）
    """
    try:
        normalized = (region or "").strip().lower()
        if normalized == "cn":
            return _build_cn_section(review_language)
        if normalized == "us":
            return _build_us_section(review_language)
        return ""
    except Exception as exc:  # pragma: no cover - 该段落不得影响主复盘链路
        logger.warning("[资金动向] 段落构造失败（已跳过）: %s", exc)
        return ""


def build_us_capital_flow_context() -> str:
    """给美股→A股推荐单独使用的资金动向上下文。

    与 ``build_capital_flow_block('us')`` 同源，但不带 Markdown 小标题，
    便于嵌入推荐 prompt。
    """
    block = _build_us_section("zh")
    if not block:
        return ""
    return "\n".join(line for line in block.splitlines() if not line.startswith("## "))
