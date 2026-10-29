"""
RS 排名池的「全市场名单」构建（纯逻辑，零网络）。

网络部分全在 tools/build_rs_universe.py，这里只负责可测的判定规则。
理由：这些规则一旦写错，池子会**静默地**多收或少收一类标的 ——
RS Rating 是横截面百分位，分母里混进权证、优先股、被剔掉的杠杆 ETF，
整个 1-99 的刻度就偏了，而日志里什么都看不出来。

数据源（均为公开、无需鉴权）
--------------------------
- api.nasdaq.com screener：6,955 行**普通股**，带 sector / country / marketCap
- nasdaqtrader.com SymDir（nasdaqlisted + otherlisted）：13,294 行全部挂牌，
  带 `ETF` / `Test Issue` 标记，是 ETF 的唯一来源（screener 不含 ETF）

为什么不用 Alpaca get_all_assets 做名单：它只多给约 700 只，
而 screener + SymDir 合并去重后已覆盖全部 12,000+ 挂牌证券。
用 SDK 换 700 只不划算（且实测调用会 hang）。

为什么「American Depositary」必须和「Depositary Shares」分开
-----------------------------------------------------------
优先股几乎全叫 "Depositary Shares"（实测 196 只），
而 ADR 叫 "American Depositary Shares"（实测 282 只）。
用 'depositary' 一刀切会把 196 只优先股放进股票排名池。

为什么不能简单用 \\bUltra\\b 剔杠杆 ETF
--------------------------------------
实测 Nasdaq 上有 8 只无杠杆产品的名字里带 "Ultra"：
abrdn Ultra Short Municipal / Nuveen Ultra Short Income / Vanguard Ultra-Short
Treasury / Angel Oak UltraShort / ProShares Ultra QQQ Equal Weight（等权）
/ ProShares Ultra QQQ Top 30（因子）/ ProShares UltraShort Nasdaq Biotech。
真正的杠杆标记是 2x/3x/4x/-1x/inverse/UltraPro/daily target/ultra bull|bear。
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

# ── Nasdaq sector（12 个 + Miscellaneous）→ 项目在用的 GICS L1（11 个）──
# 名字对不上的只有 4 个，其余一一对应。
# Miscellaneous 在 GICS 里没有对应板块 —— 落 Unknown，别硬塞。


class _GicsSectorMap(dict):
    """查不到的 Nasdaq 板块一律落 Unknown。

    走 __missing__ 而不是到处 .get(..., "Unknown")：映射表只写已知项，
    新增板块名忘了补也能安全退化，不会静默 KeyError 崩在名单构建里。
    """

    def __missing__(self, key: str) -> str:
        return "Unknown"
NASDAQ_SECTOR_TO_GICS: dict[str, str] = _GicsSectorMap(
    {
        "Technology": "Information Technology",
        "Finance": "Financials",
        "Basic Materials": "Materials",
        "Telecommunications": "Communication Services",
        "Consumer Discretionary": "Consumer Discretionary",
        "Consumer Staples": "Consumer Staples",
        "Health Care": "Health Care",
        "Industrials": "Industrials",
        "Energy": "Energy",
        "Utilities": "Utilities",
        "Real Estate": "Real Estate",
        "Miscellaneous": "Unknown",
    }
)

# ── 基准 ETF：绝不能进自己的排名池（自指偏差）──
BENCHMARK_SYMBOLS: frozenset[str] = frozenset(
    {"SPY", "QQQ", "DIA", "IWM", "VOO", "ASHR", "EWT"}
)

# ── 杠杆/反向 ETF 标记 ──
# 注意没有裸的 \bUltra\b —— 见模块 docstring。
_LEVERAGED_RE = re.compile(
    r"\b(2x|3x|4x|-1x|-2x|inverse|ultrapro|daily target|ultra (bull|bear))\b"
)

# ── 非股票证券的名称特征 ──
# 用 \b 包边界，否则 "Warrants" 之类会连 "Unit" 一起误伤。
_WARRANT_RE = re.compile(r"\bwarrants?\b")
_UNIT_RE = re.compile(r"\bunits?\b")
_RIGHT_RE = re.compile(r"\brights?\b")
# 优先股有三种写法，缺一不可（实测）：
#   "Preferred"                —— Arbor Realty Trust ... Preferred
#   "Depositary Shares"        —— AGNC Investment Corp - Depositary Shares
#   "Dep Shr Srs A Pfd"        —— Hovnanian Enterprises Inc Dep Shr Srs A Pfd（缩写）
_PREFERRED_RE = re.compile(
    r"\b(preferred|pfd|depositary shares?|depository shares?|dep shr|"
    r"fixed-to-floating)\b"
)
# 票据 / ETN：优先股之外的另一种「不是股票」的东西。
# 实际靠 % 和 "Notes Due" 就能抓到，但 ETN 名字里两者都没有，得单认。
_NOTE_RE = re.compile(r"\bnotes?\b|\bdebentures?\b|\betns?\b")
# ── 「单位」有两种，不能一刀切 ─────────────────────────────────────
# SPAC 的 Units 是壳单位（不是股票）：Apogee Acquisition Corp Units。
# MLP 的 Common Units 是**股权**：MPLX / PAA / WES / NGL / GEL / GLP…
#   （"L.P." / "Limited Partnership" + Common Units，或信托的
#     "Units of Beneficial Interest"）
# 混为一谈的话，池子里 24 只大中盘 MLP 会变成「RS 算不出」。
_PARTNERSHIP_RE = re.compile(
    r"\b(l\.?p\.?|limited partners?hip|limited partner)\b", re.IGNORECASE
)
_EQUITY_UNIT_RE = re.compile(r"\bunits? of beneficial interest\b", re.IGNORECASE)
_NON_EQUITY_UNIT_RE = re.compile(
    r"\b(corporate units?|tangible equity units?)\b", re.IGNORECASE
)
# ADR 的写法实测有五种，少认一种就少收一批：
#   "American Depositary Shares"（标准）/ "American Depository Shares"（少个 y）
#   "ADS" / "ADSs" / "New York Registry Shares" / "Global Depositary Shares"（GDS）
# ADS 用大小写敏感的写法，免得把 "ADS-TEC ENERGY PLC" 当成 ADR。
_ADR_RE = re.compile(
    r"\bamerican (depositary|depository)\b"
    r"|\bnew york registry shares\b"
    r"|\bglobal depositary\b",
    re.IGNORECASE,
)
_ADS_RE = re.compile(r"(?<![A-Za-z0-9-])ADSs?(?![A-Za-z0-9-])")

# 保留 `^`/`$`/`：`/空格 → 非正常 ticker
_BAD_SYMBOL_RE = re.compile(r"[\s$^:]")


def is_index_benchmark(symbol: str) -> bool:
    """基准 ETF 必须排除，否则 SPY 会参与自己的排名池。"""
    return symbol in BENCHMARK_SYMBOLS


def to_ranking_symbol(symbol: str) -> str | None:
    """
    归一成排名池用的 ticker，形状不对就返回 None。

    保留 `BRK.B` 这种带点原形（与 UNIVERSE.md 一致）。
    `ABR$D`（优先股）、`AAC.U`（单位）、`AAC.W`（权证）一律拒绝。

    ⚠️ 不要用「代码以 W 结尾」判权证：实测 322 只 ticker 以 W 结尾，其中
    86 只是真股票 —— 包括 **CDW**（CDW Corporation）、**CHRW**（C.H. Robinson
    Worldwide）这类大盘股。权证的可靠判据是 SymDir 名称里写明的 "Warrant"，
    符号形态只拦带点的 `.U` / `.W` 这种无歧义写法。
    """
    if not symbol:
        return None
    # screener 里 SPAC 的权证/权利/单位写成 `RAC/WS` / `RAC/R` / `RAC/U`，
    # 类别股才写成 `BRK/B`（A/B 类）。实测 13 个带斜杠的代码里 12 个是类别股，
    # 唯一例外 RAC/WS = Rithm Acquisition Corp. 的权证 —— 不拦的话它会
    # 以普通股身份进排名池（权证的价格能翻几倍，RS 会被它顶穿）。
    if re.search(r"/(WS|W|R|U)$", symbol, re.IGNORECASE):
        return None
    # 两源对「同一家公司不同类别股」写法不一致：screener 用 `BRK/B`，
    # SymDir 用 `BRK.B`。不归一的话 Berkshire 会在排名池里被数成两只，
    # 分子分母一起被污染。统一到点号（与 UNIVERSE.md 一致）。
    symbol = symbol.replace("/", ".")
    if _BAD_SYMBOL_RE.search(symbol):
        return None
    # 单位 / 权证的无歧义写法：点后缀 AAC.U / AAC.W
    if re.search(r"\.[UW]$", symbol):
        return None
    return symbol


def to_alpaca_symbol(symbol: str) -> str:
    """查 Alpaca 时才把点换成连字符；落盘的一律是原形。"""
    return symbol.replace(".", "-")


def _is_equity_unit(name: str) -> bool:
    """名字里带 "unit" 的证券，是不是「股权型单位」（MLP / 信托）而不是壳单位。"""
    low = name.lower()
    if "%" in name or _PREFERRED_RE.search(low) or _NON_EQUITY_UNIT_RE.search(low):
        return False
    if _EQUITY_UNIT_RE.search(low):
        return True
    return bool(_PARTNERSHIP_RE.search(low))


def _declares_common_stock(low: str) -> bool:
    """
    名字里**明确声明了股份类别**是普通股吗？

    用来豁免优先股规则：`Preferred Bank - Common Stock`（PFBC，实测 NASDAQ 上市）
    名字里的 "Preferred" 是**银行的名字**，不是股份类别；不看声明就一刀切，
    会把真股票踢出池子。

    `depositary shares` 不豁免 —— 优先股的存托凭证也写 "Common Stock"
    以外的普通词，但它们仍然是优先股（ADR/GDS 走的是另一条 is_adr 分支，
    根本不进这里）。
    """
    if "depositary share" in low or "depository share" in low:
        return False
    return "common stock" in low or "common share" in low


def classify_security(
    name: str, is_etf: bool | None = None, assume_equity: bool = False
) -> dict | None:
    """
    按名称判定证券类型，不是「普通股或 ETF」就返回 None。

    返回 {"security_type", "is_etf", "is_adr", "is_leveraged"}。
    `is_etf` 传入时以交易所标记为准 —— SymDir 的 ETF 列是权威的，
    比从名字猜靠谱（"ProShares Ultra QQQ Equal Weight" 这种名字里根本没 ETF 字样，
    但它确实是 ETF）。

    `assume_equity` 只给 screener 用
    -------------------------------
    screener 是交易所自己的**股票**名单（api.nasdaq.com/api/screener/stocks），
    里面 6,955 行里有 275 行的名字后面什么都没跟（"Advance Auto Parts Inc."、
    "Alphabet Inc. Class C Capital Stock"…），另有 17 行 ADR 因为拼写少个 y
    （"American Depository Shares"）被当成优先股丢掉。

    认不出名字 ≠ 不是股票，所以 screener 行走兜底：只要名字里没有
    权证/单位/权利/优先股/票据/ETN 这些明确的「非股票」标记，就按普通股收。
    非股票证券的判据是**名称里的显式标记**（SymDir 的 Security Name 就带
    "Warrant" / "Units" / "Preferred"），不是「像不像公司名」。

    SymDir 独有的行**不**走兜底：实测它 414 行认不出来的全是
    权证/单位/权利（+ 21 行 ETN/结构化票据），没有一只该收的股票。
    """
    if not name:
        return None
    low = name.lower()
    is_adr = bool(_ADR_RE.search(name)) or bool(_ADS_RE.search(name))
    looks_like_etf = " etf" in low or low.endswith("etf")
    etf_flag = is_etf if is_etf is not None else looks_like_etf

    # 1) 先剔非股票（混进排名池会稀释真实股票的百分位）。
    #    ADR 从这里跳过：它的名字也含 "Depositary Shares"。
    if not is_adr:
        if _WARRANT_RE.search(low) or _RIGHT_RE.search(low):
            return None
        if _UNIT_RE.search(low) and not _is_equity_unit(name):
            return None
        # 优先股：名字里出现 "Preferred" 之类的标记。
        # 例外是「名字明确声明了普通股类别」—— 见 _declares_common_stock。
        if _PREFERRED_RE.search(low) and not _declares_common_stock(low):
            return None
        if _NOTE_RE.search(low):
            return None
        # 带票息（%）的非 ETF 一律不是普通股：优先股 / 抵押贷款债券 /
        # STRATS / ABS 证书 / CorTS。ETF 不受影响 —— 名字里的 % 多是
        # "10% Structured Buffer" 这类产品说明。已核：6,955 行 screener 里
        # 没有一只真股票的名字带 %。
        if not etf_flag and "%" in name:
            return None

    # 2) ETF
    if etf_flag:
        return {
            "security_type": "etf",
            "is_etf": True,
            "is_adr": False,
            "is_leveraged": bool(_LEVERAGED_RE.search(low)),
        }

    # 3) ADR / 普通股
    #
    # ⚠️ "common shares" 必须和 "common stock" 等价：英国/加拿大注册的发行人在
    # 美股用的是英式写法，实测 screener 里有 392 只真实普通股（ABCL / ACB /
    # ACHV / AEC / AFYA …）只用 "Common Shares"。漏掉它会静默砍掉整批外资股。
    if (
        is_adr
        or "common stock" in low
        or "common share" in low
        or "ordinary share" in low
        or "capital stock" in low
        or "beneficial interest" in low
        or "voting share" in low
        # MLP 的标准写法（"Common Units representing Limited Partner Interests"）——
        # SymDir 独有的 MLP（SUN / USAC…）全靠这一条。
        or "common unit" in low
    ):
        return {
            "security_type": "common_stock",
            "is_etf": False,
            "is_adr": is_adr,
            "is_leveraged": False,
        }

    # 4) 认不出来 —— 保守起见不收；只有 screener 行（assume_equity）才兜底收编。
    if assume_equity:
        return {
            "security_type": "common_stock",
            "is_etf": False,
            "is_adr": is_adr,
            "is_leveraged": False,
        }
    return None


def load_nasdaq_screener_rows(path: Path) -> dict:
    """
    读 api.nasdaq.com screener 的 download=true 响应。

    返回 {symbol: {name, gics_sector, country, market_cap}}。
    sector 为空的（实测 656 行）落 Unknown，不抛异常。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = (payload.get("data") or {}).get("rows") or []

    out: dict[str, dict] = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        if not symbol:
            continue
        out[symbol] = {
            "name": (row.get("name") or "").strip(),
            "gics_sector": NASDAQ_SECTOR_TO_GICS.get(
                (row.get("sector") or "").strip(), "Unknown"
            ),
            "country": (row.get("country") or "").strip(),
            "market_cap": _parse_market_cap(row.get("marketCap")),
        }
    return out


def _parse_market_cap(raw) -> int:
    """marketCap 字段实测是字符串，缺失/脏值一律 0。"""
    if raw in (None, "", "N/A", "NA"):
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


# otherlisted.txt 的交易所代码 → 可读名字
_EXCHANGE_NAMES = {
    "A": "NYSE American",
    "F": "IEX",
    "M": "NYSE American (Arca)",
    "N": "NYSE",
    "P": "NYSE Arca",
    "V": "IEX",
    "Z": "Cboe BZX",
}


def load_symdir_rows(directory: Path) -> dict:
    """
    读 nasdaqtrader SymDir 的两个文件，合并成一张表。

    两份文件表头不同：nasdaqlisted.txt 用 `Symbol`，otherlisted.txt 用
    `ACT Symbol`。文件第一行是 `File Creation Time: ...`，DictReader 会把它
    当表头吃掉，后面要显式跳过。
    """
    merged: dict[str, dict] = {}
    for name in ("nasdaqlisted.txt", "otherlisted.txt"):
        merged.update(_load_symdir_file(Path(directory) / name))
    return merged


def _load_symdir_file(path: Path) -> dict:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh, delimiter="|"):
                row.pop("File Creation Time", None)
                symbol = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
                if not symbol or symbol.startswith("File Creation"):
                    continue
                # Test Issue = 交易所挂着但还不能交易的占位代码
                if (row.get("Test Issue") or "N").strip() == "Y":
                    continue
                out[symbol] = {
                    "name": (row.get("Security Name") or "").strip(),
                    "is_etf": (row.get("ETF") or "N").strip() == "Y",
                    "exchange": _EXCHANGE_NAMES.get(
                        (row.get("Exchange") or "").strip(), "NASDAQ"
                    ),
                }
    except Exception:
        return out
    return out


def merge_sources(screener: dict, symdir: dict) -> dict:
    """
    合并两源，返回最终的排名池名单。

    规则：
    - 只保留普通股与 ETF，其余（warrant/unit/preferred）全部剔除
    - 基准 ETF 剔除（自指偏差）
    - screener 的 sector 只对普通股有效；ETF 落 Unknown（无 GICS 行业，
      不进板块排名）
    - symdir 独有的（绝大多数是 ETF）靠 SymDir 的 ETF 标记补上分类
    """
    merged: dict[str, dict] = {}

    for symbol, scr in screener.items():
        norm = to_ranking_symbol(symbol)
        if norm is None or is_index_benchmark(norm):
            continue
        sym = symdir.get(norm) or symdir.get(symbol) or {}
        # screener 是交易所的股票名单 → 名字认不出来也按普通股收（assume_equity）
        kind = classify_security(
            scr["name"], is_etf=sym.get("is_etf", False), assume_equity=True
        )
        if kind is None or kind["security_type"] == "etf":
            # screener 理论上只给普通股；真出现 ETF 标记就走 symdir 分支统一处理
            continue
        merged[norm] = {
            "name": scr["name"],
            "security_type": kind["security_type"],
            "is_etf": False,
            "is_adr": kind["is_adr"],
            "is_leveraged": False,
            "gics_sector": scr["gics_sector"],
            "country": scr.get("country", ""),
            "market_cap": scr.get("market_cap", 0),
            "exchange": sym.get("exchange", "NASDAQ"),
            "source": "nasdaq_screener+symdir",
        }

    for symbol, sd in symdir.items():
        norm = to_ranking_symbol(symbol)
        if norm is None or is_index_benchmark(norm) or norm in merged:
            continue
        kind = classify_security(sd.get("name", ""), is_etf=sd.get("is_etf"))
        if kind is None:
            continue
        # symdir 独有的普通股几乎都是 ADR/优先股/warrant。screener 没覆盖到的
        # 普通股保留，但它们没有 sector —— 落 Unknown，不进板块排名。
        merged[norm] = {
            "name": sd.get("name", ""),
            "security_type": kind["security_type"],
            "is_etf": kind["is_etf"],
            "is_adr": kind["is_adr"],
            "is_leveraged": kind["is_leveraged"],
            # ETF 没有 GICS 行业 —— 落 Unknown，不进板块排名
            "gics_sector": "Unknown",
            "country": "",
            "market_cap": 0,
            "exchange": sd.get("exchange", "NASDAQ"),
            "source": "symdir",
        }
    return merged