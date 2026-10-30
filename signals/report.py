"""
signals/report.py — 每日 Markdown 报告生成器

职责：
  1. 读取过去 7 天内的所有信号（从对应策略的 signals.csv）
  2. 对每个信号的触发原因进行结构化解读
  3. 生成 Markdown 格式的每日报告
  4. 保存到 output/{strategy_id}/YYYY-MM-DD/报告_{strategy_name}_YYYY-MM-DD.md
"""

import csv
import pickle
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.rs import RS_COLUMN_NAMES, compute_rs, parse_rs_columns_from_reason

# ── 路径 ───────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ── 常量 ───────────────────────────────────────────────────
RECENT_SIGNAL_DAYS = 10

# O'Neil RS Rating 判读标准（IBD 官方口径）
RS_RATING_BANDS = [
    (90, "极强", "全市场前 10%，市场 leaders 级别"),
    (80, "强", "全市场前 20%，符合 CAN SLIM 对 RS 的基本要求"),
    (70, "中强", "全市场前 30%，可关注但不是首选"),
    (50, "中性", "与市场平均无明显差异"),
    (0, "弱", "跑输市场中位，趋势疲软，不建议追"),
]


def _safe_int(value, default: int = 0) -> int:
    """把 CSV 里读出来的值转成 int，取不到数字就退回默认值。

    「强度(1-5)」这一列可能被挤歪：旧 schema 的 signals.csv（6/7 列）
    遇上 12 列的行 → 按表头名字取值时，第 4 列（信号类型）会落到强度头上，
    int('v_eps_v2_buy') 直接抛 ValueError，一份报告崩掉、一整天白跑。
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _rs_rating_band(rating: int) -> tuple[str, str]:
    """把 1-99 的 RS Rating 翻译成 (等级, 说明)。"""
    for threshold, label, note in RS_RATING_BANDS:
        if rating >= threshold:
            return label, note
    return "弱", ""


def _rs_columns(record: dict) -> dict[str, str]:
    """
    取一条信号记录的 RS 列。

    优先读 signals.csv 的独立列；老记录没有这些列时，从 触发原因 回填。
    """
    parsed = {k: (record.get(k) or "").strip() for k in RS_COLUMN_NAMES}
    if any(parsed.values()):
        return parsed
    return {k: v for k, v in parse_rs_columns_from_reason(record.get("触发原因", "")).items()}


def _rs_rating_str(record: dict) -> str:
    """RS Rating 的表格展示值（缺失时显示 —）。"""
    return _rs_columns(record).get("RS相对强度") or "—"


# ══════════════════════════════════════════════════════════
# 历史信号的 RS 重算
# ══════════════════════════════════════════════════════════

def _replace_rs_segment(reason: str, new_segment: str) -> str:
    """把 触发原因 里的 [RS] 段换成新的；原来没有就追加到末尾。"""
    parts = reason.split(" | ")
    for i, part in enumerate(parts):
        if part.strip().startswith("[RS]"):
            parts[i] = new_segment
            return " | ".join(parts)
    if not reason.strip():
        return new_segment
    return reason + " | " + new_segment


def refresh_history_rs(history: list[dict], compute_fn) -> list[dict]:
    """
    按**当前口径**重算历史信号的 RS 列与 [RS] 段。

    为什么必须重算
    --------------
    `signals.csv` 里的 RS 是**信号发出那天**算好写死的字符串。排名池从 3,844 只
    扩到 9,082 只之后，旧信号还带着旧池子的口径（板块分母 2/2、RS 评级 90）。
    报告里「今日新增」用今天新算的值、「过去 7 天」用 CSV 里的旧值 ——
    同一条信号在一份报告里出现两个 RS Rating、两个板块分母。

    重算以**信号自己的日期**为 as_of，口径跟主流程完全一致（同一个 compute_rs）。

    算不出来（历史不足 / 数据脏）就保留原值 —— 宁可给旧数，也不给空。
    """
    if not history or compute_fn is None:
        return history

    refreshed = []
    for record in history:
        symbol = (record.get("股票") or "").strip()
        date_str = (record.get("日期") or "").strip()
        if not symbol or not date_str:
            refreshed.append(record)
            continue
        try:
            rs = compute_fn(symbol, date_str)
        except Exception:
            rs = None
        if rs is None:
            refreshed.append(record)
            continue
        new_record = dict(record)
        new_record.update(rs.to_column_dict())
        new_record["触发原因"] = _replace_rs_segment(
            record.get("触发原因", ""), rs.format_reason_segment()
        )
        refreshed.append(new_record)
    return refreshed


def make_rs_refresher(market_data: dict, cache_dir=None):
    """
    造一个 (symbol, date) -> RSResult 的重算函数。

    RS 的算法只有一处实现：`data.rs.compute_rs`。这里必须复用它 ——
    复制一份就会跟主流程漂移（重算出来的数跟今天新算的对不上，等于白重算）。

    market_data 里没有的代码从 `data/cache/{symbol}.pkl` 读；
    读不到就返回 None（调用方保留原值）。
    """
    cache_dir = Path(cache_dir) if cache_dir else Path("data/cache")

    def _refresh(symbol: str, date_str: str):
        df = market_data.get(symbol)
        if df is None:
            df = _load_cached_df(symbol, cache_dir)
            if df is None:
                return None
        md = market_data if symbol in market_data else {**market_data, symbol: df}
        return compute_rs(ticker=symbol, as_of_date=date_str, market_data=md)

    return _refresh


def _load_cached_df(symbol: str, cache_dir) -> "object | None":
    """读取 data/cache/{symbol}.pkl，失败返回 None。"""
    pkl_path = Path(cache_dir) / f"{symbol}.pkl"
    if not pkl_path.exists():
        return None
    try:
        with open(pkl_path, "rb") as f:
            df = pickle.load(f)
        if df is None or getattr(df, "empty", True):
            return None
        return df
    except Exception:
        return None


def compute_recent_signal_pnl(watchlist: dict, today: datetime, cache_dir=None) -> list[dict]:
    """统计过去 10 天触发过 crossback 的信号，及其信号日收盘价至今的收益率。

    仅统计 watchlist 中 last_crossback_date 落在 [today-10, today] 的条目。

    Returns:
        list[dict]: [{"symbol", "signal_date", "signal_close", "latest_close", "pnl_pct"}]
        按 signal_date 升序。
    """
    if not watchlist or cache_dir is None:
        return []
    cutoff = (today - timedelta(days=RECENT_SIGNAL_DAYS)).date()
    today_date = today.date()
    rows = []
    for sym, entry in watchlist.items():
        lcb = entry.get("last_crossback_date")
        if not lcb:
            continue
        try:
            lcb_date = datetime.strptime(lcb, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (cutoff <= lcb_date <= today_date):
            continue
        df = _load_cached_df(sym, cache_dir)
        if df is None:
            continue
        bar_dates = df.index.date
        try:
            idx = next(i for i, d in enumerate(bar_dates) if d == lcb_date)
        except StopIteration:
            continue
        signal_close = float(df["close"].iloc[idx])
        latest_close = float(df["close"].iloc[-1])
        if signal_close <= 0:
            continue
        pnl_pct = (latest_close / signal_close - 1) * 100
        rows.append({
            "symbol": sym,
            "signal_date": lcb,
            "signal_close": signal_close,
            "latest_close": latest_close,
            "pnl_pct": pnl_pct,
        })
    rows.sort(key=lambda r: r["signal_date"])
    return rows


def _build_recent_pnl_section(recent_pnl: list[dict]) -> list[str]:
    """生成"过去 10 天信号收益追踪"Markdown 区段。"""
    lines = ["", "### 过去 10 天信号收益追踪", ""]
    if not recent_pnl:
        lines.append("过去 10 天无触发信号。")
        lines.append("")
        return lines
    lines.append("| 股票 | 信号日期 | 信号价 | 最新价 | 收益率 |")
    lines.append("|------|----------|--------|--------|--------|")
    for r in recent_pnl:
        lines.append(
            f"| {r['symbol']} | {r['signal_date']} "
            f"| ${r['signal_close']:.2f} | ${r['latest_close']:.2f} "
            f"| {r['pnl_pct']:+.1f}% |"
        )
    lines.append("")
    return lines


# ══════════════════════════════════════════════════════════
# 公开接口
# ══════════════════════════════════════════════════════════

def generate_daily_report(
    today_records: list[dict],
    strategy_id: str = "v1_wizard",
    strategy_name: str = "魔法师策略V1",
    crossback_records: list[dict] | None = None,
    watchlist_status: dict | None = None,
    watchlist: dict | None = None,
    rs_compute=None,
) -> Path:
    """
    生成今日报告，保存到 output/{strategy_id}/YYYY-MM-DD/报告_{strategy_name}_YYYY-MM-DD.md。

    参数：
        today_records: 今天新产生的信号（来自 process_signals 的返回值）
        strategy_id:   策略文件夹 ID（如 "v1_wizard"）
        strategy_name: 策略显示名称（如 "魔法师策略V1"），用于报告标题
        crossback_records: 今日 crossback 信号（来自 ema_crossback pipeline）
        watchlist_status:  watchlist 状态摘要 {"total", "added_today", "triggered_today"}
        watchlist:        watchlist 完整字典（可选）。非空时附加"Watchlist EMA10 Crossback 状态"段落。
        rs_compute:      (symbol, date) -> RSResult|None，用于把历史信号的 RS
                         按当前口径重算（见 refresh_history_rs）。None = 不重算。

    返回：
        报告文件的 Path
    """
    today = datetime.now(tz=timezone.utc)
    today_str = today.strftime("%Y-%m-%d")

    # 创建输出目录
    day_dir = OUTPUT_DIR / strategy_id / today_str
    day_dir.mkdir(parents=True, exist_ok=True)
    report_path = day_dir / f"报告_{strategy_name}_{today_str}.md"

    # 读取过去 7 天的历史信号（从该策略专属 CSV）
    history = _read_recent_signals(strategy_id=strategy_id, days=7)

    # 生成报告内容
    content = _build_report(
        today_str, today_records, history, strategy_name, rs_compute=rs_compute
    )

    # === Crossback 区段（spec §4.3） ===
    if crossback_records is not None or watchlist_status is not None:
        content += _build_crossback_section(crossback_records or [], watchlist_status or {})

    # === Watchlist EMA10 Crossback 状态（用户偏好：以后也把这种信号放到 livetest 报告里） ===
    if watchlist:
        content += _build_watchlist_crossback_section(watchlist, today)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[报告] 已生成：{report_path}")
    return report_path


def _build_watchlist_crossback_section(watchlist: dict, today: datetime) -> str:
    """列出 watchlist 中有过 EMA10 crossback 的股票（crossback_count > 0）。

    按最近一次 crossback 日期倒序；空 watchlist 或全部无 crossback 时返回空字符串。
    """
    if not watchlist:
        return ""
    rows = []
    for sym, entry in watchlist.items():
        cb_count = entry.get("crossback_count", 0) or 0
        if cb_count <= 0:
            continue
        lcb = entry.get("last_crossback_date")
        rows.append({
            "symbol": sym,
            "signal_date": entry.get("signal_date", ""),
            "signal_price": entry.get("signal_price"),
            "stage2_stop": entry.get("stage2_stop"),
            "cb_count": cb_count,
            "last_cb": lcb or "",
        })
    if not rows:
        return ""
    rows.sort(key=lambda r: (r["last_cb"], r["symbol"]), reverse=True)

    today_str = today.strftime("%Y-%m-%d")
    lines = ["", "---", "", "## 📈 Watchlist EMA10 Crossback 状态", ""]
    lines.append("> 以下为 watchlist 中**自信号日以来有过 EMA10 回踩**的股票，"
                 f"按最近一次 crossback 日期倒序（共 {len(rows)} 只）。")
    lines.append("")
    lines.append("| 股票 | 入场价 | 止损 | Crossback 次数 | 最近一次 | 距今 |")
    lines.append("|------|--------|------|----------------|----------|------|")
    for r in rows:
        price = f"${r['signal_price']:.2f}" if r['signal_price'] is not None else "—"
        stop = f"${r['stage2_stop']:.2f}" if r['stage2_stop'] is not None else "—"
        if r["last_cb"]:
            try:
                d = datetime.strptime(r["last_cb"], "%Y-%m-%d").date()
                delta = (today.date() - d).days
                # 按"交易日"近似估算（5/7 比例，最小 1）
                trading_days = max(1, round(delta * 5 / 7))
                age = f"{trading_days} 交易日"
            except ValueError:
                age = "—"
        else:
            age = "—"
        lines.append(
            f"| **{r['symbol']}** | {price} | {stop} | {r['cb_count']} 次 "
            f"| {r['last_cb'] or '—'} | {age} |"
        )
    lines.append("")
    lines.append("**说明**：")
    lines.append("- **首次 crossback（次数 0→1）**：刚回踩均线，关注是否放量站稳")
    lines.append("- **多次 crossback（次数 ≥5）**：价格反复争夺 EMA10，动能持续性存疑")
    lines.append("- 数据来源：`output/{strategy_id}/watchlist.json` 中 `crossback_count > 0` 条目")
    lines.append("")
    return "\n".join(lines)


def _build_crossback_section(crossback_records: list[dict], watchlist_status: dict) -> str:
    """生成 EMA 10 Crossback 区段 Markdown（spec §4.3）。"""
    lines = ["", "---", "", "## EMA 10 Crossback 信号（自信号日以来）", ""]

    if crossback_records:
        lines.append("| 日期 | 股票 | RS | 强度 | 现价 | 参考止损 | 量比 | 上穿 EMA10 | 上穿涨幅 |")
        lines.append("|------|------|----|------|------|----------|------|-----------|---------|")
        for r in crossback_records:
            sections = _parse_reason(r.get("触发原因", ""))
            cb_text = sections.get("[EMA10回踩]", "")
            metrics = _parse_crossback_metrics(cb_text)
            lines.append(
                f"| {r['日期']} | {r['股票']} | {_rs_rating_str(r)} | {r['强度星级']} "
                f"| {metrics['close_str']} | {r.get('参考止损', '—')} "
                f"| {metrics['vol_str']} | — | {metrics['pct_str']} |"
            )
    else:
        lines.append("暂无 crossback 信号。")
    lines.append("")

    if watchlist_status:
        lines.append("### Watch List 状态")
        lines.append("")
        lines.append(f"- 当前监控：**{watchlist_status.get('total', 0)}** 只")
        lines.append(f"- 今日新增：**{watchlist_status.get('added_today', 0)}** 只（来自 v_eps_v2_buy）")
        lines.append(f"- 今日触发 crossback：**{watchlist_status.get('triggered_today', 0)}** 只")
        if watchlist_status.get("triggered_cumulative"):
            lines.append(
                f"- 累计触发 crossback（自信号日以来，去重）："
                f"**{watchlist_status.get('triggered_cumulative', 0)}** 只"
            )
        lines.append("")

        # 今日无触发时，展示过去 10 天信号收益追踪
        if not watchlist_status.get("triggered_today"):
            recent_pnl = watchlist_status.get("recent_pnl")
            if recent_pnl:
                lines += _build_recent_pnl_section(recent_pnl)

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 报告构建
# ══════════════════════════════════════════════════════════

def _build_report(today_str: str, today_records: list[dict], history: list[dict], strategy_name: str = "魔法师策略V1", rs_compute=None) -> str:
    lines = []

    # 历史信号的 RS 按当前口径重算：CSV 里写死的是「信号发出那天的池子」，
    # 排名池扩过之后那套数字跟今天新算的对不上（同一个信号两个 RS Rating）。
    if rs_compute is not None:
        history = refresh_history_rs(history, rs_compute)

    # ── 标题 ──────────────────────────────────────────────
    lines += [
        f"# {strategy_name} 信号日报 — {today_str}",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"> 报告范围：过去 7 天（{_days_ago(7)} 至 {today_str}）  ",
        f"> 策略：{strategy_name}  ",
        "> 提示：信号日期为策略检测到条件的当天，**建议次日开盘执行**",
        "",
        "---",
        "",
    ]

    # ── 今日新增信号 ───────────────────────────────────────
    # records 里混着 crossback pipeline 吐出的**全部历史 crossback**（自信号日以来累积），
    # 不按日期过滤的话 2026-09-29 的信号会被标成「🆕 今日新增 / 明日开盘买入」。
    # crossback 记录在报告末尾的「EMA 10 Crossback 信号」区段里另有完整展示。
    today_only   = [r for r in today_records if r.get("日期") == today_str]
    today_buys  = [r for r in today_only if r.get("信号") == "买入"]
    today_sells = [r for r in today_only if r.get("信号") == "卖出"]

    if not today_only:
        lines += [
            "## 今日新增信号",
            "",
            "> 今日无新信号。SEPA 条件严格，无信号是正常状态，代表当前市场没有",
            "> 同时满足趋势模板 + VCP 形态 + 突破 + 量能四重条件的股票。",
            "",
        ]
    else:
        if today_buys:
            lines += [
                "## 今日新增买入信号",
                "",
            ]
            for r in sorted(today_buys, key=lambda x: -_safe_int(x.get("强度(1-5)", 0))):
                lines += _format_signal_detail(r, is_today=True)
                lines.append("")

        if today_sells:
            lines += [
                "## 今日出场信号",
                "",
            ]
            for r in today_sells:
                lines += _format_exit_signal(r)
                lines.append("")

    lines += ["---", ""]

    # ── 过去7天汇总表 ──────────────────────────────────────
    lines += [
        "## 过去 7 天信号汇总",
        "",
    ]

    all_recent = history  # 已经包含今天
    buys_7d  = [r for r in all_recent if r.get("信号") == "买入"]
    sells_7d = [r for r in all_recent if r.get("信号") == "卖出"]

    if not all_recent:
        lines.append("> 过去 7 天内无任何信号记录。\n")
    else:
        if buys_7d:
            lines += [
                "### 买入信号",
                "",
                "> **RS** 列 = O'Neil RS Rating（全市场加权收益百分位 1-99，越高越强）",
                "",
            ]
            if rs_compute is not None:
                lines += [
                    "> 本表的 RS 与板块排名按**当前排名池口径重算**（截止到各自的信号日），"
                    "与「今日新增」区段同一口径；`signals.csv` 里保留的是信号发出当天的原值。",
                    "",
                ]
            lines += [
                "| 日期 | 股票 | RS | 强度 | 参考止损 | 触发摘要 |",
                "|------|------|----|------|----------|----------|",
            ]
            for r in sorted(buys_7d, key=lambda x: x.get("日期", ""), reverse=True):
                stars = "★" * _safe_int(r.get("强度(1-5)", 0))
                summary = _short_summary(r.get("触发原因", ""))
                lines.append(
                    f"| {r['日期']} | **{r['股票']}** | {_rs_rating_str(r)} | {stars} "
                    f"| {r.get('参考止损', '—')} | {summary} |"
                )
            lines.append("")

        if sells_7d:
            lines += [
                "### 出场信号",
                "",
                "| 日期 | 股票 | 出场原因 |",
                "|------|------|----------|",
            ]
            for r in sorted(sells_7d, key=lambda x: x.get("日期", ""), reverse=True):
                reason_short = r.get("触发原因", "")[:45]
                lines.append(f"| {r['日期']} | **{r['股票']}** | {reason_short} |")
            lines.append("")

    lines += ["---", ""]

    # ── 过去7天详细解读（买入信号） ────────────────────────
    past_buys = [r for r in buys_7d if r.get("日期") != today_str]
    if past_buys:
        lines += [
            "## 过去 7 天买入信号详细解读",
            "",
            "> 以下是本周内（不含今日）触发的买入信号的完整条件解读，",
            "> 供复盘和学习参考。",
            "",
        ]
        for r in sorted(past_buys, key=lambda x: x.get("日期", ""), reverse=True):
            lines += _format_signal_detail(r, is_today=False)
            lines.append("")

    # ── 操作提醒 ────────────────────────────────────────────
    lines += [
        "---",
        "",
        "## 操作提醒",
        "",
        "| 要点 | 说明 |",
        "|------|------|",
        "| 执行时机 | 信号当天收盘后确认，**次日开盘**执行 |",
        "| 止损设置 | 买入后立即挂止损单（参考止损价），不要等收盘才检查 |",
        "| 仓位建议 | 单笔不超过总资金的 20%，分散持有 |",
        "| 无信号含义 | 当前没有满足全部条件的股票，持币观望比强行入场更安全 |",
        "",
        f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 单个信号详细格式化
# ══════════════════════════════════════════════════════════

def _format_signal_detail(r: dict, is_today: bool) -> list[str]:
    """生成单个买入信号的详细 Markdown 块。"""
    symbol   = r.get("股票", "?")
    date_str = r.get("日期", "?")
    strength = _safe_int(r.get("强度(1-5)", 0))
    stars    = "★" * strength + "☆" * (5 - strength)
    stop     = r.get("参考止损", "—")
    reason   = r.get("触发原因", "")

    tag = "🆕 **今日新增**" if is_today else f"📅 {date_str}"
    sections = _parse_reason(reason)
    rs_cols = _rs_columns(r)
    rs_rating = rs_cols.get("RS相对强度") or ""

    lines = [
        f"### {tag} — 买入 {symbol}　{stars}",
        "",
        "**信号概览**",
        "",
        "| 项目 | 内容 |",
        "|------|------|",
        f"| 信号日期 | {date_str} |",
        f"| 信号方向 | 买入 |",
        f"| 信号强度 | {stars}（{strength}/5） |",
        f"| RS 相对强度 | {rs_rating if rs_rating else '—（该记录早于 RS Rating 上线）'} |",
        f"| 参考止损 | {stop} |",
        f"| 建议操作 | {'明日开盘买入' if is_today else date_str + ' 次日已可执行'} |",
        "",
    ]

    # ── [趋势] ─────────────────────────────────────────────
    trend = sections.get("[趋势]", "")
    if trend:
        lines += [
            "**[趋势模板] ✓ — 8 个条件全部通过**",
            "",
        ]
        lines += _interpret_trend(trend)
        lines.append("")

    # ── [RS] ──────────────────────────────────────────────
    rs = sections.get("[RS]", "")
    if rs:
        rs_mult = _extract_number(rs)  # RS/SPY 倍数（如 12.14）
        # 解析板块与排名（如果存在）
        sector_match = re.search(r"\(([^)]+)\)", rs)
        sector_info = sector_match.group(1) if sector_match else ""
        rank_match = re.search(r"排名(\d+)/(\d+)", sector_info)
        sector_name = re.sub(r"\s*排名\d+/\d+\s*$", "", sector_info).strip() if sector_info else ""

        lines += [
            f"**[相对强度 RS] ✓ — {rs}**",
            "",
        ]
        if rs_mult:
            interpretation = ""
            if rs_mult >= 3.0:
                interpretation = "远超大盘，是市场最强领涨股之一"
            elif rs_mult >= 2.0:
                interpretation = "显著跑赢大盘，是市场领涨股"
            elif rs_mult >= 1.2:
                interpretation = "明显强于大盘"
            elif rs_mult >= 1.05:
                interpretation = "略强于大盘，趋势偏积极"
            elif rs_mult >= 0.95:
                interpretation = "与大盘同步，无明显相对优势"
            else:
                interpretation = "弱于大盘，建议谨慎"
            lines.append(f"- **RS/SPY = {rs_mult:.2f}x**：{interpretation}")

        if rank_match and sector_name:
            rank = int(rank_match.group(1))
            total = int(rank_match.group(2))
            top_pct = round(rank * 100 / total, 0) if total else "?"
            lines.append(f"- **{sector_name} 板块排名 {rank}/{total}**（前 {top_pct}%）")
            lines.append(f"- 在 {sector_name} 同板块股票中处于**第 {rank} 强**位置")
        elif sector_name:
            lines.append(f"- 所属板块：{sector_name}")

        # O'Neil RS Rating（全市场百分位）
        if rs_rating:
            rating_int = int(rs_rating)
            label, note = _rs_rating_band(rating_int)
            lines.append(f"- **O'Neil RS Rating = {rating_int} / 99**（{label}）— {note}")
        else:
            lines.append("- **O'Neil RS Rating = —**：该记录生成时尚未启用 RS Rating，只有 RS/SPY 相对强度")

        lines.append("- RS/SPY = 1.0 为大盘基准，>1 表示跑赢大盘，<1 表示弱于大盘")
        lines.append("- RS 越高说明这只股票比市场上大多数股票都更强，是市场领涨者")
        lines.append("- **RS Rating vs RS/SPY 的区别**：RS/SPY 只跟 SPY 比（比值可任意大），"
                     "RS Rating 跟全市场所有股票比（封顶 99），后者才能说明\"排第几\"")
        lines.append("")

    # ── [VCP] ─────────────────────────────────────────────
    vcp = sections.get("[VCP]", "")
    if vcp:
        lines += [
            f"**[VCP 形态] ✓ — {vcp}**",
            "",
        ]
        lines += _interpret_vcp(vcp)
        lines.append("")

    # ── [突破] ────────────────────────────────────────────
    breakout = sections.get("[突破]", "")
    if breakout:
        lines += [
            f"**[枢轴点突破] ✓ — {breakout}**",
            "",
        ]
        lines += _interpret_breakout(breakout)
        lines.append("")

    # ── [量能] ────────────────────────────────────────────
    vol = sections.get("[量能]", "")
    if vol:
        vol_mult = _extract_number(vol)
        lines += [
            f"**[量能确认] ✓ — {vol}**",
            "",
            f"- 今日成交量是 20 日均量的 {vol_mult} 倍（要求 ≥ 1.5 倍）",
            "- 放量突破代表大资金真实入场，而非散户小打小闹",
        ]
        if vol_mult and vol_mult >= 2.0:
            lines.append("- ⚡ 成交量超过 2 倍，属于**强势放量**，突破可信度更高")
        lines.append("")

    # ── 风险提示 ──────────────────────────────────────────
    lines += [
        "> **风险提示**：以上均为系统自动识别的技术信号，不构成投资建议。",
        f"> 参考止损 {stop} 对应从买入价亏损约 10%，务必在买入后立即设置止损单。",
        "",
        "---",
    ]

    return lines


def _format_exit_signal(r: dict) -> list[str]:
    """格式化出场信号。"""
    symbol  = r.get("股票", "?")
    reason  = r.get("触发原因", "")

    lines = [
        f"### 出场 — {symbol}",
        "",
        f"**出场原因：** {reason}",
        "",
    ]
    if "触发止损" in reason:
        lines.append("> 固定止损触发：价格跌至买入价 -10% 以下，系统自动发出卖出信号。")
    elif "追踪止盈" in reason:
        lines.append("> 追踪止盈触发：价格从持仓最高点回落超过 20%，保住已有盈利后退出。")
    elif "时间止损" in reason:
        lines.append("> 时间止损触发：持仓超过 20 天但盈利不足 3%，释放资金避免占用过久。")
    lines.append("")
    return lines


# ══════════════════════════════════════════════════════════
# 触发原因解析器
# ══════════════════════════════════════════════════════════

def _parse_reason(reason: str) -> dict:
    """把 '[趋势] ... | [RS] ... | [VCP] ...' 拆分成字典。"""
    sections = {}
    parts = reason.split(" | ")
    for part in parts:
        part = part.strip()
        for key in ["[趋势]", "[RS]", "[VCP]", "[突破]", "[量能]", "[EPS]", "[Stage2]", "[EMA10回踩]"]:
            if part.startswith(key):
                sections[key] = part[len(key):].strip()
                break
    return sections


def _parse_crossback_metrics(cb_text: str) -> dict:
    """从 [EMA10回踩] 段文本里抽取 现价 / 上穿涨幅 / 量比。"""
    m_close = re.search(r"\$(\d+\.\d+)", cb_text)
    close_str = f"${m_close.group(1)}" if m_close else "—"
    m_pct = re.search(r"\+(-?\d+\.\d+)%", cb_text)
    pct_str = f"+{m_pct.group(1)}%" if m_pct else "—"
    m_vol = re.search(r"量能(\d+\.\d+)x", cb_text)
    vol_str = f"{m_vol.group(1)}x" if m_vol else "—"
    return {"close_str": close_str, "pct_str": pct_str, "vol_str": vol_str}


def _interpret_trend(trend: str) -> list[str]:
    """将趋势模板字符串转化为逐条解读。"""
    lines = []

    # 提取均线数值：SMA50(800)>SMA150(650)>SMA200(600)
    sma_match = re.search(r"SMA50\((\d+)\)>SMA150\((\d+)\)>SMA200\((\d+)\)", trend)
    if sma_match:
        s50, s150, s200 = sma_match.group(1), sma_match.group(2), sma_match.group(3)
        lines.append(f"- **均线多头排列**：SMA50({s50}) > SMA150({s150}) > SMA200({s200})")
        lines.append("  — 短中长三条均线从上到下依次排列，代表各时间周期都处于上升趋势")

    if "SMA200上升中" in trend:
        lines.append("- **SMA200 本身在上升**：长期趋势方向向上，大背景良好")

    # 52周数据
    low_match  = re.search(r"距52W低点\+?(-?\d+)%", trend)
    high_match = re.search(r"距52W高点(-?\d+)%", trend)
    if low_match:
        pct = low_match.group(1)
        lines.append(f"- **距 52 周低点 +{pct}%**：已从底部充分上涨，进入 Stage 2 上升阶段")
    if high_match:
        pct = high_match.group(1)
        note = "接近历史高点，属于强势区域" if abs(int(pct)) <= 10 else f"距高点 {pct}%"
        lines.append(f"- **距 52 周高点 {pct}%**：{note}")

    return lines


def _interpret_vcp(vcp: str) -> list[str]:
    """将 VCP 字符串转化为逐步解读。"""
    lines = []

    # 提取收缩次数
    t_match = re.search(r"VCP(\d+)T", vcp)
    n = t_match.group(1) if t_match else "?"
    lines.append(f"- **{n} 次波动收缩**：股价经历 {n} 轮整理，每轮回调幅度比上一轮更小")

    # 提取回调序列：18%→11%→5%
    depth_match = re.search(r"回调([\d%→]+)", vcp)
    if depth_match:
        seq = depth_match.group(1)
        parts = seq.split("→")
        desc = " → ".join([f"第{i+1}次 -{p}" for i, p in enumerate(parts)])
        lines.append(f"- **回调深度递减**：{desc}")
        lines.append("  — 每次调整的幅度越来越小，卖盘在逐步枯竭")

    lines.append("- **量能收缩**：每次回调期间的平均成交量也在递减，说明抛售力量减弱")

    # 末端箱体
    tail_match = re.search(r"末端([\d.]+)%箱体", vcp)
    if tail_match:
        pct = tail_match.group(1)
        lines.append(f"- **末端箱体仅 {pct}%**：最近 10 天价格波动极小（< 8%），能量高度压缩")
        lines.append("  — 这是 VCP 的关键特征：突破前的\"弹簧压紧\"状态")

    return lines


def _interpret_breakout(breakout: str) -> list[str]:
    """将突破字符串转化为逐步解读。"""
    lines = []
    pivot_match = re.search(r"\$?([\d.]+)，幅度\+?([\d.]+)%", breakout)
    if pivot_match:
        pivot = pivot_match.group(1)
        pct   = pivot_match.group(2)
        lines.append(f"- **枢轴点价格**：${pivot}（过去 30 日最高收盘价）")
        lines.append(f"- **突破幅度**：+{pct}%（要求 > 0.5%，实际 {pct}%）")
        strength_note = ""
        if float(pct) >= 3.0:
            strength_note = " — ⚡ 强势突破"
        elif float(pct) >= 1.5:
            strength_note = " — 较强突破"
        else:
            strength_note = " — 有效突破，幅度适中"
        lines.append(f"- **评估**：{float(pct):.1f}% 突破{strength_note}")
    return lines


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════

def _read_recent_signals(strategy_id: str = "v1_wizard", days: int = 7) -> list[dict]:
    """从策略专属 signals.csv 读取最近 N 天的信号记录。days=0 表示仅用于计数。"""
    signals_csv = OUTPUT_DIR / strategy_id / "signals.csv"
    if not signals_csv.exists():
        return []

    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    records = []

    with open(signals_csv, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("日期", "")
            if days == 0 or date_str >= cutoff:
                records.append(dict(row))

    # 去重：同一天同一股票同一方向只保留最后一条
    seen = set()
    unique = []
    for r in reversed(records):
        key = (r.get("日期"), r.get("股票"), r.get("信号"))
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return list(reversed(unique))


def _extract_number(text: str) -> float | None:
    """从字符串中提取第一个数字。"""
    m = re.search(r"[\d.]+", text)
    return float(m.group()) if m else None


def _short_summary(reason: str) -> str:
    """提取触发原因的简短摘要（VCP + 突破部分）。"""
    sections = _parse_reason(reason)
    parts = []
    vcp = sections.get("[VCP]", "")
    if vcp:
        t_match = re.search(r"VCP(\d+)T", vcp)
        if t_match:
            parts.append(f"VCP{t_match.group(1)}T")
    breakout = sections.get("[突破]", "")
    if breakout:
        pct_match = re.search(r"幅度\+?([\d.]+)%", breakout)
        if pct_match:
            parts.append(f"突破+{pct_match.group(1)}%")
    vol = sections.get("[量能]", "")
    if vol:
        mult_match = re.search(r"([\d.]+)倍", vol)
        if mult_match:
            parts.append(f"量{mult_match.group(1)}x")
    return " | ".join(parts) if parts else reason[:40]


def _days_ago(n: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════
# EMA 10 Crossback 独立报告
# ══════════════════════════════════════════════════════════

def generate_crossback_report(
    records: list[dict],
    watchlist: dict,
    strategy_id: str,
    today: datetime,
    recent_pnl: list | None = None,
    cache_dir=None,
) -> Path | None:
    """生成独立的 EMA 10 Crossback 报告（spec §4.3）。

    输出路径：output/{strategy_id}/{YYYY-MM-DD}/报告_EMA10回踩_{YYYY-MM-DD}.md
    若 records 为空且 watchlist 也为空，返回 None（不生成）。

    Returns:
        Path | None：写入的报告文件路径；若不生成则返回 None
    """
    if not records and not watchlist:
        return None

    today_str = today.strftime("%Y-%m-%d")
    report_dir = OUTPUT_DIR / strategy_id / today_str
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"报告_EMA10回踩_{today_str}.md"

    lines = []
    lines.append(f"# EMA 10 回踩报告 ({today_str})")
    lines.append("")
    lines.append(f"**策略**：EMA 10 Crossback（{strategy_id}）")
    lines.append("")
    lines.append("## 字段说明")
    lines.append("")
    lines.append("- **现价**：crossback 当日收盘价")
    lines.append("- **EMA10**：crossback 当日的 EMA(10)")
    lines.append("- **上穿涨幅**：crossback 当日 close 相对 EMA10 的涨幅 (%)")
    lines.append("- **量比**：crossback 当日成交量 ÷ 过去 20 日均量")
    lines.append("- **强度**：1-5 ★（基于量能倍数）")
    lines.append("- **RS**：O'Neil RS Rating（全市场加权收益百分位 1-99，越高越强；"
                 "80 以上属 CAN SLIM 的合格区间）")
    lines.append("")
    lines.append("## Crossback 信号（自信号日以来）")
    lines.append("")
    if records:
        lines.append("| 日期 | 股票 | RS | 强度 | 现价 | 参考止损 | 量比 | 上穿 EMA10 | 上穿涨幅 |")
        lines.append("|------|------|----|------|------|----------|------|-----------|---------|")
        for r in records:
            sections = _parse_reason(r.get("触发原因", ""))
            cb_text = sections.get("[EMA10回踩]", "")
            metrics = _parse_crossback_metrics(cb_text)
            lines.append(
                f"| {r['日期']} | {r['股票']} | {_rs_rating_str(r)} | {r['强度星级']} "
                f"| {metrics['close_str']} | {r.get('参考止损', '—')} "
                f"| {metrics['vol_str']} | — | {metrics['pct_str']} |"
            )
    else:
        lines.append("暂无 crossback 信号。")

    lines.append("")
    lines.append("## 看盘建议")
    lines.append("")
    lines.append("- 首次 crossback（crossback_count == 0 → 1）：重点关注")
    lines.append("- 二次 crossback：观察量能是否继续放大")
    lines.append("- 参考止损（stage2_stop）= v_eps_v2 信号日的 close × (1 - 8%)")
    lines.append("")
    lines.append("## Watch List 状态汇总")
    lines.append("")
    lines.append(f"- 当前监控：**{len(watchlist)}** 只")
    today_triggered = {r['股票'] for r in records if r.get('日期') == today_str}
    if today_triggered:
        lines.append(f"- 今日触发 crossback：**{len(today_triggered)}** 只（{', '.join(sorted(today_triggered))}）")
    else:
        lines.append(f"- 今日触发 crossback：**0** 只")
    if records:
        cumulative_symbols = {r['股票'] for r in records}
        lines.append(
            f"- 累计触发 crossback（自信号日以来，去重）：**{len(cumulative_symbols)}** 只"
            f"（{', '.join(sorted(cumulative_symbols))}）"
        )
    lines.append("")

    # 今日无触发时，展示过去 10 天信号收益追踪
    if not today_triggered:
        if recent_pnl is None and cache_dir is not None:
            recent_pnl = compute_recent_signal_pnl(watchlist, today, cache_dir)
        if recent_pnl:
            lines += _build_recent_pnl_section(recent_pnl)

    if watchlist:
        lines.append("### 监控明细")
        lines.append("")
        lines.append("| 股票 | 信号日期 | 信号价 | Stage2 止损 | Crossback 次数 | 最近 crossback 日期 |")
        lines.append("|------|----------|--------|------------|---------------|--------------------|")
        for sym in sorted(watchlist.keys()):
            entry = watchlist[sym]
            cb_count = entry.get("crossback_count", 0)
            last_cb = entry.get("last_crossback_date", "—")
            lines.append(
                f"| {sym} | {entry.get('signal_date', '—')} "
                f"| ${entry.get('signal_price', 0):.2f} "
                f"| ${entry.get('stage2_stop', 0):.4f} "
                f"| {cb_count} | {last_cb} |"
            )
        lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return report_path
