#!/usr/bin/env python3
"""
给 `data/rs_universe.json` 里新增的标的补齐历史行情（Alpaca SIP，adjustment="all"）。

为什么需要
----------
Step 0-8 已经把排名池从 3,844 只修到 5,001 只，但那是在**已有的 6,303 个 pkl**
上做的。全市场名单有 11,423 个代码，cache 里还缺 5,912 个 —— 其中 5,062 只是 ETF
（多为债券 ETF）与 42 只 ADR。缺的那些现在完全进不了排名池。

用户红线
--------
**绝不覆盖已存在的 `data/cache/*.pkl`。** 与 `fix_dirty_history.py` 不同 ——
那个脚本走的是「改名移到 `data/cache_quarantine/`」，覆盖了还能一条命令回滚；
这里是直接往缓存目录写，一旦写错就是原件没了。所以 `save_new` 在写盘**之前**
检查目标是否存在，存在就原样跳过（返回 False）。

不删除任何东西：整个脚本只有「新写」和「跳过」两种动作。

续跑
----
两条线索，缺一不可：
  - `data/cache/<SYM>.pkl` 存在   -> 已经写过了
  - 清单里有该代码的条目          -> 已经判定过了（ok / too_short / fetch_failed）

只看 pkl 的话，那些永远攒不满 253 根 bar 的新 ETF 每跑一次就重下一遍，
几千只 × 10 年 = 白烧几小时。

用法
----
    uv run python tools/download_universe_history.py --limit 200   # 先试一小批
    uv run python tools/download_universe_history.py               # 跑完整批

中断了直接重跑同一条命令即可。
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.fix_dirty_history import (  # noqa: E402
    MIN_BARS,
    _make_alpaca_fetch,
    fetch_resilient,
    validate_frame,
)

CACHE_DIR = ROOT / "data" / "cache"
UNIVERSE_PATH = ROOT / "data" / "rs_universe.json"
MANIFEST = ROOT / "data" / "download_universe_manifest.json"
# 刷新存量是**另一件事**（补一天），判定结果必须跟下载清单分开存。
# 共用一个文件时，刷新一轮会把下载清单整个盖掉 —— 那 1,372 只「取不到 /
# 历史太短」的票就重新变成「未判定」，下次重跑脚本会把它们全下一遍。
REFRESH_MANIFEST = ROOT / "data" / "refresh_stale_manifest.json"
# 刷新存量时的旧文件留档处（改名移动，不是删除）
REFRESH_BACKUP_DIR = ROOT / "data" / "cache_refresh_stale"

# 抓取起点。
#
# 刻意用「远早于任何现存数据」而不是 as_of - 10 年：Alpaca SIP 的日线最早到
# 2016-01-04，起点给早它就原样返回全部，给晚了就**静默截断**。
# 实测踩过：起点写成 as_of - 3652 天（2016-10-22），而存量文件到 2016-04-07，
# AAPL 被从 4,103 行砍到 2,513 行、起点整整推后 6 个月 —— 新文件照样「新鲜」、
# 照样能算 RS，回测覆盖却无声缩水了。
START = datetime(2000, 1, 1, tzinfo=timezone.utc)


# ════════════════════════════════════════════════════════════════
# 选谁下载
# ════════════════════════════════════════════════════════════════


def load_universe(path: Path = UNIVERSE_PATH) -> dict:
    """读全市场名单；格式不对直接抛错，不要静默返回空名单。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        raise ValueError(f"{path} 的 symbols 不是 dict，名单文件损坏")
    return payload


def _existing_cache_symbols(cache_dir: Path) -> set[str]:
    """cache 里已有的行情 pkl（eps_/polygon_ 等非行情缓存不算）。"""
    from data.rs_universe import discover_symbols

    return set(discover_symbols(Path(cache_dir)))


def pending_symbols(
    universe: dict,
    cache_dir: Path,
    manifest: dict,
    include_etfs: bool = True,
    include_adrs: bool = True,
) -> list[str]:
    """
    还该下载哪些代码（升序）。

    排除三处：已在 cache、已在清单里判定过、类型被配置关掉。
    """
    have = _existing_cache_symbols(cache_dir)
    decided = set(manifest or {})
    out = []
    for sym, meta in universe.get("symbols", {}).items():
        if sym in have or sym in decided:
            continue
        if not include_etfs and meta.get("is_etf"):
            continue
        if not include_adrs and meta.get("is_adr"):
            continue
        out.append(sym)
    return sorted(out)


def load_manifest(path: Path = MANIFEST) -> dict:
    """
    读续跑清单，返回**代码 -> 判定**这一层。损坏就当空重来。

    ⚠️ 返回的不是整个 payload。清单文件长这样：

        {"generated_at": ..., "as_of": ..., "counts": {...},
         "entries": {"AAPL": {"status": "ok", ...}, ...}}

    `pending_symbols` 拿返回值去算「已判定」的代码集合（`set(manifest)`）。
    返回整个 payload 的话，`set(payload)` 得到的是
    {"generated_at","as_of","counts","entries"} 四个**元数据键** ——
    一只票都跳不过去，续跑完全失效：1,521 只没缓存的票每跑一次就重下一遍。

    单元测试当初传的是扁平 dict（能跳过），而 main() 传的是本函数的返回值
    （跳过不了）—— 两边对不上，测试绿灯而线上是坏的。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else payload


def alpaca_symbol(symbol: str) -> str:
    """
    原样返回 —— Alpaca 的分级股代码用的就是**点**。

    实测（2026-10-23）：`BRK.A` 返回 2,528 根 bar，`BRK-A` 直接报
    `invalid symbol`。名单里 25 个带点代码，原样能取到 23 个，换成连字符 0 个。

    早先这里写的是「点换连字符」，方向反了，于是这 25 只全部静默失败。
    保留这个函数（而不是内联 identity）是为了让这个反直觉的事实留在代码里：
    看到它的人会停下来想一秒，而不会以为多此一举给删掉。
    """
    return symbol


# ════════════════════════════════════════════════════════════════
# 写盘
# ════════════════════════════════════════════════════════════════


def save_new(cache_dir: Path, symbol: str, df: pd.DataFrame) -> bool:
    """
    写入一个新 pkl。**已存在就原样跳过**，绝不覆盖。

    返回 True = 写成功，False = 目标已存在、没动。
    """
    cache_dir = Path(cache_dir)
    target = cache_dir / f"{symbol}.pkl"
    if target.exists():
        return False
    # 不 mkdir：路径写错时应当立刻炸，而不是悄悄造出另一个缓存目录。
    with open(target, "wb") as f:
        pickle.dump(df, f)
    return True


# ════════════════════════════════════════════════════════════════
# 补齐落后一天的存量文件
# ════════════════════════════════════════════════════════════════


def stale_symbols(wide: pd.DataFrame) -> list[str]:
    """
    宽表里有数据、但**最后一根 bar 之前就断了**的列（升序）。

    Step 9 下回来的新文件带 2026-10-22 的 bar，早先那批只到 10-21。
    于是宽表最后一行只覆盖约一半的列，`vectorized_weighted_return` 的
    `has_current_bar` 会把**全部**存量文件判成「落后于最新交易日」——
    新票进了池，用户原有的 AAPL 之类却被全踢出去。

    判据刻意排除「一格数据都没有」的列：那是从没下过（压根不在 cache），
    不是落后一天，两者不该混在一张刷新名单里。
    """
    last = wide.iloc[-1]
    return sorted(wide.columns[last.isna() & wide.notna().any(axis=0)])


def refresh_stale(cache_dir: Path, backup_dir: Path, symbol: str, df: pd.DataFrame) -> bool:
    """
    用 df 替换一个**落后于最新交易日**的存量 pkl。

    返回 True = 已替换，False = 原文件不需要动（已新鲜 / 会截断历史）。

    两道拒绝，红线都在这：
      - 原文件已经有最新那一根 bar -> 没必要动
      - 新数据的**起点比旧文件更晚** -> 会砍掉前面的历史
    第二道最关键，因为它失败得很安静：新文件一样新鲜、一样能算 RS，
    但回测覆盖无声缩水几个月。判据用起点日期而不是行数 —— 旧文件可能有
    重复的日内行（AAPL 4,103 行其实只有约 2,632 个交易日），行数不可比。

    替换一律是「改名移到 backup_dir」，不是删除 ——
    回滚就一条 `mv data/cache_refresh_stale/*.pkl data/cache/`。
    """
    cache_dir = Path(cache_dir)
    backup_dir = Path(backup_dir)
    target = cache_dir / f"{symbol}.pkl"
    current = pd.read_pickle(target)
    if not isinstance(current, pd.DataFrame) or current.empty:
        return False
    if df.index[-1] in current.index:
        return False  # 已经有这一天了，不必动
    if pd.Timestamp(df.index[0]) > pd.Timestamp(current.index[0]):
        return False  # 会截断历史 —— 宁可保持落后，也不能少数据
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(backup_dir / f"{symbol}.pkl"))
    with open(target, "wb") as f:
        pickle.dump(df, f)
    return True


# ════════════════════════════════════════════════════════════════
# 下载主循环
# ════════════════════════════════════════════════════════════════


def _write_manifest(entries: dict, as_of, path: Path = MANIFEST) -> None:
    from collections import Counter

    counts = Counter(v["status"] for v in entries.values())
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": str(as_of),
        "counts": dict(counts),
        "entries": entries,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    print(f"\n[清单] {path}")
    for status, n in counts.most_common():
        print(f"    {status:14s} {n:5d}")
    problems = {
        k: v for k, v in entries.items() if v.get("status") in ("too_short", "fetch_failed", "rejected")
    }
    if problems:
        print("\n  未写盘样例（这些票要么取不到，要么历史不够 253 根）：")
        for sym, info in list(problems.items())[:10]:
            print(f"    {sym:8s} {info['status']:14s} {info.get('reason', '')}")


def download(
    symbols: list[str],
    as_of,
    cache_dir: Path = CACHE_DIR,
    batch: int = 100,
    sleep: float = 0.5,
    feed_name: str = "sip",
    refresh: bool = False,
    backup_dir: Path = REFRESH_BACKUP_DIR,
    manifest_path: Path = MANIFEST,
) -> dict:
    """
    抓一批代码写进 cache_dir，返回清单条目。

    历史不足 253 根的**不写盘**（写下来也是永远进不了排名池的死重），
    但要记进清单的 `too_short`，否则续跑会无限重下它们。

    refresh=True 时改为「刷新存量」：新数据先进备份目录再上岗（见 refresh_stale），
    供补齐落后一个交易日的旧文件用。刷新写**另一份**清单（manifest_path），
    免得把下载清单里的判定结果盖掉。
    """
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from alpaca.data.historical import StockHistoricalDataClient

    client = StockHistoricalDataClient(
        os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    )
    fetch = _make_alpaca_fetch(client, START, feed_name=feed_name)

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    t0 = time.time()

    print(f"[{'刷新' if refresh else '抓取'}] {len(symbols)} 个代码，feed={feed_name}，目标 {cache_dir}")
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        keys = {s: alpaca_symbol(s) for s in chunk}
        try:
            got = fetch_resilient(fetch, list(keys.values()), max_batch=batch)
        except Exception as exc:  # 网络抖动不该让整轮停下来
            print(f"  [WARN] 批次 {i} 失败：{exc}")
            got = {}

        for sym in chunk:
            df = got.get(keys[sym])
            if df is None or df.empty:
                entries[sym] = {"status": "fetch_failed", "reason": "Alpaca 无数据"}
                continue
            if len(df) < MIN_BARS:
                # 写下来也是永远攒不满 253 根 bar、进不了排名池的死重
                entries[sym] = {
                    "status": "too_short",
                    "bars": int(len(df)),
                    "reason": f"只有 {len(df)} 根 bar，少于 {MIN_BARS}",
                }
                continue
            ok, reason = validate_frame(df, as_of)
            if not ok:
                entries[sym] = {"status": "rejected", "reason": reason}
                continue
            try:
                changed = (
                    refresh_stale(cache_dir, backup_dir, sym, df)
                    if refresh
                    else save_new(cache_dir, sym, df)
                )
            except FileNotFoundError:
                entries[sym] = {"status": "skipped", "reason": "cache 里没有这个文件"}
                continue
            if not changed:
                entries[sym] = {"status": "skipped", "reason": "已有最新 bar，未动"}
                continue
            entries[sym] = {
                "status": "ok",
                "bars": int(len(df)),
                "last_bar": str(pd.Timestamp(df.index.max()).date()),
            }

        done = min(i + batch, len(symbols))
        n_ok = sum(1 for v in entries.values() if v["status"] == "ok")
        rate = done / max(time.time() - t0, 1e-9)
        print(f"  [{done}/{len(symbols)}] {time.time() - t0:.0f}s 已写 {n_ok}（{rate:.1f} 只/秒）")
        if sleep:
            time.sleep(sleep)

    _write_manifest(entries, as_of, path=manifest_path)
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="只下前 N 个（调试用）")
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--sleep", type=float, default=0.5, help="批次间隔秒数")
    ap.add_argument(
        "--feed", choices=("sip", "iex"), default="sip",
        help="Alpaca 数据源。默认 sip —— IEX 日线被硬截在 2020-07-27",
    )
    ap.add_argument("--as-of", default="2026-10-22", help="信号日期 YYYY-MM-DD")
    ap.add_argument("--no-etfs", action="store_true", help="不下 ETF")
    ap.add_argument("--no-adrs", action="store_true", help="不下 ADR")
    ap.add_argument("--dry-run", action="store_true", help="只打印要下哪些，不联网")
    ap.add_argument(
        "--refresh-stale", action="store_true",
        help="刷新存量：补齐落后于最新交易日的旧文件（旧文件先移到 data/cache_refresh_stale/）",
    )
    args = ap.parse_args()

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if args.refresh_stale:
        from data.rs_universe import load_universe_closes, trim_sparse_tail

        wide = load_universe_closes()
        if wide is None:
            print("没有宽表，先跑一次 build_universe_closes 或正常 live run")
            return 1
        todo = stale_symbols(trim_sparse_tail(wide))
        if args.limit:
            todo = todo[: args.limit]
        print(f"宽表最后交易日 {trim_sparse_tail(wide).index[-1].date()}，"
              f"落后于它的存量文件 {len(todo)} 个")
        if args.dry_run:
            print("样例：", todo[:20])
            return 0
        if not todo:
            print("没有落后的存量文件")
            return 0
        download(todo, as_of, batch=args.batch, sleep=args.sleep,
                 feed_name=args.feed, refresh=True, manifest_path=REFRESH_MANIFEST)
        print(f"  回滚命令：mv {REFRESH_BACKUP_DIR}/*.pkl {CACHE_DIR}/")
        return 0

    universe = load_universe()
    manifest = load_manifest()
    todo = pending_symbols(
        universe, CACHE_DIR, manifest,
        include_etfs=not args.no_etfs,
        include_adrs=not args.no_adrs,
    )
    if args.limit:
        todo = todo[: args.limit]

    print(f"名单 {len(universe['symbols'])} 个，cache 已有 {len(_existing_cache_symbols(CACHE_DIR))}，"
          f"清单已判定 {len(manifest)}，待下载 {len(todo)}")
    if args.dry_run:
        print("样例：", todo[:20])
        return 0
    if not todo:
        print("没有待下载的代码")
        return 0

    download(todo, as_of, batch=args.batch, sleep=args.sleep, feed_name=args.feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())