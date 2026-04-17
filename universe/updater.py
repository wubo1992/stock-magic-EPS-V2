"""
universe/updater.py — 股票池自动扫描 & UNIVERSE.md 更新器

工作流程：
1. 构建"候选池"：Alpaca 自动池（universe_cache.json） + config 中的 extra_candidates
2. 排除已在 UNIVERSE.md 手动池中的股票（避免重复）
3. 对候选池逐一调用 SA Quant Rating API
4. 评分 >= 4.5（Strong Buy）的新股票，写入 UNIVERSE.md 的「自动扫描新增」节
5. 更新 UNIVERSE.md 顶部的手动池总数

UNIVERSE.md 自动扫描新增节格式：
  ## 板块：自动扫描新增
  | 代码 | 公司 | SA Quant | 加入日期 |
  | ADBE | Adobe | Strong Buy 4.85 | 2026-03-05 |
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .manager import _read_universe_md, CACHE_FILE, UNIVERSE_MD
from .sa_scanner import scan_tickers, STRONG_BUY_THRESHOLD

# 自动扫描新增节的标题（必须与解析逻辑一致）
AUTO_SECTION_HEADER = "## 板块：自动扫描新增"

# 手动池总数那一行的模式（用于更新）
_COUNT_RE = re.compile(r"^## 当前手动池总数：\d+ 只")


def _load_auto_cache() -> list[str]:
    """从 data/universe_cache.json 加载 Alpaca 新闻自动抓取的股票。"""
    if not CACHE_FILE.exists():
        return []
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return sorted(data.keys())
    except Exception:
        return []


def _has_auto_section(lines: list[str]) -> bool:
    """检查 UNIVERSE.md 中是否已有自动扫描新增节。"""
    return any(AUTO_SECTION_HEADER in line for line in lines)


def _ensure_auto_section(lines: list[str]) -> list[str]:
    """
    确保 UNIVERSE.md 中存在自动扫描新增节。
    如果不存在，在「待移出记录」节之前插入。
    返回更新后的行列表。
    """
    if _has_auto_section(lines):
        return lines

    # 找到「待移出记录」节或文末
    insert_pos = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("##") and "待移出记录" in line:
            insert_pos = i
            break

    new_section = [
        "\n",
        f"{AUTO_SECTION_HEADER}\n",
        "\n",
        "> 由 SA Quant 扫描程序自动添加（评分 ≥ 4.5 = Strong Buy）。\n",
        "> 可以随时将此处的股票移至对应板块（把该行复制过去，删掉此处即可）。\n",
        "\n",
        "| 代码 | 公司 | SA Quant | 加入日期 |\n",
        "|------|------|----------|--------|\n",
    ]

    return lines[:insert_pos] + new_section + lines[insert_pos:]


def _find_auto_section_end(lines: list[str]) -> int:
    """
    在 AUTO_SECTION_HEADER 之后，找到下一个 ## 节或文末，
    返回应该插入新行的位置（即空行之前/节标题之前）。
    """
    in_section = False
    for i, line in enumerate(lines):
        if AUTO_SECTION_HEADER in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            # 新节开始，插入到这里（跳过前面的 --- 分隔线）
            # 往前找 --- 行
            insert = i
            for j in range(i - 1, max(0, i - 4), -1):
                if lines[j].strip() == "---":
                    insert = j
                    break
            return insert
    return len(lines)


def _update_count_header(lines: list[str], new_count: int) -> list[str]:
    """更新 UNIVERSE.md 顶部的「当前手动池总数：N 只」行。"""
    result = []
    for line in lines:
        if _COUNT_RE.match(line.strip()):
            result.append(f"## 当前手动池总数：{new_count} 只\n")
        else:
            result.append(line)
    return result


def run_scan(config: dict, dry_run: bool = False) -> int:
    """
    执行一次完整的 SA Quant 扫描并更新 UNIVERSE.md。

    参数：
        config: 读取自 config.yaml 的完整配置字典
        dry_run: 为 True 时只打印结果，不修改文件

    返回：
        新增到 UNIVERSE.md 的股票数量
    """
    scan_cfg = config.get("scan", {})
    min_rating = scan_cfg.get("sa_quant_min", STRONG_BUY_THRESHOLD)
    min_cap_b = scan_cfg.get("min_market_cap_b", 0.5)
    max_new = scan_cfg.get("max_new_per_run", 30)
    extra = [s.upper() for s in scan_cfg.get("extra_candidates", [])]
    request_delay = scan_cfg.get("request_delay_seconds", 1.0)

    # ── 步骤 1：构建候选池 ──────────────────────────────────────
    auto_tickers = _load_auto_cache()
    candidates = sorted(set(auto_tickers) | set(extra))
    print(f"[扫描] 候选池：{len(candidates)} 只"
          f"（自动 {len(auto_tickers)} + 额外 {len(extra)}）")

    # ── 步骤 2：排除已在手动池的股票 ───────────────────────────
    manual_pool = set(_read_universe_md())
    to_check = [t for t in candidates if t not in manual_pool]
    print(f"[扫描] 去除已在手动池的 {len(candidates) - len(to_check)} 只，"
          f"需检查 {len(to_check)} 只")

    if not to_check:
        print("[扫描] 没有新股票需要检查，退出。")
        return 0

    # ── 步骤 3：SA Quant 扫描 ──────────────────────────────────
    print(f"[扫描] 开始查询 SA Quant Rating（阈值 ≥ {min_rating}）...")
    strong_buy_results = scan_tickers(
        to_check,
        min_quant_rating=min_rating,
        request_delay=request_delay,
        verbose=True,
    )

    if not strong_buy_results:
        print("[扫描] 未发现新 Strong Buy 股票。")
        return 0

    # 限制每次新增数量
    if len(strong_buy_results) > max_new:
        print(f"[扫描] 发现 {len(strong_buy_results)} 只，限制最多新增 {max_new} 只")
        strong_buy_results = strong_buy_results[:max_new]

    print(f"\n[扫描] 准备新增 {len(strong_buy_results)} 只到 UNIVERSE.md：")
    for r in strong_buy_results:
        print(f"  {r.ticker:8s}  Quant {r.quant_rating:.2f}  ({r.rating_label})")

    if dry_run:
        print("[扫描] dry_run=True，不修改文件。")
        return len(strong_buy_results)

    # ── 步骤 4：更新 UNIVERSE.md ───────────────────────────────
    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    with open(UNIVERSE_MD, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # 确保自动扫描新增节存在
    lines = _ensure_auto_section(lines)

    # 找到插入位置（节末尾，在下一个 ## 或 --- 之前）
    insert_pos = _find_auto_section_end(lines)

    # 构建新行
    new_rows = []
    for r in strong_buy_results:
        row = (f"| {r.ticker} | | "
               f"{r.rating_label} {r.quant_rating:.2f} | {today_str} |\n")
        new_rows.append(row)

    lines = lines[:insert_pos] + new_rows + lines[insert_pos:]

    # 更新总数
    new_count = len(_read_universe_md()) + len(strong_buy_results)
    # 注意：_read_universe_md() 基于未更新的文件，加上新增数
    lines = _update_count_header(lines, new_count)

    with open(UNIVERSE_MD, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"\n[扫描] ✓ 已将 {len(strong_buy_results)} 只股票写入 UNIVERSE.md")
    print(f"[扫描] 新手动池总数：{new_count} 只")
    return len(strong_buy_results)
