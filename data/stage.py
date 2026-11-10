"""
data/stage.py — Weinstein 四阶段判定（Stage 1/2/3/4）+ A/B 子阶段

判据**完全移植**自参考项目的生产引擎（不是照着文档重写）：

    /Users/wubo/Desktop/codes/Stan Weinstein stage analysis/weinstein/classical.py
    规则版本：WEINSTEIN-STRUCTURE-1.3

参考实现跑在**周线**上，所以本模块先把日线重采样成周线，再跑同一套状态机：

    MA30（30 周均线）+ 近 4 周变化率（±0.30% 视为走平）
    前 26 周区间支撑/阻力（不含本周）
    最近 4 周 vs 此前 4 周的更高/更低高点、更低低点
    前序阶段上下文（Stage 1 只有突破自身底部阻力才进 Stage 2，只有跌破自身底部支撑才回 Stage 4）

主阶段**只用结构**，不用 13 周动量分数、不用连续三周确认、不用固定八周最短阶段、
不用严重百分比自动跳级 —— 这些是参考项目里已废弃的旧引擎（analysis.py）的做法。

A/B 子阶段是 Weinstein Global Trend Alert 的映射（1A/1/1B、2A/2/2B、3A/3/3B、4A/4/4B/4B-），
数值阈值是工程测量代理，不是原书公式。

参考实现硬性要求至少 60 根周线（约 1.2 年日线），不够就判不了（stage=None），不猜。

判定用的是**当前**状态（最新一根周线），所以能回答「这条信号现在还在不在 Stage 2」。
信号触发时必然是 Stage 2（那是入场条件本身），所以这一列的价值全在**事后跟踪**。

「本周尚未收完」（发展中）时的呈现规则：只有这一周**把阶段翻掉了**（与上一完整周的
主阶段不同）才在报告里标「待定」并写出上一完整周的读数；没翻掉就不标 ——
否则整张表每行都挂一个没信息量的提示，半个交易日的价格波动就能报一个假预警。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from data.daily_bars import dedup_daily_bars

__all__ = [
    "StageResult",
    "compute_stage",
    "LABELS",
    "STAGE_NAMES",
    "SUBSTAGE_NAMES",
    "CLASSICAL_RULES_VERSION",
]

# ---- 参考实现 classical.py / analysis.py 的常量 ----
CLASSICAL_RULES_VERSION = "WEINSTEIN-STRUCTURE-1.3"
MA_WINDOW = 30                 # 30 周简单移动平均线
SLOPE_LOOKBACK = 4             # 均线斜率的回看周数
FLAT_SLOPE = 0.003             # ±0.30% 视为走平（工程测量参考）
RANGE_LOOKBACK = 26            # 区间结构回看周数（不含本周）
MIN_WEEKLY_BARS = 60           # 参考实现硬性要求
VALID_OFFSET = MA_WINDOW - 1 + SLOPE_LOOKBACK   # 33：前 33 根算不出斜率

STAGE_NAMES = {1: "筑底期", 2: "上升期", 3: "顶部期", 4: "下跌期"}
SUBSTAGE_NAMES = {
    "1A": "筑底初期", "1": "筑底期", "1B": "筑底后期",
    "2A": "上升初期", "2": "上升期", "2B": "上升后期",
    "3A": "顶部初期", "3": "顶部期", "3B": "顶部后期",
    "4A": "下跌初期", "4": "下跌期", "4B": "下跌后期", "4B-": "周期低点观察",
}
SUBSTAGE_DESCRIPTIONS = {
    "1A": "底部开始形成，仍需要更多时间确认，不宜把初步止跌直接当成完成筑底。",
    "1": "处于底部构建阶段，价格与均线逐步平衡，继续观察区间和突破条件。",
    "1B": "底部构建后期，价格接近区间阻力且动量改善，重点观察是否形成有效突破。",
    "2A": "处于上升趋势早期，通常对应底部突破或首次延续确认；突破质量仍需成交量和相对强度验证。",
    "2": "处于已确立的上升趋势，价格与均线结构保持健康。",
    "2B": "处于上升趋势后期或动能减速区，应关注整理、失败突破和均线走平风险。",
    "3A": "顶部开始形成，上涨结构减弱，但尚未出现严重区间破位。",
    "3": "处于明确的顶部整理区域，价格与均线结构显示上涨已停止持续推进。",
    "3B": "顶部特征日益明显，价格、动量或区间支撑进一步恶化，属于顶部后期/破位过渡。",
    "4A": "已进入下跌趋势初期，顶部支撑和价格结构开始明确失守。",
    "4": "处于已确立的下跌趋势，价格和均线结构持续偏弱。",
    "4B": "处于下跌趋势后期，但后期不等于见底，仍需要底部结构确认。",
    "4B-": "尚未正式进入 Stage 1A，但出现本轮周期低点可能形成的观察信号，不等于反转确认。",
}

LABELS = {stage: f"Stage {stage} {name}" for stage, name in STAGE_NAMES.items()}


@dataclass
class StageResult:
    """一次阶段判定的结果。

    `stage` 为 None 表示**判断不了**（历史不够 / 没行情），不是「不属于任何阶段」。
    """

    stage: int | None
    label: str
    reason: str
    substage: str | None = None
    substage_name: str | None = None
    substage_reason: str | None = None
    close: float | None = None
    ma30: float | None = None
    slope_pct: float | None = None      # MA30 近 4 周变化率（0.005 = 4 周涨 0.5%）
    px_vs_ma: float | None = None       # 收盘价相对 MA30（0.28 = 高出 28%）
    momentum_13w: float | None = None   # 13 周涨幅（参考实现只当辅助信息，不参与阶段判定）
    weeks_in_stage: int | None = None   # 当前主阶段已持续周数
    weekly_bars: int = 0                # 参与判定的周线根数
    developing: bool = False            # 最新一周尚未收完（结论是暂定的）
    confirmed_stage: int | None = None  # 上一根**完整**周线的主阶段
    confirmed_substage: str | None = None
    consistency: int | None = None      # 规则契合度（不是概率）


@dataclass(frozen=True)
class _WeeklyBar:
    """周线 bar。volume_* 三个字段用于「同口径量比」。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_regime: str      # 该周主导的行情口径
    volume_comparable: float  # 只累加同口径日的量
    volume_days: int


# --------------------------------------------------------------------------
# 参考实现 classical.py 的移植（逐行对照，判定语义完全一致）
# --------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _structure_direction(slope: float, distance: float) -> str:
    """只看均线斜率与价格位置的结构方向（不掺动量）。"""
    if slope > FLAT_SLOPE and distance > 0:
        return "up"
    if slope < -FLAT_SLOPE and distance < 0:
        return "down"
    if abs(slope) <= FLAT_SLOPE:
        return "flat"
    return "mixed"


def classify_classical_substage(week: dict) -> tuple[str, str]:
    """结构优先的 A/B 子阶段映射（移植自 classical.classify_classical_substage）。"""
    stage = week["stage"]
    age = week["weeks_in_stage"]
    slope = week["ma30_slope"] or 0
    distance = week["price_vs_ma"] or 0
    slope_delta = week["slope_delta_4w"] or 0
    range_position = week["range_position_26w"]

    if stage == 1:
        if week["breakout_26w"] or (
            age >= 8 and range_position is not None and range_position >= 0.75
            and slope >= -FLAT_SLOPE and week["higher_lows"]
        ):
            return "1B", "底部区间较成熟，价格接近/突破既有阻力且低点改善，符合 Stage 1B。"
        if age <= 4 or slope < -FLAT_SLOPE or week["new_low_13w"]:
            return "1A", "底部仍早期、均线仍下降或价格继续创新低，需要更多时间形成结构。"
        return "1", "已经形成可识别的底部区间，但尚未达到 Stage 1B 的成熟突破条件。"

    if stage == 2:
        if (
            week["lower_highs"]
            or (age >= 16 and slope_delta <= -FLAT_SLOPE)
            or (age >= 26 and week["direction"] in ("flat", "mixed"))
        ):
            return "2B", "上升均线仍未逆转，但出现更低高点、整理或均线增速放缓，符合 Stage 2B 后期观察。"
        if (
            age <= 8 and week["previous_stage"] == 1
            and week["breakout_in_stage"] and not week["continuation_in_stage"]
            and week["direction"] in ("up", "flat", "mixed")
        ):
            return "2A", "本阶段发生真实底部突破，且尚未进入首次延续上涨，属于 Stage 2A 突破/首次回踩区。"
        return "2", "价格仍位于上升均线之上，且已进入或处于延续上涨结构，归为 Stage 2。"

    if stage == 3:
        if (
            week["breakdown_26w"]
            or (slope < -FLAT_SLOPE and distance < 0)
            or (week["lower_highs"] and distance < 0)
            or (week["lower_lows"] and distance < 0)
            or (week["tests_support"] and week["lower_highs"])
        ):
            return "3B", "出现更低高点/更低低点并跌破均线，或测试、跌破顶部支撑，顶部结构持续恶化，符合 Stage 3B。"
        if age <= 4:
            if week.get("stage3_matured"):
                return "3", "本段顶部已经历破位或成熟整理；当前属于 Stage 3 顶部区域恢复，不能倒退为 Stage 3A。"
            return "3A", "上涨后顶部开始形成，但支撑尚未明确失守，属于 Stage 3A。"
        return "3", "已经形成顶部整理结构，尚未出现足够的 Stage 3B 支撑失守证据。"

    if (
        week["direction"] == "up"
        or (distance > 0 and week["higher_lows"])
        or (age >= 4 and week["rebound_from_stage_low"] >= 0.10 and not week["new_low_13w"])
    ):
        return "4B-", "价格从阶段低点反弹或重新站回均线，进入 4B- 低点观察；尚不等于 Stage 1A。"
    if age >= 26 or (age >= 16 and (slope_delta > 0 or week["volume_ratio"] is not None and week["volume_ratio"] < 0.8 or distance <= -0.15)):
        return "4B", "下跌趋势已充分延展并出现降速、缩量或深度偏离，归为 Stage 4B，仍不提前猜底。"
    if age <= 8:
        return "4A", "顶部或底部支撑失守后的下跌初期，符合 Stage 4A。"
    return "4", "下降趋势已经确立，尚未出现充分的后期降速或低点观察证据。"


def _assign_classical_stages(weeks: list[dict], valid_indices: list[int]) -> list[int]:
    """结构状态机（移植自 classical._assign_classical_stages）。

    关键点：Stage 1 只有突破**自身底部阻力**才进 Stage 2，只有跌破**自身底部支撑**
    才回 Stage 4；不是「看到更低低点就换阶段」。
    """
    stages: list[int] = []
    current = None
    base_resistance = base_support = None
    for index in valid_indices:
        week = weeks[index]
        slope = week["ma30_slope"] or 0
        distance = week["price_vs_ma"] or 0
        direction = week["direction"]
        close = week.get("close")
        breakout_base = base_resistance is not None and close is not None and close > base_resistance
        breakdown_base = base_support is not None and close is not None and close < base_support
        week.update({
            "base_resistance": base_resistance, "base_support": base_support,
            "breakout_base": breakout_base, "breakdown_base": breakdown_base,
        })
        if current is None:
            if direction == "up":
                current = 2
            elif direction == "down":
                current = 4
            else:
                current = 3 if (week["change_26w"] or 0) > 0 else 1
        elif current == 1:
            if direction == "up" and breakout_base:
                current = 2
            elif direction == "down" and breakdown_base:
                current = 4
        elif current == 2:
            if direction == "down" and (week["breakdown_26w"] or week["new_low_13w"] or week["lower_lows"]):
                current = 4
            elif distance < 0 and (week["lower_highs"] or week["lower_lows"] or slope <= FLAT_SLOPE):
                current = 3
            elif abs(slope) <= FLAT_SLOPE and week["ma_crosses_13w"] >= 2:
                current = 3
        elif current == 3:
            if direction == "down" and (week["breakdown_26w"] or week["new_low_13w"] or week["lower_lows"]):
                current = 4
            elif direction == "up" and week["breakout_26w"]:
                current = 2
        elif current == 4:
            if direction == "up" and week["breakout_26w"]:
                current = 2
            elif distance > 0 and (week["higher_lows"] or abs(slope) <= FLAT_SLOPE):
                current = 1
            elif abs(slope) <= FLAT_SLOPE and week["higher_lows"]:
                current = 1
        stages.append(current)
        if current == 1:
            high, low = week.get("high"), week.get("low")
            if high is not None:
                base_resistance = high if base_resistance is None else max(base_resistance, high)
            if low is not None:
                base_support = low if base_support is None else min(base_support, low)
        else:
            base_resistance = base_support = None
    return stages


def _fit(week: dict) -> int:
    """规则契合度（不是概率）。移植自 classical._fit。"""
    stage = week["stage"]
    slope = week["ma30_slope"] or 0
    distance = week["price_vs_ma"] or 0
    if stage == 2:
        checks = [slope > FLAT_SLOPE, distance > 0, not week["breakdown_26w"], not week["lower_lows"]]
    elif stage == 4:
        checks = [slope < -FLAT_SLOPE, distance < 0, not week["breakout_26w"], not week["higher_lows"]]
    else:
        checks = [week["previous_stage"] is not None, week["range_position_26w"] is not None,
                  not week["breakdown_26w"], week["direction"] in ("flat", "mixed")]
    return min(95, round(45 + 50 * sum(checks) / 4))


def _description(week: dict, developing: bool) -> str:
    """子阶段说明（移植自 classical._description）。"""
    substage = week["substage"]
    stage = week["stage"]
    slope = week["ma30_slope"] or 0
    distance = week["price_vs_ma"] or 0
    text = SUBSTAGE_DESCRIPTIONS[substage]
    if substage == "3B" and slope > FLAT_SLOPE:
        text += " 当前 30 周均线仍上升，Stage 4A 需要后续下降均线与更低低点结构确认。"
    if stage == 4 and distance > 0:
        text += " 本周价格已反弹至均线上方，属于下跌阶段的反弹/过渡观察，尚未确认反转。"
    elif stage == 3 and distance > 0 and slope > FLAT_SLOPE:
        text += " 本周价格回到上升均线上方，属于顶部区间的反弹或向上过渡观察；未突破既有阻力前不升级为 Stage 2。"
    elif stage == 2 and distance < 0:
        text += " 本周价格位于均线下方，属于上升结构的弱势警告，需要结合区间和高低点继续判断。"
    if developing:
        text += " 最新周线尚未完成，当前子阶段为发展中判断。"
    return text


def is_developing_week(latest_date: str, today: date) -> bool:
    """当前 ISO 周尚未收完 → 结论只是暂定的（移植自 analysis.is_developing_week）。"""
    latest = date.fromisoformat(latest_date)
    if latest.isocalendar()[:2] != today.isocalendar()[:2]:
        return False
    return today.weekday() < 5


# --------------------------------------------------------------------------
# 日线 → 周线
# --------------------------------------------------------------------------


def _resample_weekly(df: pd.DataFrame) -> list[_WeeklyBar]:
    """把日线重采样成周线（周内首开 / 最高 / 最低 / 末收 / 成交量合计）。

    量能口径：日线 pkl 里同一交易日有两根 bar（合并量 + IEX），去重后留下的是
    IEX 那一根，量只有合并量的 1/20~1/50。若直接跨口径比量比，最近几周会假性暴跌，
    触发 Stage 4B 的「缩量」误判。所以每周额外记一个**主导口径**，量比只跟同口径比。
    """
    bars = dedup_daily_bars(df)
    if bars is None or bars.empty or "close" not in bars.columns:
        return []
    if not isinstance(bars.index, pd.DatetimeIndex):
        return []

    frame = bars.copy()
    for column in ("open", "high", "low"):
        if column not in frame.columns:
            frame[column] = frame["close"]
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    frame = frame[["open", "high", "low", "close", "volume"]].astype(float)
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        return []
    frame = frame.sort_index()
    # 04:00 / 05:00 UTC 是 IEX 的收盘 bar（夏令时差一小时），其余是合并量口径
    frame["_regime"] = ["iex" if hour in (4, 5) else "consolidated" for hour in frame.index.hour]

    weekly: list[_WeeklyBar] = []
    for label, chunk in frame.groupby(pd.Grouper(freq="W-FRI")):
        if chunk.empty:
            continue
        counts = chunk["_regime"].value_counts()
        dominant = counts.index[0]
        if len(counts) > 1 and counts.iloc[0] == counts.iloc[1]:
            dominant = chunk["_regime"].iloc[-1]
        same = chunk[chunk["_regime"] == dominant]
        weekly.append(_WeeklyBar(
            date=pd.Timestamp(label).strftime("%Y-%m-%d"),
            open=float(chunk["open"].iloc[0]),
            high=float(chunk["high"].max()),
            low=float(chunk["low"].min()),
            close=float(chunk["close"].iloc[-1]),
            volume=float(chunk["volume"].sum()),
            volume_regime=str(dominant),
            volume_comparable=float(same["volume"].sum()),
            volume_days=int(len(same)),
        ))
    return weekly


def _volume_ratio(bars: list[_WeeklyBar], index: int) -> float | None:
    """本周量 / 前 20 周均量，只跟**同口径**的历史周比（同口径样本不足 4 个则不算）。"""
    current = bars[index]
    if index < 20 or current.volume_days <= 0:
        return None
    peers = [
        bars[j] for j in range(index - 20, index)
        if bars[j].volume_regime == current.volume_regime and bars[j].volume_days > 0
    ]
    if len(peers) < 4:
        return None
    peer_average = _mean([peer.volume_comparable / peer.volume_days for peer in peers])
    if not peer_average:
        return None
    return (current.volume_comparable / current.volume_days) / peer_average


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _analyze_weekly(bars: list[_WeeklyBar], as_of: date | None = None) -> dict:
    """移植 classical.analyze_classical 的核心部分（只留本项目要用的字段）。"""
    closes = [bar.close for bar in bars]
    weeks: list[dict] = []
    valid_indices: list[int] = []

    for index, bar in enumerate(bars):
        ma30 = _mean(closes[index - MA_WINDOW + 1: index + 1]) if index >= MA_WINDOW - 1 else None
        slope = distance = None
        if index >= VALID_OFFSET and ma30:
            previous_ma = _mean(closes[index - SLOPE_LOOKBACK - MA_WINDOW + 1: index - SLOPE_LOOKBACK + 1])
            slope = ma30 / previous_ma - 1 if previous_ma else 0
            distance = bar.close / ma30 - 1
            valid_indices.append(index)

        resistance = support = range_position = None
        breakout = breakdown = False
        if index >= RANGE_LOOKBACK:
            prior = bars[index - RANGE_LOOKBACK: index]
            resistance = max(item.high for item in prior)
            support = min(item.low for item in prior)
            breakout = bar.close > resistance
            breakdown = bar.close < support
            range_position = (bar.close - support) / (resistance - support) if resistance > support else 0.5

        lower_highs = higher_lows = lower_lows = False
        if index >= 8:
            recent = bars[index - 3: index + 1]
            previous = bars[index - 7: index - 3]
            lower_highs = max(item.high for item in recent) < max(item.high for item in previous)
            higher_lows = min(item.low for item in recent) > min(item.low for item in previous)
            lower_lows = min(item.low for item in recent) < min(item.low for item in previous)

        new_low = index >= 13 and bar.close < min(closes[index - 13: index])
        new_high = index >= 13 and bar.close > max(closes[index - 13: index])

        crosses = 0
        if index >= VALID_OFFSET + 13:
            signs = [1 if (weeks[j]["price_vs_ma"] or 0) > 0 else -1 for j in range(index - 12, index)]
            previous_sign = signs[0]
            for sign in signs[1:]:
                if sign != previous_sign:
                    crosses += 1
                previous_sign = sign

        volume_ratio = _volume_ratio(bars, index)

        slope_delta = None
        if index >= VALID_OFFSET + SLOPE_LOOKBACK:
            prior_slope = weeks[index - SLOPE_LOOKBACK]["ma30_slope"]
            slope_delta = slope - prior_slope if prior_slope is not None else None

        momentum = bar.close / closes[index - 13] - 1 if index >= 13 else None
        direction = _structure_direction(slope or 0, distance or 0) if slope is not None else None

        weeks.append({
            "date": bar.date, "open": _rounded(bar.open), "high": _rounded(bar.high),
            "low": _rounded(bar.low), "close": _rounded(bar.close), "volume": _rounded(bar.volume, 0),
            "ma30": _rounded(ma30, 3),
            "ma30_slope_pct": _rounded(slope * 100 if slope is not None else None, 3),
            "price_vs_ma_pct": _rounded(distance * 100 if distance is not None else None),
            "momentum_13w_pct": _rounded(momentum * 100 if momentum is not None else None),
            "volume_ratio": _rounded(volume_ratio), "direction": direction, "stage": None,
            "ma30_slope": slope, "price_vs_ma": distance, "momentum_13w": momentum,
            "slope_delta_4w": slope_delta, "range_position_26w": range_position,
            "resistance_26w": _rounded(resistance), "support_26w": _rounded(support),
            "breakout_26w": breakout, "breakdown_26w": breakdown,
            "new_low_13w": new_low, "new_high_13w": new_high,
            "lower_highs": lower_highs, "higher_lows": higher_lows, "lower_lows": lower_lows,
            "tests_support": support is not None and bar.close >= support and bar.close <= support * 1.03,
            "ma_crosses_13w": crosses,
            "change_26w": bar.close / closes[index - RANGE_LOOKBACK] - 1 if index >= RANGE_LOOKBACK else None,
            "severe_breakdown": False, "rebound_from_stage_low": 0.0,
        })

    stages = _assign_classical_stages(weeks, valid_indices)

    active_stage = previous_stage = active_substage = None
    stage_age = substage_age = 0
    stage_low = None
    breakout_in_stage = continuation_in_stage = False
    stage3_matured = False
    breakout_close = None
    for index, stage in zip(valid_indices, stages):
        week = weeks[index]
        if stage != active_stage:
            if active_stage is not None:
                previous_stage = active_stage
            active_stage = stage
            stage_age = 0
            stage_low = week["close"]
            active_substage = None
            breakout_in_stage = continuation_in_stage = False
            stage3_matured = False
            breakout_close = None
        stage_age += 1
        if (week["breakout_26w"] or week.get("breakout_base")) and not breakout_in_stage:
            breakout_in_stage = True
            breakout_close = week["close"]
        elif breakout_in_stage and not continuation_in_stage and week["new_high_13w"] \
                and breakout_close is not None and week["close"] > breakout_close:
            continuation_in_stage = True
        stage_low = min(stage_low, week["close"])
        week.update({
            "stage": stage, "stage_name": STAGE_NAMES[stage], "weeks_in_stage": stage_age,
            "previous_stage": previous_stage, "breakout_in_stage": breakout_in_stage,
            "continuation_in_stage": continuation_in_stage,
            "rebound_from_stage_low": week["close"] / stage_low - 1 if stage_low else 0,
        })
        week["stage3_matured"] = stage3_matured
        substage, reason = classify_classical_substage(week)
        if stage == 3 and substage in ("3", "3B"):
            stage3_matured = True
        substage_age = substage_age + 1 if substage == active_substage else 1
        active_substage = substage
        week.update({
            "substage": substage, "substage_name": SUBSTAGE_NAMES[substage],
            "substage_reason": reason, "weeks_in_substage": substage_age,
        })

    latest = weeks[-1]
    developing = is_developing_week(latest["date"], as_of or date.today())
    confirmed = weeks[-2] if developing else latest
    latest["is_developing"] = developing

    return {
        "latest": latest,
        "confirmed": confirmed,
        "developing": developing,
        "description": _description(latest, developing),
        "consistency": _fit(latest),
    }


def _unknown(reason: str) -> StageResult:
    return StageResult(stage=None, label="—", reason=reason)


def _yes_no(flag: bool) -> str:
    return "是" if flag else "否"


def _developing_note(
    developing: bool,
    stage: int,
    confirmed_stage: int | None,
    confirmed_substage: str | None,
) -> str:
    """周线没收完时的补充说明。

    参考实现的口径是「发展中结论可变化，同时显示上一完整周」。但如果这一周
    根本没把阶段翻掉，「可变化」就是纯噪声 —— 只有真翻掉了才提示。
    """
    if not developing:
        return ""
    if confirmed_stage is None:
        return "（本周尚未收完）"
    confirmed = f"Stage {confirmed_stage}" + (f" / {confirmed_substage}" if confirmed_substage else "")
    if confirmed_stage == stage:
        return f"（本周尚未收完，但与上一完整周结论一致：{confirmed}）"
    return (
        f"（本周尚未收完，这一周把阶段从 Stage {confirmed_stage} 翻成了 Stage {stage}；"
        f"上一完整周为 {confirmed}）"
    )


def compute_stage(df: pd.DataFrame, as_of: date | None = None) -> StageResult:
    """按参考实现（classical.py）判定一只票**当前**处于哪个 Weinstein 阶段。

    `as_of` 是「今天」—— 参考实现的 `analyze_classical` 也收这个参数：
    它决定最后一根周线算不算「本周尚未收完」。
    """
    if df is None or df.empty or "close" not in df.columns:
        return _unknown("无行情数据，无法判定阶段")

    bars = _resample_weekly(df)
    if len(bars) < MIN_WEEKLY_BARS:
        return _unknown(
            f"历史只有 {len(bars)} 根周线，不足 {MIN_WEEKLY_BARS} 根"
            f"（参考实现要求至少 {MIN_WEEKLY_BARS} 根周线才能判定阶段）"
        )

    result = _analyze_weekly(bars, as_of)
    latest = result["latest"]
    confirmed = result["confirmed"]
    stage = latest["stage"]

    reason = (
        f"30周均线近4周 {latest['ma30_slope_pct']:+.2f}%，价格相对均线 {latest['price_vs_ma_pct']:+.2f}%；"
        f"更低高点={_yes_no(latest['lower_highs'])}、更高低点={_yes_no(latest['higher_lows'])}、"
        f"26周突破={_yes_no(latest['breakout_26w'])}、26周跌破={_yes_no(latest['breakdown_26w'])}；"
        f"{latest['substage_reason']}"
    )
    reason += _developing_note(
        result["developing"], stage, confirmed["stage"], confirmed["substage"]
    )

    return StageResult(
        stage=stage,
        label=LABELS[stage],
        reason=reason,
        substage=latest["substage"],
        substage_name=latest["substage_name"],
        substage_reason=latest["substage_reason"],
        close=latest["close"],
        ma30=latest["ma30"],
        slope_pct=(latest["ma30_slope"] if latest["ma30_slope"] is not None else None),
        px_vs_ma=latest["price_vs_ma"],
        momentum_13w=latest["momentum_13w"],
        weeks_in_stage=latest["weeks_in_stage"],
        weekly_bars=len(bars),
        developing=result["developing"],
        confirmed_stage=confirmed["stage"],
        confirmed_substage=confirmed["substage"],
        consistency=result["consistency"],
    )
