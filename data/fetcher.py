"""
data/fetcher.py — 数据获取模块

职责：给定股票代码列表，获取日线 OHLCV 历史数据。

数据来源（三层优先级）：
1. 本地持久化存储（data/cache/）
   - 永久保存，无过期删除
   - 若数据足够新（实盘 ≤0天，回测 ≤3天）直接使用，零网络请求
2. 增量更新（本地有历史但不够新）
   - 只从最早的缺口日期起，批量下载 delta 数据，合并后写回本地
   - 例：本地数据到 3月4日，今天3月6日 → 只下载 3月5日~6日 两天的新数据
3. 全量下载（本地无数据或历史不足）
   - Alpaca Market Data API（主）→ Yahoo Finance v8（备）

数据格式：pandas DataFrame，列名统一为小写 (open, high, low, close, volume)
         索引为 UTC 时区的 DatetimeIndex
"""

import os
import time
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件（确保从脚本所在目录加载）
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# 调试：确保 API Key 已加载
_api_key = os.getenv("ALPACA_API_KEY")
if not _api_key:
    print(f"[警告] Alpaca API Key 未加载，env_path={env_path}")
import pickle
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests


# ─── 路径 ──────────────────────────────────────────────────────────
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# Alpaca 批次失败时二分拆批的最大层数。200 只 -> 1 只需 log2(200) ≈ 8 层，
# 留到 12 层做余量，保证递归一定收敛。
_MAX_BATCH_SPLIT_DEPTH = 12

# Yahoo 回退（Alpaca 覆盖不到的 ADR / OTC 代码）的上限与熔断。
#
# 2026-10-26 实测：无上限时 107 只回退花了 327.6 秒，而它们**一只都取不到**
# （403 IP 级限流）。上限 20 只 + 连续 403 熔断，把这段从 5 分半压到 1 分钟内。
YAHOO_FALLBACK_MAX_SYMBOLS = 20
_YAHOO_403_STREAK_LIMIT = 10

# 取不到的代码记到这里（追加），不静默丢
FAILED_DOWNLOADS_PATH = Path(__file__).parent / "download_failed.txt"

# ─── Yahoo Finance 备用接口 ────────────────────────────────────────
_YF_API = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


# ══════════════════════════════════════════════════════════════════
# 公开接口
# ══════════════════════════════════════════════════════════════════

def _is_us_stock(symbol: str) -> bool:
    """
    判断股票是否为美股（Alpaca 支持的标的）。
    美股：无后缀（如 AAPL、BRK.B）或后缀为 .PK（OTC）；
    非美股：有区域后缀如 .HK、.SS、.SH、.L、.TO 等。
    """
    if "." not in symbol:
        return True
    suffix = symbol.split(".", 1)[1].upper()
    return suffix == "PK"


def _is_index(symbol: str) -> bool:
    """
    判断是否为指数（^开头，如 ^VIX、^SPX）。
    指数不走 Alpaca，直接走 Yahoo Finance。
    """
    return symbol.startswith("^")


def fetch(
    symbols: list[str],
    history_days: int,
    live_mode: bool = False,
    end_date: datetime | None = None,
) -> dict[str, pd.DataFrame]:
    """
    获取多只股票的日线历史数据。

    三层策略：
    1. 本地数据足够新 → 直接使用，无网络请求
    2. 本地有历史但不够新 → 增量下载 delta，合并后写回
    3. 本地无数据或历史不足 → 全量下载

    参数：
        symbols:       股票代码列表，如 ["AAPL", "NVDA"]
        history_days:  往前拉多少天的数据
        live_mode:     实盘模式时设为 True，强制要求最新收盘数据（当天或上一交易日）
                       回测模式设为 False，允许 3 天缓存宽容（覆盖周末/节假日）
        end_date:      可选，指定数据截止日期。回测时传入实际回测结束日，
                       本地数据以此判断是否足够新（避免用 datetime.now() 误判过期）

    返回：
        {symbol: DataFrame}，DataFrame 列：open, high, low, close, volume
    """
    if end_date is None:
        end_date = datetime.now(tz=timezone.utc)
    start_date = end_date - timedelta(days=history_days)

    t0 = time.time()
    result: dict[str, pd.DataFrame] = {}
    need_full: list[str] = []                         # 无本地数据，全量下载
    need_update: list[tuple[str, pd.DataFrame]] = []  # 有本地数据但需增量更新

    # ── 第一步：三分类 ────────────────────────────────────────────
    t1 = time.time()
    for symbol in symbols:
        local = _load_local(symbol)
        if local is None:
            need_full.append(symbol)
        else:
            last = local.index[-1]
            if last.tzinfo is None:
                last = last.tz_localize("UTC")
            max_age = 0 if live_mode else 3
            if (end_date - last).days <= max_age:
                # Tier 1：本地数据足够新，直接使用
                result[symbol] = local
            elif (end_date - last).days > 60:
                # Tier 3：本地数据太旧（>60天），当无数据处理，全量下载
                # 避免旧缓存（如2018年数据）拉低全局 delta_start
                need_full.append(symbol)
            else:
                # Tier 2：本地有历史，但需要增量补充
                need_update.append((symbol, local))

    print(
        f"[数据] 本地直接使用 {len(result)} 只，"
        f"增量更新 {len(need_update)} 只，"
        f"全量下载 {len(need_full)} 只"
    )
    print(f"[数据] 分类耗时: {time.time()-t1:.1f}秒")

    # ── 第二步：全量下载（使用 Alpaca + Yahoo 备用）────────────────────────────
    t2 = time.time()
    if need_full:
        # Alpaca 只支持美股，分离非美股和指数
        us_full = [s for s in need_full if _is_us_stock(s) and not _is_index(s)]
        intl_full = [s for s in need_full if not _is_us_stock(s)]
        index_full = [s for s in need_full if _is_index(s)]

        # ^VIX 走 GitHub 专用源
        if "^VIX" in index_full:
            vix_df = _fetch_vix_from_github()
            if vix_df is not None and not vix_df.empty:
                result["^VIX"] = vix_df
                _save_local("^VIX", vix_df)

        # 其他指数暂不处理
        remaining_index = [s for s in index_full if s != "^VIX"]
        if remaining_index:
            print(f"[数据] 指数 {remaining_index} 暂不支持，跳过")

        if intl_full:
            print(f"[数据] 非美股 {len(intl_full)} 只，直接走 Yahoo Finance（全量）...")
            yahoo_full = _fetch_via_yahoo(intl_full, start_date, end_date, history_days)
            for symbol, df in yahoo_full.items():
                result[symbol] = df
                _save_local(symbol, df)

        if us_full:
            alpaca_result = _fetch_via_alpaca(us_full, start_date, end_date)
            for symbol, df in alpaca_result.items():
                result[symbol] = df
                _save_local(symbol, df)

            still_missing = [s for s in us_full if s not in alpaca_result]
            if still_missing:
                print(f"[数据] Alpaca 未覆盖 {len(still_missing)} 只，fallback 到 Yahoo Finance...")
                yahoo_result = _fetch_via_yahoo(
                    still_missing,
                    start_date,
                    end_date,
                    history_days,
                    max_symbols=YAHOO_FALLBACK_MAX_SYMBOLS,
                )
                for symbol, df in yahoo_result.items():
                    result[symbol] = df
                    _save_local(symbol, df)

            # ── Alpha Vantage 补充：Alpaca 数据不足 2016 年则补充 2010-2016 ─
            # 检查 Alpaca 返回的数据中，有哪些股票的截止日期早于 2016-01-01
            # 这说明其历史不够长，需要用 Alpha Vantage 补充早期数据
            cutoff_2016 = datetime(2016, 1, 1, tzinfo=timezone.utc)
            need_av_backfill = []
            for symbol, df in alpaca_result.items():
                last_date = df.index[-1]
                if last_date.tzinfo is None:
                    last_date = last_date.tz_localize("UTC")
                if last_date < cutoff_2016:
                    need_av_backfill.append(symbol)

            if need_av_backfill:
                print(f"[数据] {len(need_av_backfill)} 只股票 Alpaca 数据截止于 2016 年前，用 Alpha Vantage 补充历史...")
                av_result = _fetch_via_alpha_vantage(need_av_backfill, start_date, end_date)
                for symbol, df in av_result.items():
                    # Alpha Vantage 可能有更早的数据，合并
                    existing = result.get(symbol)
                    if existing is not None and not existing.empty:
                        # 同样按交易日去重（见 _merge_delta）
                        merged = _merge_delta(existing, df)
                        result[symbol] = merged
                        # 存合并后的结果 —— 原来存的是 df（只有 AV 那份），
                        # 磁盘上的文件和内存里算 RS 的那份对不上。
                        _save_local(symbol, merged)
                    else:
                        result[symbol] = df
                        _save_local(symbol, df)

    # ── 第三步：增量更新（使用 Alpaca + Yahoo 备用）────────────────────────────
    if need_update:
        update_symbols = [s for s, _ in need_update]

        # 从所有待更新股票中取最早的 last_date（只取日期，去掉时间）
        min_last = min(
            (df.index[-1].date() if hasattr(df.index[-1], 'date') else df.index[-1].normalize())
            for _, df in need_update
        )
        delta_start = datetime.combine(min_last + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

        print(
            f"[数据] 增量下载 {len(update_symbols)} 只，"
            f"从 {delta_start.strftime('%Y-%m-%d')} 起"
        )

        # Alpaca 只支持美股，分离非美股和指数
        us_update = [s for s in update_symbols if _is_us_stock(s) and not _is_index(s)]
        intl_update = [s for s in update_symbols if not _is_us_stock(s)]
        index_update = [s for s in update_symbols if _is_index(s)]

        # ^VIX 增量更新走 GitHub（GitHub 数据通常当天或前一天更新）
        if "^VIX" in index_update:
            # 全量替换：GitHub 数据是完整的，不需要合并本地
            vix_df = _fetch_vix_from_github()
            if vix_df is not None and not vix_df.empty:
                result["^VIX"] = vix_df
                _save_local("^VIX", vix_df)

        # 其他指数暂不处理
        remaining_index = [s for s in index_update if s != "^VIX"]
        if remaining_index:
            print(f"[数据] 指数 {remaining_index} 暂不支持，跳过")

        new_data: dict = {}

        if intl_update:
            print(f"[数据] 非美股 {len(intl_update)} 只，直接走 Yahoo Finance（增量）...")
            yahoo_delta = _fetch_via_yahoo(intl_update, delta_start, end_date, 30)
            new_data.update(yahoo_delta)

        if us_update:
            us_update_tuples = [(s, df) for s, df in need_update if _is_us_stock(s)]
            new_data_alpaca = _fetch_via_alpaca(us_update, delta_start, end_date)

            # Alpaca 不支持的股票 fallback 到 Yahoo Finance 增量补充
            alpaca_missed = [s for s in us_update if s not in new_data_alpaca]
            if alpaca_missed:
                print(f"[数据] 增量更新：Alpaca 未覆盖 {len(alpaca_missed)} 只，fallback 到 Yahoo Finance...")
                yahoo_delta = _fetch_via_yahoo(
                    alpaca_missed,
                    delta_start,
                    end_date,
                    30,
                    max_symbols=YAHOO_FALLBACK_MAX_SYMBOLS,
                )
                new_data.update(yahoo_delta)

            new_data.update(new_data_alpaca)

        for symbol, existing_df in need_update:
            new_rows = new_data.get(symbol)
            if new_rows is not None and not new_rows.empty:
                # 按**交易日**去重（见 _merge_delta）：按时间戳去重会让
                # Alpaca(04:00) 和 Yahoo(00:00) 的同一个交易日同时留在文件里，
                # 一份文件两套价格基准，RS 就算错了。
                merged = _merge_delta(existing_df, new_rows)
                result[symbol] = merged
                _save_local(symbol, merged)
            else:
                # delta 为空（节假日或 API 暂无数据），使用现有本地数据
                result[symbol] = existing_df

    print(f"[数据] 成功获取 {len(result)}/{len(symbols)} 只股票的数据")
    print(f"[数据] 总耗时: {time.time()-t0:.1f}秒")
    return result


def _merge_delta(existing_df: pd.DataFrame, new_rows: pd.DataFrame) -> pd.DataFrame:
    """
    把增量数据并进已有行情，**按交易日**去重。

    为什么不能按时间戳去重
    ----------------------
    两个数据源的日线 bar 时间戳不一样：

        Alpaca → 04:00 UTC（美东当日零点）
        Yahoo  → 00:00 UTC

    按精确时间戳去重时，同一个交易日的两根 bar 会**同时留在文件里**。
    而两套 bar 的价格基准还不一样 —— Alpaca 的 adjustment="all" 只做拆股复权，
    Yahoo 的 adjclose 还做分红复权。一份文件里同时住着两套基准，算出来的
    加权收益就是拿两套尺子做比值。

    实测（2026-10-23，10,695 只行情 pkl）：5,366 只文件同时含 00:00 和
    04:00 的 bar，其中 1,489 只的同日收盘价差 >2%（最大的差 10,415%）。
    而 RS 排名池正是拿这些 close 算百分位 —— 差 2% 时排名错位看不出来，
    差 100 倍时那只票的 RS 完全是另一个数。

    规则：**文件里已有的那一天，以文件自己的 bar 为准**（保住它原本的价格
    基准），增量只负责补上文件里还没有的交易日。这样文件不会因为一次
    增量更新就沾上另一个源的基准。
    """
    if existing_df is None or existing_df.empty:
        return new_rows
    if new_rows is None or new_rows.empty:
        return existing_df

    # 时区对齐：一边有 tz 一边没有时，统一成有 tz 的那份
    if existing_df.index.tz is None and new_rows.index.tz is not None:
        existing_df = existing_df.copy()
        existing_df.index = existing_df.index.tz_localize("UTC")
    elif existing_df.index.tz is not None and new_rows.index.tz is None:
        new_rows = new_rows.copy()
        new_rows.index = new_rows.index.tz_localize("UTC")

    if not isinstance(existing_df.index, pd.DatetimeIndex) or not isinstance(
        new_rows.index, pd.DatetimeIndex
    ):
        # 不是时间索引就退回按时间戳去重（老行为）
        merged = pd.concat([existing_df, new_rows])
        return merged[~merged.index.duplicated(keep="last")].sort_index()

    have = existing_df.index.normalize()
    incoming = new_rows.index.normalize()
    fresh = new_rows[~incoming.isin(have)]

    merged = pd.concat([existing_df, fresh])
    return merged.sort_index()


def clear_cache(symbol: str | None = None) -> None:
    """清除本地持久化数据。不指定 symbol 则清除全部。"""
    if symbol:
        f = CACHE_DIR / f"{symbol}.pkl"
        if f.exists():
            f.unlink()
            print(f"[数据] 已清除 {symbol} 的本地数据")
    else:
        for f in CACHE_DIR.glob("*.pkl"):
            f.unlink()
        print("[数据] 已清除所有本地数据")


# ══════════════════════════════════════════════════════════════════
# 本地持久化读写（永久存储，不做时效性判断）
# ══════════════════════════════════════════════════════════════════

def _load_local(symbol: str) -> pd.DataFrame | None:
    """
    读取本地持久化数据。只要本地文件存在且可读就直接返回，
    不做历史覆盖检查，不做时效性判断。
    """
    cache_file = CACHE_DIR / f"{symbol}.pkl"
    if not cache_file.exists():
        return None

    try:
        with open(cache_file, "rb") as f:
            df: pd.DataFrame = pickle.load(f)
    except Exception:
        return None

    if df is None or df.empty:
        return None

    return df


def _save_local(symbol: str, df: pd.DataFrame) -> None:
    """将 DataFrame 写入本地持久化存储（覆盖旧文件）。"""
    cache_file = CACHE_DIR / f"{symbol}.pkl"
    with open(cache_file, "wb") as f:
        pickle.dump(df, f)


# ══════════════════════════════════════════════════════════════════
# Alpaca Market Data API（主要来源）
# ══════════════════════════════════════════════════════════════════

def _fetch_via_alpaca(
    symbols: list[str], start_date: datetime, end_date: datetime
) -> dict[str, pd.DataFrame]:
    """
    用 Alpaca Historical Data API 批量下载多只股票的日线数据。

    优点：专为程序化访问设计，无限流，批量下载一次请求拿所有数据。
    需要：.env 文件中配置 ALPACA_API_KEY 和 ALPACA_SECRET_KEY。
    """
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

    if not api_key or not secret_key:
        print("[数据] 未配置 Alpaca API Key，跳过 Alpaca 下载")
        return {}

    print(f"[数据] Alpaca 尝试下载 {len(symbols)} 只股票...")

    # 过滤非 ASCII 字符的股票代码（如中文"代码"），防止整批请求失败
    original_count = len(symbols)
    symbols = [s for s in symbols if s.isascii()]
    if len(symbols) < original_count:
        print(f"[数据] 过滤 {original_count - len(symbols)} 个非 ASCII 代码")

    try:
        from alpaca.data.historical import StockHistoricalDataClient
    except ImportError:
        print("[数据] alpaca-py 未安装，跳过 Alpaca 下载")
        return {}

    # Alpaca 每次最多请求 1000 只，分批处理
    BATCH = 200
    result: dict[str, pd.DataFrame] = {}
    client = StockHistoricalDataClient(api_key, secret_key)

    for i in range(0, len(symbols), BATCH):
        batch = symbols[i: i + BATCH]
        batch_result = _alpaca_fetch_batch(
            client, batch, start_date, end_date, set()
        )
        result.update(batch_result)

    if result:
        print(f"[数据] Alpaca 成功下载 {len(result)}/{len(symbols)} 只")
    return result


def _alpaca_fetch_batch(
    client,
    symbols: list[str],
    start_date: datetime,
    end_date: datetime,
    bad_symbols: set[str],
    _depth: int = 0,
) -> dict[str, pd.DataFrame]:
    """
    Alpaca 单批次下载。

    失败**绝不整批丢弃**：拆成两半分别重试，只有单只票反复失败才放弃它一只。

    为什么不能整批丢：live run 的股票池有 6,000+ 只，混着 ADR、unit、
    warrant 和早该退市的票。整批 `return {}` 会让 200 只票静默没有新数据，
    日志只留一行「批次失败」—— 项目「成功跑完、什么错都没报」，但这些票
    的 RS 拿的是上个月的旧价。

    "invalid symbol" 的剔除上限也一并去掉了：原来剔满 10 个之后，
    第 11 个无效代码会让**剩下整批**直接返回 {}。现在靠拆批定位坏代码，
    好票不会陪葬。
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    # 尝试导入 DataFeed（用于指定免费 IEX 数据源）
    try:
        from alpaca.data.enums import DataFeed
        feed = DataFeed.IEX
    except (ImportError, AttributeError):
        feed = "iex"

    clean = [s for s in symbols if s not in bad_symbols]
    if not clean:
        return {}

    try:
        # 注意：不使用 end 参数，避免 Alpaca API bug（返回空数据）
        req = StockBarsRequest(
            symbol_or_symbols=clean,
            timeframe=TimeFrame.Day,
            start=start_date.replace(tzinfo=None),
            adjustment="all",
            feed=feed,   # IEX = 免费数据源；SIP 需付费订阅
        )
        bars = client.get_stock_bars(req)
        raw_df = bars.df  # MultiIndex: (symbol, timestamp)

        # 调试：打印 Alpaca 返回的数据
        # if not raw_df.empty:
        #     print(f"[调试] Alpaca 返回 {len(raw_df)} 条记录")
        # else:
        #     print(f"[调试] Alpaca 返回空数据，start_date={start_date}")

        if raw_df.empty:
            # 空响应**不等于**「这批票都没数据」—— 拆批重试。
            #
            # 原来这里直接 return {}，一批 200 只静默消失，随后全部 fallback
            # 到 Yahoo。Yahoo 的 bar 时间戳是 00:00 UTC、Alpaca 的是 04:00 UTC，
            # 合并进已有文件后同一个交易日会留下两根 bar —— 双源污染的来源。
            if len(clean) > 1 and _depth < _MAX_BATCH_SPLIT_DEPTH:
                mid = len(clean) // 2
                print(f"[数据] Alpaca 批次返回空数据（{len(clean)} 只），拆成两半重试")
                left = _alpaca_fetch_batch(
                    client, clean[:mid], start_date, end_date, bad_symbols, _depth + 1
                )
                right = _alpaca_fetch_batch(
                    client, clean[mid:], start_date, end_date, bad_symbols, _depth + 1
                )
                return {**left, **right}
            print(f"[数据] Alpaca 放弃代码 {clean[0] if clean else '?'}：返回空数据")
            return {}

        result: dict[str, pd.DataFrame] = {}
        for sym in raw_df.index.get_level_values("symbol").unique():
            sym_df = raw_df.xs(sym, level="symbol").copy()
            keep = [c for c in ["open", "high", "low", "close", "volume"] if c in sym_df.columns]
            sym_df = sym_df[keep]
            if sym_df.index.tzinfo is None:
                sym_df.index = sym_df.index.tz_localize("UTC")
            sym_df = sym_df.dropna()
            sym_df = sym_df[sym_df["volume"] > 0]
            if not sym_df.empty:
                result[sym] = sym_df
        return result

    except Exception as e:
        err_str = str(e)
        # 尝试从错误信息中提取无效 symbol，例如 {"message":"invalid symbol: BRK-B"}
        m = re.search(r"invalid symbol[:\s]+([A-Z0-9./\-]+)", err_str, re.IGNORECASE)
        if m:
            bad_sym = m.group(1).strip('",}')
            # 只在「这个代码还没被剔过」时重试整批。否则报错会一直指着同一个
            # 代码，整批重试变成死循环（旧实现那个 10 次上限就是被这个逼出来的）。
            if bad_sym not in bad_symbols:
                print(f"[数据] Alpaca 跳过无效代码：{bad_sym}")
                bad_symbols.add(bad_sym)
                return _alpaca_fetch_batch(
                    client, symbols, start_date, end_date, bad_symbols, _depth
                )

        # 整批失败 -> 二分拆批重试，绝不让几百只票陪葬
        if len(clean) > 1 and _depth < _MAX_BATCH_SPLIT_DEPTH:
            mid = len(clean) // 2
            print(f"[数据] Alpaca 批次失败（{len(clean)} 只），拆成两半重试：{e}")
            left = _alpaca_fetch_batch(
                client, clean[:mid], start_date, end_date, bad_symbols, _depth + 1
            )
            right = _alpaca_fetch_batch(
                client, clean[mid:], start_date, end_date, bad_symbols, _depth + 1
            )
            return {**left, **right}

        # 只剩一只票还失败 -> 放弃它一只（记下来，别静默）
        print(f"[数据] Alpaca 放弃代码 {clean[0] if clean else '?'}：{e}")
        return {}


# ══════════════════════════════════════════════════════════════════
# Alpha Vantage API（补充早期历史数据，2010-2016 专用）
# ══════════════════════════════════════════════════════════════════

_AV_BASE = "https://www.alphavantage.co/query"


def _fetch_via_alpha_vantage(
    symbols: list[str],
    start_date: datetime,
    end_date: datetime,
) -> dict[str, pd.DataFrame]:
    """
    用 Alpha Vantage TIME_SERIES_DAILY（outputsize=full）补充历史数据。
    每次调用返回约 20 年历史数据，适合补充 Alpaca/Yahoo 覆盖不到的 2010-2016 段。
    限流：5 次/分钟，25 次/天（免费层）。
    """
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        print("[数据] 未配置 ALPHA_VANTAGE_API_KEY，跳过")
        return {}

    result: dict[str, pd.DataFrame] = {}
    # Alpha Vantage 免费层限流：5 次/分钟
    # 为保险起见每次 sleep 12 秒，25 次/d * 12s = 5 分钟，刚好够用
    delay = 12

    for i, symbol in enumerate(symbols):
        if i > 0 and i % 5 == 0:
            print(f"[数据] Alpha Vantage 已下载 {i}/{len(symbols)} 只，暂停防限流...")
            time.sleep(delay)
        time.sleep(delay)
        df = _av_fetch_one(symbol, api_key)
        if df is not None and not df.empty:
            # 按时间窗口裁剪
            df = df[(df.index >= start_date) & (df.index <= end_date)]
            if not df.empty:
                result[symbol] = df
                print(f"[数据]   {symbol}: AV 返回 {len(df)} 条 ({df.index[0].date()} ~ {df.index[-1].date()})")
            else:
                print(f"[数据]   {symbol}: AV 数据不在目标区间内")
        else:
            print(f"[数据]   {symbol}: AV 下载失败或无数据")

    print(f"[数据] Alpha Vantage 成功 {len(result)}/{len(symbols)} 只")
    return result


def _av_fetch_one(symbol: str, api_key: str) -> pd.DataFrame | None:
    """单只股票从 Alpha Vantage 下载 full daily 数据（CSV 格式）。"""
    url = f"{_AV_BASE}?function=TIME_SERIES_DAILY&symbol={symbol}&outputsize=full&apikey={api_key}&datatype=csv"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        text = resp.text.strip()
        if "Invalid API call" in text or "premium" in text.lower():
            print(f"[数据]   {symbol}: AV API 调用无效或需要 premium")
            return None
        lines = text.split("\n")
        if len(lines) < 2:
            return None
        # CSV format: date,open,high,low,close,volume
        header = lines[0].split(",")
        expected = ["timestamp", "open", "high", "low", "close", "volume"]
        if header[:6] != expected:
            # Try alternative format
            pass
        records = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                dt = pd.to_datetime(parts[0], utc=True)
                records.append({
                    "open": float(parts[1]),
                    "high": float(parts[2]),
                    "low": float(parts[3]),
                    "close": float(parts[4]),
                    "volume": int(parts[5]),
                })
            except (ValueError, IndexError):
                continue
        if not records:
            return None
        df = pd.DataFrame(records)
        df.index = pd.DatetimeIndex([r["date"] for r in records], tz="UTC")
        df.index.name = "date"
        df = df[["open", "high", "low", "close", "volume"]]
        df = df.dropna()
        df = df[df["volume"] > 0]
        return df
    except Exception as e:
        print(f"[数据]   {symbol}: AV 请求异常 {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# Yahoo Finance v8 API（备用来源）
# ══════════════════════════════════════════════════════════════════

def _fetch_via_yahoo(
    symbols: list[str],
    start_date: datetime,
    end_date: datetime,
    history_days: int,
    max_symbols: int | None = None,
    failed_path: Path = FAILED_DOWNLOADS_PATH,
) -> dict[str, pd.DataFrame]:
    """
    逐只从 Yahoo Finance 下载，每只之间等待 2 秒防限流。

    max_symbols 是**回退上限**：Alpaca 覆盖不到的代码往往上百只，而 Yahoo 对
    它们基本一只都取不到（403 IP 级限流）。2026-10-26 实测：无上限时
    107 只回退花了 327.6 秒，live run 的数据获取有 86% 的时间耗在这上面。
    超上限的代码记进 data/download_failed.txt，不静默丢。

    连续 403 达到 _YAHOO_403_STREAK_LIMIT 就熔断：IP 级限流时继续打只会更糟。
    """
    t0 = time.time()
    result: dict[str, pd.DataFrame] = {}
    session = requests.Session()
    session.headers.update(_HEADERS)

    failed: list[str] = []
    todo = list(symbols)
    if max_symbols is not None and len(todo) > max_symbols:
        over = todo[max_symbols:]
        failed.extend(over)
        print(
            f"[数据] Yahoo 回退上限 {max_symbols} 只，"
            f"超出的 {len(over)} 只不再尝试（记入失败清单）"
        )
        todo = todo[:max_symbols]

    streak_403 = 0
    for symbol in todo:
        if streak_403 >= _YAHOO_403_STREAK_LIMIT:
            failed.append(symbol)
            continue
        time.sleep(2)
        df, status = _yahoo_download_one(symbol, history_days, session)
        if status == "rate_limited":
            streak_403 += 1
        else:
            streak_403 = 0
        if df is not None and not df.empty:
            result[symbol] = df
        else:
            # 取不到就要留痕：被丢掉又没记录的代码，下一轮还会被当成
            # 「没试过」，静悄悄地继续用陈旧数据算 RS。
            failed.append(symbol)
            if status != "rate_limited":
                print(f"[数据] 警告：{symbol} 无法获取数据，已跳过")

    if streak_403 >= _YAHOO_403_STREAK_LIMIT:
        print(
            f"[数据] Yahoo 连续 {streak_403} 次 403（IP 级限流），"
            f"停止回退，剩余 {len(todo) - len(result)} 只记入失败清单"
        )

    if failed:
        _record_failed_downloads(failed, "yahoo", path=failed_path)

    print(f"[数据] Yahoo 下载 {len(symbols)} 只，预计耗时: {len(symbols)*2/60:.1f}分钟，实际: {time.time()-t0:.1f}秒")
    return result


def _yahoo_download_one(
    symbol: str, history_days: int, session: requests.Session
) -> tuple[pd.DataFrame | None, str]:
    """
    单只股票的 Yahoo Finance 下载。

    返回 (数据, 状态)：
      "ok"           取到数据
      "rate_limited" 403（IP 级限流）—— 调用方据此熔断
      "no_data"      响应正常但没有可用数据
      "error"        其他异常
    """
    print(f"[数据] Yahoo 下载 {symbol}...")
    try:
        url = _YF_API.format(symbol=symbol)
        params = {"interval": "1d", "range": _days_to_range(history_days)}
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code == 403:
            print(f"[数据]   {symbol} 被限流（403），跳过")
            return None, "rate_limited"
        resp.raise_for_status()
        df = _parse_yahoo_response(resp.json())
        if df is not None and not df.empty:
            return df, "ok"
        print(f"[数据]   {symbol} 无有效数据，跳过")
        return None, "no_data"
    except Exception as e:
        print(f"[数据]   {symbol} 下载失败：{e}")
        return None, "error"


def _record_failed_downloads(
    symbols: list[str], reason: str, path: Path = FAILED_DOWNLOADS_PATH
) -> None:
    """
    把取不到的代码记进 data/download_failed.txt。

    失败要留痕：被丢掉又没记录的代码，下一轮还会被当成「没试过」，
    静悄悄地继续用陈旧数据算 RS。
    """
    if not symbols:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "a", encoding="utf-8") as f:
        for sym in symbols:
            f.write(f"{stamp}\t{sym}\t{reason}\n")


def _parse_yahoo_response(data: dict) -> pd.DataFrame | None:
    chart = data.get("chart", {})
    if chart.get("error"):
        return None
    results = chart.get("result")
    if not results:
        return None
    result = results[0]
    timestamps = result.get("timestamp", [])
    if not timestamps:
        return None
    quotes = result["indicators"]["quote"][0]
    adjclose_list = result["indicators"].get("adjclose", [{}])
    adjclose = adjclose_list[0].get("adjclose") if adjclose_list else None
    df = pd.DataFrame(
        {
            "open":   quotes.get("open", []),
            "high":   quotes.get("high", []),
            "low":    quotes.get("low", []),
            "close":  adjclose if adjclose else quotes.get("close", []),
            "volume": quotes.get("volume", []),
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True),
    )
    df.index.name = "date"
    df = df.dropna()
    df = df[df["volume"] > 0]
    return df


def _days_to_range(history_days: int) -> str:
    if history_days <= 5:      return "5d"
    if history_days <= 30:     return "1mo"
    if history_days <= 90:     return "3mo"
    if history_days <= 180:    return "6mo"
    if history_days <= 365:    return "1y"
    if history_days <= 730:    return "2y"
    if history_days <= 1825:   return "5y"
    return "10y"


# ══════════════════════════════════════════════════════════════════
# GitHub datasets（VIX 专用数据源）
# 数据源：https://github.com/datasets/finance-vix
# 覆盖：1990-01-02 至今，每日更新
# ══════════════════════════════════════════════════════════════════

_VIX_GITHUB_URL = "https://raw.githubusercontent.com/datasets/finance-vix/main/data/vix-daily.csv"


def _fetch_vix_from_github() -> pd.DataFrame | None:
    """
    从 GitHub datasets 仓库获取 VIX 历史数据（每日收盘价）。

    返回：
        DataFrame，列：open, high, low, close, volume（全为0）
        索引：UTC DatetimeIndex
        如果失败返回 None。
    """
    try:
        resp = requests.get(_VIX_GITHUB_URL, timeout=20)
        if resp.status_code != 200:
            print(f"[数据] VIX GitHub 下载失败: HTTP {resp.status_code}")
            return None

        from io import StringIO
        df = pd.read_csv(
            StringIO(resp.text),
            parse_dates=["DATE"],
        )

        # 转换日期格式 MM/DD/YYYY → UTC DatetimeIndex
        df.index = pd.to_datetime(df["DATE"], format="%m/%d/%Y", utc=True)
        df.index.name = "date"

        # 统一列名（小写）
        df = df.rename(columns={
            "OPEN": "open",
            "HIGH": "high",
            "LOW": "low",
            "CLOSE": "close",
        })
        df = df[["open", "high", "low", "close"]]

        # VIX 成交量设为 0（VIX 是指数，非可交易资产）
        df["volume"] = 0

        df = df.dropna()
        print(f"[数据] VIX GitHub: {len(df)} 行, {df.index[0].date()} ~ {df.index[-1].date()}")
        return df
    except Exception as e:
        print(f"[数据] VIX GitHub 异常: {e}")
        return None
