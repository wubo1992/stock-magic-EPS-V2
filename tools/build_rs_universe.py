#!/usr/bin/env python3
"""
构建 RS 排名池的全市场名单 -> data/rs_universe.json

数据源（全部公开、无需鉴权）
--------------------------
1. api.nasdaq.com screener (download=true)：普通股 + sector + country + marketCap
   注意必须**按交易所分别请求** —— 传 "exchange=NASDAQ,NYSE,AMEX" 会返回
   rows: null（实测），拆成三次请求才拿得到 6,955 行。
2. nasdaqtrader.com SymDir（nasdaqlisted + otherlisted）：全部 13,294 条挂牌，
   带 ETF / Test Issue 标记。**ETF 只在这里有** —— screener 不含 ETF。

判定逻辑全在 data/rs_universe_list.py（可测、零网络），这里只负责下载和落盘。

用法：
    uv run python tools/build_rs_universe.py
    uv run python tools/build_rs_universe.py --max-age-days 7   # 只检查新鲜度
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.rs_universe_list import (  # noqa: E402
    load_nasdaq_screener_rows,
    load_symdir_rows,
    merge_sources,
)

SCREENER_URL = (
    "https://api.nasdaq.com/api/screener/stocks?exchange={exchange}&download=true"
)
SYMDIR_URLS = {
    "nasdaqlisted.txt": (
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    ),
    "otherlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
EXCHANGES = ("NASDAQ", "NYSE", "AMEX")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

OUT_PATH = ROOT / "data" / "rs_universe.json"
DEFAULT_MAX_AGE_DAYS = 7


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def download(into: Path) -> tuple[dict, dict]:
    """下载两源到 into，返回 (screener_rows, symdir_rows)。"""
    into.mkdir(parents=True, exist_ok=True)

    # screener 必须按交易所分开下
    all_rows: dict[str, dict] = {}
    for exchange in EXCHANGES:
        path = into / f"screener_{exchange}.json"
        if not path.exists():
            path.write_bytes(_fetch(SCREENER_URL.format(exchange=exchange)))
        rows = load_nasdaq_screener_rows(path)
        print(f"  screener {exchange}: {len(rows)} 行 -> {path.name}")
        all_rows.update(rows)

    for name, url in SYMDIR_URLS.items():
        path = into / name
        if not path.exists():
            path.write_bytes(_fetch(url))
        print(f"  symdir {name}: {path.stat().st_size:,} bytes")

    return all_rows, load_symdir_rows(into)


def build(out_path: Path = OUT_PATH, download_dir: Path | None = None) -> dict:
    tmp = download_dir or (out_path.parent / "rs_universe_src")
    print(f"[1/2] 下载名单源 -> {tmp}")
    screener, symdir = download(tmp)

    print("[2/2] 合并 + 过滤")
    merged = merge_sources(screener, symdir)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "api.nasdaq.com screener + nasdaqtrader SymDir",
        "symbols": merged,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    _report(merged, out_path)
    return payload


def _report(merged: dict, out_path: Path) -> None:
    kinds = Counter(v["security_type"] for v in merged.values())
    print(f"\n名单已写入 {out_path}（{out_path.stat().st_size / 1e6:.1f} MB）")
    print(f"  总计 {len(merged):,} 只")
    for kind, n in kinds.most_common():
        print(f"    {kind:16s} {n:6,d}")
    print(f"    {'其中 ADR':16s} {sum(v['is_adr'] for v in merged.values()):6,d}")
    print(f"    {'其中杠杆ETF':16s} {sum(v['is_leveraged'] for v in merged.values()):6,d}")

    sectors = Counter(v["gics_sector"] for v in merged.values() if not v["is_etf"])
    print("\n  普通股板块分布（GICS L1）：")
    for sector, n in sectors.most_common():
        print(f"    {sector:26s} {n:6,d}")
    print(
        f"\n  无板块（不进板块排名）: "
        f"{sum(1 for v in merged.values() if v['gics_sector'] == 'Unknown'):,}"
    )


def check_age(out_path: Path, max_age_days: int) -> int:
    """返回过期天数；文件不存在返回 -1。"""
    if not out_path.exists():
        return -1
    generated = json.loads(out_path.read_text(encoding="utf-8")).get("generated_at", "")
    try:
        ts = datetime.fromisoformat(generated)
    except ValueError:
        return -1
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).days


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument(
        "--check-age-only",
        action="store_true",
        help="只检查名单新鲜度，不重新下载（过旧只告警不阻断）",
    )
    ap.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    args = ap.parse_args()

    if args.check_age_only:
        age = check_age(args.out, args.max_age_days)
        if age < 0:
            print(f"[WARN] {args.out} 不存在或无法解析时间戳")
            return 1
        if age > args.max_age_days:
            print(f"[WARN] 名单已 {age} 天未刷新（阈值 {args.max_age_days} 天）")
            return 1
        print(f"[OK] 名单 {age} 天前生成")
        return 0

    build(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())