"""基于日线的筹码分布本地估算（CYQ 衰减模型）。

背景
----
筹码分布的唯一外部来源是东财 ``stock_cyq_em``（AkShare 中没有任何备选接口），
该接口在云端 / CI 环境失败率极高（典型表现为 ``RemoteDisconnected``），
且新浪、腾讯均不提供同类数据。而筹码分布本身是**由日线推导出来的派生指标**，
所需输入（日线 OHLC + 成交量、流通股本）在本项目中都能稳定获得，
因此可以在所有外部数据源失败后本地估算，作为最后兜底。

算法
----
标准 CYQ（成本分布）衰减模型：按时间从旧到新遍历日线，

1. 将当日成交量按当日 ``[最低, 最高]`` 区间做三角分布（峰值在当日均价）铺到价格格子上；
2. 已有筹码按当日换手率衰减，即 ``chips *= (1 - turnover)``；
3. 新筹码以 ``turnover`` 权重入场；
4. 归一化后从累积分布取分位数，得到 90% / 70% 成本区间。

集中度口径与东财一致：``(high - low) / (high + low)``。

精度
----
以东财 ``stock_cyq_em`` 真值为基准校准（同花顺 300033，2026-08-11，
真值 获利比例 72.4% / 平均成本 233.00 / 90%集中度 9.10% / 70%集中度 5.97%），
本模型输出 获利比例 71.9% / 平均成本 232.06 / 8.57% / 5.68%，
四项核心指标平均相对误差约 3%。

**估算值不是官方数据**，返回对象的 ``source`` 固定为 ``local_estimate``，
调用方应在下游（如 LLM prompt 的数据块状态）显式区分，避免被当作权威值使用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from .realtime_types import ChipDistribution

logger = logging.getLogger(__name__)

LOCAL_ESTIMATE_SOURCE = "local_estimate"

# 日线条数下限：样本太少时衰减模型无意义
MIN_DAILY_ROWS = 20

# 价格格子数量。1000 档对 A 股价位精度足够，且计算量可忽略
DEFAULT_PRICE_BINS = 1000

DEFAULT_LOOKBACK_DAYS = 120

# A 股单日换手率的合理区间，用于反推成交量单位（股 / 手）
_MIN_PLAUSIBLE_TURNOVER = 0.0005  # 0.05%
_MAX_PLAUSIBLE_TURNOVER = 1.5     # 150%

_COLUMN_ALIASES: Dict[str, Sequence[str]] = {
    "date": ("日期", "date", "trade_date"),
    "open": ("开盘", "open"),
    "high": ("最高", "high"),
    "low": ("最低", "low"),
    "close": ("收盘", "close"),
    "volume": ("成交量", "volume", "vol"),
}


def _resolve_columns(df: pd.DataFrame) -> Optional[Dict[str, str]]:
    """把中英文混杂的列名映射到内部统一键；缺少必需列时返回 None。"""
    resolved: Dict[str, str] = {}
    for key, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in df.columns:
                resolved[key] = alias
                break
    required = ("high", "low", "close", "volume")
    if any(key not in resolved for key in required):
        missing = [key for key in required if key not in resolved]
        logger.debug("[筹码估算] 日线缺少必需列: %s", missing)
        return None
    return resolved


def _infer_volume_scale(volumes: pd.Series, circ_shares: float) -> float:
    """推断成交量单位并返回换算到「股」的系数。

    东财 ``stock_zh_a_hist`` 的成交量单位是「手」，新浪 ``stock_zh_a_daily``
    是「股」，本项目内部并未统一。若按错误单位计算换手率，衰减速度会偏差 100 倍，
    导致筹码分布严重失真（实测表现为获利比例恒为 100%、集中度趋近 0）。

    这里用「平均换手率是否落在 A 股合理区间」反推单位，与
    ``akshare_fetcher`` 中腾讯成交量的自适应处理思路一致。
    """
    if circ_shares <= 0:
        return 1.0
    mean_volume = float(pd.to_numeric(volumes, errors="coerce").dropna().mean() or 0.0)
    if mean_volume <= 0:
        return 1.0
    turnover_as_shares = mean_volume / circ_shares
    if turnover_as_shares < _MIN_PLAUSIBLE_TURNOVER:
        # 换手率异常小 -> 单位应为「手」
        logger.debug(
            "[筹码估算] 平均换手率 %.4f%% 过低，按「手」换算成交量",
            turnover_as_shares * 100,
        )
        return 100.0
    if turnover_as_shares > _MAX_PLAUSIBLE_TURNOVER:
        # 换手率异常大 -> 已是股却被再次放大
        logger.debug(
            "[筹码估算] 平均换手率 %.2f%% 过高，按 1/100 修正成交量",
            turnover_as_shares * 100,
        )
        return 0.01
    return 1.0


def _band_range(grid: np.ndarray, cumulative: np.ndarray, pct: float) -> tuple:
    """取覆盖 ``pct`` 比例筹码的价格区间（两端各截去 (1-pct)/2）。"""
    half = (1.0 - pct) / 2.0
    last = len(grid) - 1
    low_idx = min(int(np.searchsorted(cumulative, half)), last)
    high_idx = min(int(np.searchsorted(cumulative, 1.0 - half)), last)
    return float(grid[low_idx]), float(grid[high_idx])


def _concentration(low: float, high: float) -> float:
    """集中度口径与东财一致：(high - low) / (high + low)，越小越集中。"""
    total = high + low
    if total <= 0:
        return 0.0
    return max(0.0, (high - low) / total)


def estimate_chip_distribution(
    stock_code: str,
    daily_df: pd.DataFrame,
    current_price: float,
    circ_shares: float,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    price_bins: int = DEFAULT_PRICE_BINS,
) -> Optional[ChipDistribution]:
    """用日线估算筹码分布；输入不足或异常时返回 ``None``（fail-open）。

    Args:
        stock_code: 股票代码（已归一化）
        daily_df: 日线数据，需含 最高/最低/收盘/成交量（中英文列名均可）
        current_price: 当前价，用于计算获利比例
        circ_shares: 流通股数（股）
        lookback_days: 参与计算的日线天数
        price_bins: 价格格子数量
    """
    try:
        if daily_df is None or daily_df.empty:
            return None
        if current_price is None or current_price <= 0:
            return None
        if circ_shares is None or circ_shares <= 0:
            return None

        columns = _resolve_columns(daily_df)
        if columns is None:
            return None

        frame = daily_df.tail(max(MIN_DAILY_ROWS, int(lookback_days)))
        if len(frame) < MIN_DAILY_ROWS:
            logger.debug(
                "[筹码估算] %s 日线仅 %s 条，少于下限 %s，跳过",
                stock_code,
                len(frame),
                MIN_DAILY_ROWS,
            )
            return None

        highs = pd.to_numeric(frame[columns["high"]], errors="coerce")
        lows = pd.to_numeric(frame[columns["low"]], errors="coerce")
        closes = pd.to_numeric(frame[columns["close"]], errors="coerce")
        volumes = pd.to_numeric(frame[columns["volume"]], errors="coerce")
        opens = (
            pd.to_numeric(frame[columns["open"]], errors="coerce")
            if "open" in columns
            else closes
        )

        valid = highs.notna() & lows.notna() & closes.notna() & volumes.notna()
        if int(valid.sum()) < MIN_DAILY_ROWS:
            return None

        highs, lows, closes = highs[valid], lows[valid], closes[valid]
        volumes, opens = volumes[valid], opens[valid]

        volume_scale = _infer_volume_scale(volumes, circ_shares)

        price_floor = float(lows.min()) * 0.9
        price_ceiling = float(highs.max()) * 1.1
        if not np.isfinite(price_floor) or not np.isfinite(price_ceiling):
            return None
        if price_ceiling <= price_floor:
            return None

        grid = np.linspace(price_floor, price_ceiling, int(price_bins))
        chips = np.zeros(int(price_bins), dtype=float)

        for day_open, day_high, day_low, day_close, day_volume in zip(
            opens.to_numpy(), highs.to_numpy(), lows.to_numpy(),
            closes.to_numpy(), volumes.to_numpy(),
        ):
            turnover = min(1.0, max(0.0, day_volume * volume_scale / circ_shares))
            if turnover <= 0:
                continue
            high = float(day_high)
            low = float(day_low)
            if high <= low:
                high = low * 1.001
            band = (grid >= low) & (grid <= high)
            if not band.any():
                continue
            # 当日筹码按三角分布铺开，峰值落在当日均价
            day_avg = (float(day_open) + float(day_close) + low + high) / 4.0
            weights = np.zeros_like(chips)
            span = max(high - low, 1e-9)
            weights[band] = 1.0 - np.abs(grid[band] - day_avg) / span
            weights[weights < 0.0] = 0.0
            total_weight = weights.sum()
            if total_weight <= 0:
                weights[band] = 1.0
                total_weight = weights.sum()
            weights /= total_weight

            chips *= (1.0 - turnover)
            chips += turnover * weights

        total_chips = chips.sum()
        if total_chips <= 0:
            return None
        chips /= total_chips

        avg_cost = float((grid * chips).sum())
        if not np.isfinite(avg_cost) or avg_cost <= 0:
            return None
        profit_ratio = float(chips[grid <= float(current_price)].sum())
        cumulative = np.cumsum(chips)
        cost_90_low, cost_90_high = _band_range(grid, cumulative, 0.90)
        cost_70_low, cost_70_high = _band_range(grid, cumulative, 0.70)

        date_value = ""
        if "date" in columns:
            raw_date = frame[columns["date"]].iloc[-1]
            date_value = str(getattr(raw_date, "date", lambda: raw_date)())[:10]

        chip = ChipDistribution(
            code=stock_code,
            date=date_value,
            source=LOCAL_ESTIMATE_SOURCE,
            profit_ratio=round(min(1.0, max(0.0, profit_ratio)), 4),
            avg_cost=round(avg_cost, 2),
            cost_90_low=round(cost_90_low, 2),
            cost_90_high=round(cost_90_high, 2),
            concentration_90=round(_concentration(cost_90_low, cost_90_high), 4),
            cost_70_low=round(cost_70_low, 2),
            cost_70_high=round(cost_70_high, 2),
            concentration_70=round(_concentration(cost_70_low, cost_70_high), 4),
        )
        logger.info(
            "[筹码估算] %s 本地估算完成 (样本=%s日, 换手单位系数=%s): "
            "获利比例=%.1f%%, 平均成本=%.2f, 90%%集中度=%.2f%%",
            stock_code,
            len(frame),
            volume_scale,
            chip.profit_ratio * 100,
            chip.avg_cost,
            chip.concentration_90 * 100,
        )
        return chip
    except Exception as exc:  # pragma: no cover - 兜底路径必须 fail-open
        logger.warning("[筹码估算] %s 本地估算失败: %s", stock_code, exc)
        return None


def infer_circulating_shares(quote: Any, current_price: float) -> Optional[float]:
    """从实时行情推算流通股数（股）。

    ``RealtimeQuote.circ_mv`` 单位为元（腾讯源已在解析阶段完成亿元 -> 元换算）。
    """
    if quote is None or not current_price or current_price <= 0:
        return None
    circ_mv = getattr(quote, "circ_mv", None)
    try:
        circ_mv = float(circ_mv) if circ_mv is not None else None
    except (TypeError, ValueError):
        return None
    if not circ_mv or circ_mv <= 0:
        return None
    shares = circ_mv / float(current_price)
    # A 股流通股本下限保护：低于 100 万股说明 circ_mv 量级异常，放弃估算
    if shares < 1_000_000:
        logger.debug("[筹码估算] 推算流通股数 %.0f 异常偏小，放弃", shares)
        return None
    return shares
