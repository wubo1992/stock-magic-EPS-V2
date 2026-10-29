"""Tests for 全市场名单构建（security_type 过滤 + Nasdaq sector -> GICS 映射）。

这些规则全是「静默失真」型的：漏掉一类只会让池子小一点，
但多收一类（warrant / unit / preferred）会让 RS 百分位被非股票稀释，
而日志里什么都看不出来。所以每条规则都要单独锁死。
"""
import pytest

from data.rs_universe_list import (
    NASDAQ_SECTOR_TO_GICS,
    classify_security,
    is_index_benchmark,
    load_nasdaq_screener_rows,
    load_symdir_rows,
    merge_sources,
    to_alpaca_symbol,
    to_ranking_symbol,
)


# ════════════════════════════════════════════════════════════════
# GICS 映射
# ════════════════════════════════════════════════════════════════


def test_every_nasdaq_sector_maps_to_a_known_gics():
    """Nasdaq 的 12 个板块必须全部有映射，否则整批票静默变 Unknown。"""
    from tools.sector_analysis import INDUSTRY_TO_GICS

    known = set(INDUSTRY_TO_GICS.values())
    nasdaq_sectors = {
        "Finance",
        "Consumer Discretionary",
        "Health Care",
        "Technology",
        "Industrials",
        "Real Estate",
        "Energy",
        "Utilities",
        "Basic Materials",
        "Consumer Staples",
        "Telecommunications",
    }
    unmapped = {s for s in nasdaq_sectors if NASDAQ_SECTOR_TO_GICS[s] not in known}
    assert not unmapped, f"这些 Nasdaq 板块没映射到项目的 GICS: {unmapped}"


def test_finance_maps_to_financials_not_finance():
    """
    Nasdaq 的 "Finance" 必须映射到 "Financials"。

    这是计划里点名的一条：把 Nasdaq 的 sector 直接写进 sector_cache.json 的话，
    "Finance" 根本不是 INDUSTRY_TO_GICS 的 key，会静默变成 Unmapped。
    """
    assert NASDAQ_SECTOR_TO_GICS["Finance"] == "Financials"


def test_miscellaneous_is_deliberately_unmapped():
    """Miscellaneous 在 GICS 里没有对应，映射成 Unknown 而不是随便塞一个板块。"""
    assert NASDAQ_SECTOR_TO_GICS["Miscellaneous"] == "Unknown"


def test_unknown_nasdaq_sector_returns_unknown():
    assert NASDAQ_SECTOR_TO_GICS[""] == "Unknown"
    assert NASDAQ_SECTOR_TO_GICS["Whatever"] == "Unknown"


# ════════════════════════════════════════════════════════════════
# 证券类型分类
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "name",
    [
        "Apple Inc. Common Stock",
        "Berkshire Hathaway Inc. Class B Common Stock",
        "Alibaba Group Holding Ltd - American Depositary Shares",
    ],
)
def test_common_stock_is_kept(name):
    assert classify_security(name)["security_type"] == "common_stock"


@pytest.mark.parametrize(
    "name",
    [
        "AbCellera Biologics Inc. Common Shares",
        "Aurora Cannabis Inc. Common Shares",
        "Afya Limited Class A Common Shares",
        "Elong Power Holding Limited Class A Ordinary Shares",
        "CTW Class A Ordinary Shares",
    ],
)
def test_common_shares_is_kept(name):
    """
    「Common Shares」必须和「Common Stock」等价。

    实测 screener 里有 392 只真实普通股（ABCL / ACB / ACHV / AEC / AFYA …）
    因为用了英式 "Common Shares" 而被判成非股票 —— 全是英国/加拿大注册的发行人。
    只认 "common stock" 会静默砍掉它们，而且一只都看不出来。
    """
    info = classify_security(name)
    assert info is not None and info["security_type"] == "common_stock"


@pytest.mark.parametrize(
    "name",
    [
        "SPDR S&P 500 ETF Trust",
        "iShares Core S&P 500 ETF",
        "Vanguard Total Stock Market ETF",
    ],
)
def test_plain_etf_is_kept_and_flagged(name):
    info = classify_security(name)
    assert info["security_type"] == "etf"
    assert info["is_etf"] is True


@pytest.mark.parametrize(
    "name",
    [
        "AAC.U Unit",
        "AAC.W Warrant",
        "BRKHW Warrant",
        "AACPW Warrant",
    ],
)
def test_warrants_and_units_are_dropped(name):
    """权证/单位不是股票，混进排名池会稀释真实股票的百分位。"""
    assert classify_security(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "Arbor Realty Trust 6.375% Series D Cumulative Redeemable Preferred",
        "AGNC Investment Corp - Depositary Shares Each Representing 1/1,000th",
        "Bank of America Corporation Depositary Shares each representing 1/40th",
    ],
)
def test_preferred_shares_are_dropped(name):
    """
    优先股必须剔掉，「Depositary Shares」不等于 ADR。

    混淆点：优先股几乎全叫 "Depositary Shares"，而 ADR 叫
    "American Depositary Shares"。用 'depositary' 一刀切会误放 196 只优先股进来。
    """
    assert classify_security(name) is None


def test_american_depositary_is_adr_not_preferred():
    """ADR 的判据是 "american depositary"，且要标 is_adr 以便单独统计。"""
    info = classify_security("Alibaba Group Holding Ltd - American Depositary Shares")
    assert info["security_type"] == "common_stock"
    assert info["is_adr"] is True


def test_plain_common_stock_is_not_adr():
    assert classify_security("Apple Inc. Common Stock")["is_adr"] is False


@pytest.mark.parametrize(
    "name",
    [
        "iShares 3+ Month Treasury Bond ETF",
        "abrdn Ultra Short Municipal Income Active ETF",
        "Nuveen Ultra Short Income ETF",
        "ProShares Ultra QQQ Equal Weight",
        "ProShares Ultra QQQ Top 30",
        "F/m Ultrashort Treasury Inflation-Protected Security (TIPS) ETF",
        "Angel Oak UltraShort Income ETF",
        "Vanguard Ultra-Short Treasury ETF",
    ],
)
def test_non_leveraged_ultra_etfs_survive(name):
    """
    「Ultra」不等于杠杆。

    实测纯 \\bUltra\\b 会误伤这 8 只无杠杆基金（AMUN / NUSB / EQQQ / QQUP /
    QQXL / RBIL / UYLD / VGUS）——全是被动债券、久期或等权产品。
    杠杆 ETF 的真实标记是 2x/3x/-1x/inverse/UltraPro/daily target/ultra bull|bear。

    统一按 SymDir 的 ETF 标记调用（生产路径就是这样），
    因为 EQQQ / QQUP / QQXL 的名字里压根没有 "ETF" 字样。
    """
    info = classify_security(name, is_etf=True)
    assert info is not None
    assert info["security_type"] == "etf"
    assert info["is_leveraged"] is False


@pytest.mark.parametrize(
    "name",
    [
        "ProShares UltraPro QQQ",
        "ProShares UltraPro Short QQQ",
        "Leverage Shares 2X Long AAPL Daily ETF",
        "Tradr 2X Short AAPL Daily ETF",
        "Direxion Daily AAPL Bull 2X ETF",
        "GraniteShares 3x Long SPY Daily ETF",
        "ProShares Short Nasdaq 100 Inverse ETF",
        "Defiance Daily Target 2X Long AMAT ETF",
    ],
)
def test_leveraged_etfs_are_detected(name):
    assert classify_security(name, is_etf=True)["is_leveraged"] is True


# ════════════════════════════════════════════════════════════════
# screener 的「裸公司名」
#
# 实测（2026-10-28，data/rs_universe_src/ 的真实数据）：
# screener 6,955 行里有 1,339 行被 classify_security 判成「不是股票」，
# 其中 446 行是权证/单位/权利（该丢），601 行是优先股/票据（该丢），
# 但剩下 **275 行是真股票** —— 名字后面什么都没跟：
#
#     Advance Auto Parts Inc.                     (AAP)
#     Apollo Commercial Real Estate Finance Inc   (ARI)
#     ARMOUR Residential REIT Inc.                (ARR)
#     Alphabet Inc. Class C Capital Stock         (GOOG)
#     Colliers International Group Inc. Subordinate Voting Shares (CIGI)
#
# 还有 17 行 ADR 因为拼写少个 y（"American Depository Shares"）被当优先股丢掉。
#
# 后果：名单里少了 386 只池内成员 → 板块分母少一大截
# （Health Care 实际约 1,300 只，名单里只有 941）。
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "name,symbol",
    [
        ("Advance Auto Parts Inc.", "AAP"),
        ("Apollo Commercial Real Estate Finance Inc", "ARI"),
        ("ARMOUR Residential REIT Inc.", "ARR"),
        ("Alphabet Inc. Class C Capital Stock", "GOOG"),
        ("Colliers International Group Inc. Subordinate Voting Shares", "CIGI"),
        ("ANI Pharmaceuticals Inc.", "ANIP"),
        ("Azenta Inc.", "AZTA"),
    ],
)
def test_screener_bare_company_name_is_still_common_stock(name, symbol):
    """
    screener 是交易所自己的**股票**名单，名字后面没写 "Common Stock"
    不代表它不是股票。认不出来就丢掉的话，ASML / GOOG / ARI / AAP 这些票
    根本不在名单里，板块排名分母跟着被低估。
    """
    info = classify_security(name, assume_equity=True)
    assert info is not None, f"{symbol}（{name}）被当成非股票丢掉了"
    assert info["security_type"] == "common_stock"


@pytest.mark.parametrize(
    "name,symbol",
    [
        ("ASML Holding N.V. New York Registry Shares", "ASML"),
        ("Baidu Inc. ADS", "BIDU"),
        ("BRBI BR Partners S.A. ADSs", "BRBI"),
        (
            "Ascentage Pharma Group International American Depository Shares",
            "AAPG",
        ),
        ("Genmab A/S ADS", "GMAB"),
    ],
)
def test_screener_adr_wordings_are_all_flagged_as_adr(name, symbol):
    """
    ADR 的写法实测有四种：American Depositary Shares / American Depository
    Shares（少个 y）/ ADS / New York Registry Shares。
    少认一种就少收一批 ADR，还会被误判成优先股丢掉。
    """
    info = classify_security(name, assume_equity=True)
    assert info is not None, f"{symbol}（{name}）被当成非股票丢掉了"
    assert info["security_type"] == "common_stock"
    assert info["is_adr"] is True, f"{symbol}（{name}）没被认成 ADR"


@pytest.mark.parametrize(
    "name",
    [
        "Hovnanian Enterprises Inc Dep Shr Srs A Pfd",
        "Bank of America Corporation Depositary Sh repstg 1/1000th Perp Pfd Ser E",
        "EPR Properties Series E Cumulative Conv Pfd Shs Ser E",
        "First Merchants Corporation Depository Shares",
        "Adamas Trust Inc. 9.125% Senior Notes Due 2030",
        "iPath Series B S&P 500 VIX Short-Term Futures ETN",
    ],
)
def test_screener_fallback_still_rejects_non_equities(name):
    """
    兜底收编「认不出来的名字」时，优先股/票据/ETN 必须照样剔掉。

    "Dep Shr Srs A Pfd" 这种缩写没有 "preferred" 字样，
    光靠关键词会漏 —— 必须显式认 Pfd / Dep Shr / % / ETN。
    """
    assert classify_security(name, assume_equity=True) is None


def test_screener_fallback_never_turns_a_leveraged_etf_into_a_common_stock():
    """杠杆 ETF 永远是 ETF，不能因为「认不出来」被兜底成普通股。"""
    info = classify_security("GraniteShares 2x Long SPY Daily ETF", assume_equity=True)
    assert info is not None
    assert info["security_type"] == "etf"
    assert info["is_leveraged"] is True


@pytest.mark.parametrize(
    "name",
    [
        "Synthetic Fixed-Income Securities Inc 6.375% (STRATS) Cl A-1",
        "Entergy Arkansas LLC First Mortgage Bonds 4.875% Series Due September 1 2066",
        "Lehman ABS 3.50 3.50% Adjustable Corp Backed Tr Certs GS Cap I",
        "Structured Products Corp 8.205% CorTS 8.205% Corporate Backed Trust Securities",
        "Liberty Latin America Ltd. 9.0% Fixed Rate Cumulative Perpetual Redeemable "
        "Series A Preference Shares",
    ],
)
def test_coupon_bearing_non_etfs_are_not_common_stocks(name):
    """
    带票息（%）的非 ETF 名字不是普通股。

    实测这 5 类混进名单了：优先股、抵押贷款债券、STRATS、ABS 证书、CorTS。
    它们没有 "preferred"/"warrant" 之类的显式标记，只有票息能认出来。
    （已核：现有 6,955 行里没有一只真股票的名字带 %。）
    """
    assert classify_security(name, assume_equity=True) is None


def test_etf_with_percent_in_its_name_stays_an_etf():
    """ETF 名字里的 % 是产品说明（"10% Structured Buffer"），不能因此被剔。"""
    info = classify_security(
        "Corgi U.S. Equities 10% Structured Buffer ETF - August Series",
        assume_equity=True,
    )
    assert info is not None
    assert info["security_type"] == "etf"


@pytest.mark.parametrize(
    "name,symbol",
    [
        (
            "MPLX LP Common Units Representing Limited Partner Interests",
            "MPLX",
        ),
        (
            "Plains All American Pipeline L.P. Common Units representing Limited "
            "Partnership Interests",
            "PAA",
        ),
        (
            "Alliance Resource Partners L.P. Common Units representing Limited "
            "Partners Interests",
            "ARLP",
        ),
        ("PermRock Royalty Trust Units of Beneficial Interest", "PRMT"),
    ],
)
def test_partnership_common_units_are_equity(name, symbol):
    """
    MLP 的「Common Units」是股权，不是 SPAC 那种壳单位。

    实测池里有 24 只这样的票（MPLX / PAA / WES / NGL / GEL / GLP / SUN…），
    一律按 "units" 剔掉的话，它们在报告里会变成「RS 算不出」。
    """
    info = classify_security(name, assume_equity=True)
    assert info is not None, f"{symbol}（{name}）被当成非股票丢掉了"
    assert info["security_type"] == "common_stock"


@pytest.mark.parametrize(
    "name",
    [
        "Apogee Acquisition Corp Units",
        "BrightSpring Health Services Inc. Tangible Equity Unit",
        "Duke Energy Corporation Corporate Units",
        "NextEra Energy Inc. 7.299% Corporate Units",
        "Flagstar Bank N.A. Bifurcated Option Note Unit Securities",
        "Archimedes Tech SPAC Partners II Co. Unit",
    ],
)
def test_shell_units_are_not_equity(name):
    """SPAC 单位 / 混合单位 / 公司债单位都不是股票。"""
    assert classify_security(name, assume_equity=True) is None


def test_symdir_mlp_common_unit_needs_no_assume_equity():
    """
    MLP 的标准写法是 "Common Units representing Limited Partner Interests"
    （SUN / USAC 等只在 SymDir 里，不在 screener 里）。

    没有 assume_equity 时也必须认成股票 —— 否则 SymDir 独有的 MLP 会被丢掉。
    """
    info = classify_security(
        "Sunoco LP Common Units representing limited partner interests",
        is_etf=False,
    )
    assert info is not None
    assert info["security_type"] == "common_stock"


def test_ads_tec_is_not_mistaken_for_an_adr():
    """
    "ADS-TEC ENERGY PLC Ordinary Shares" 里的 ADS 是公司名的一部分，
    不是 American Depositary Shares。不能靠 \\bADS\\b 乱认。
    """
    info = classify_security(
        "ADS-TEC ENERGY PLC Ordinary Shares", assume_equity=True
    )
    assert info is not None
    assert info["is_adr"] is False


def test_merge_sources_keeps_screener_bare_name_stock():
    """端到端：名字裸的普通股必须出现在合并结果里，且带上 screener 的板块。"""
    screener_rows = {
        "data": {
            "rows": [
                {
                    "symbol": "GOOG",
                    "name": "Alphabet Inc. Class C Capital Stock",
                    "sector": "Technology",
                }
            ]
        }
    }
    symdir = load_symdir_rows_from_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "GOOG|Alphabet Inc. Class C Capital Stock|Q|N|N|100|N|\n"
    )
    merged = merge_sources(load_nasdaq_screener_rows_from_obj(screener_rows), symdir)
    assert "GOOG" in merged, "裸公司名的普通股被合并阶段丢掉了"
    assert merged["GOOG"]["gics_sector"] == "Information Technology"


def test_merge_sources_keeps_symdir_etn_out_of_the_pool():
    """SymDir 独有的 ETN / 结构化票据不是股票，不能进排名池。"""
    symdir = load_symdir_rows_from_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "VXX|iPath Series B S&P 500 VIX Short-Term Futures ETN|Q|N|N|100|N|\n"
    )
    merged = merge_sources({}, symdir)
    assert "VXX" not in merged


# ════════════════════════════════════════════════════════════════
# 基准 ETF 自指
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("sym", ["SPY", "QQQ", "DIA", "IWM", "VOO", "ASHR", "EWT"])
def test_benchmark_etfs_are_recognized(sym):
    """
    基准 ETF 绝不能进自己的排名池。

    main.py 会强制注入 SPY/ASHR/EWT，而 SPY 在自己的排名池里 —— 自指偏差。
    """
    assert is_index_benchmark(sym) is True


def test_ordinary_ticker_is_not_a_benchmark():
    assert is_index_benchmark("AAPL") is False
    assert is_index_benchmark("SPYG") is False  # 前缀相同但不是基准


# ════════════════════════════════════════════════════════════════
# symbol 归一
# ════════════════════════════════════════════════════════════════


def test_dotted_symbol_keeps_dot_form_for_ranking():
    """排名池里必须存 BRK.B 原形（与 UNIVERSE.md 一致）。"""
    assert to_ranking_symbol("BRK.B") == "BRK.B"


def test_dollar_suffixed_preferred_is_rejected():
    """ABR$D 这类优先股代码不能进池子。"""
    assert to_ranking_symbol("ABR$D") is None


def test_slash_class_share_is_normalized_to_dot_form():
    """
    Nasdaq screener 用 `HEI/A`，SymDir 用 `HEI.A` —— 同一只票两个 key。

    不归一的后果：同一家公司（Heico / Berkshire / Brown-Forman）在排名池里
    被数成两只，分子分母都被污染。实测 screener 有 13 个斜杠代码，
    包括 BRK/A、BRK/B、BF/A、BF/B。
    """
    assert to_ranking_symbol("HEI/A") == "HEI.A"
    assert to_ranking_symbol("BRK/B") == "BRK.B"


def test_dot_and_slash_forms_collapse_to_one_entry(tmp_path):
    """两源合并后，同一只票只能留一条 —— 否则排名池里凭空多出一只。"""
    import json

    payload = {
        "data": {
            "rows": [
                {
                    "symbol": "BRK/B",
                    "name": "Berkshire Hathaway Inc. Class B Common Stock",
                    "sector": "Finance",
                }
            ]
        }
    }
    path = tmp_path / "s.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    symdir = load_symdir_rows_from_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "BRK.B|Berkshire Hathaway Inc. Class B Common Stock|Q|N|N|100|N|\n"
    )
    merged = merge_sources(load_nasdaq_screener_rows(path), symdir)
    assert list(merged) == ["BRK.B"], f"应合并成一条，实际: {sorted(merged)}"
    assert merged["BRK.B"]["gics_sector"] == "Financials"
    assert merged["BRK.B"]["source"] == "nasdaq_screener+symdir"


def test_unit_and_warrant_suffixes_are_rejected():
    assert to_ranking_symbol("AAC.U") is None
    assert to_ranking_symbol("AAC.W") is None


@pytest.mark.parametrize(
    "symbol",
    [
        "CDW",  # CDW Corporation
        "CHRW",  # C.H. Robinson Worldwide
        "AROW",  # Arrow Financial
        "ACIW",  # ACI Worldwide
        "AVNW",  # Aviat Networks
        "ARTW",  # Art's-Way Manufacturing
        "CHW",  # Calamos Global Dynamic Income Fund
    ],
)
def test_tickers_merely_ending_in_w_are_kept(symbol):
    """
    不能因为代码以 W 结尾就当权证 —— 实测 322 只 ticker 以 W 结尾，
    其中 86 只是真股票，包括 CDW、CHRW 这类大盘股。

    权证的可靠判据是名称（SymDir 的 Security Name 里写明 "Warrant"），
    不是代码尾缀。符号形态只拦带点的 `.U` / `.W` 这种无歧义的写法。
    """
    assert to_ranking_symbol(symbol) is not None


def test_alpaca_symbol_converts_dot_to_dash_only_at_fetch_time():
    """查 Alpaca 时才把 BRK.B 转成 BRK-B，落盘的一律是原形。"""
    assert to_alpaca_symbol("BRK.B") == "BRK-B"
    assert to_alpaca_symbol("AAPL") == "AAPL"


def test_normal_symbol_passes_through_unchanged():
    assert to_ranking_symbol("AAPL") == "AAPL"
    assert to_ranking_symbol("GOOGL") == "GOOGL"


# ════════════════════════════════════════════════════════════════
# 解析 + 合并
# ════════════════════════════════════════════════════════════════


def test_load_nasdaq_screener_rows_parses_real_payload(tmp_path):
    payload = {
        "data": {
            "rows": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc. Common Stock",
                    "sector": "Technology",
                    "country": "United States",
                    "marketCap": "3450000000000",
                },
                {
                    "symbol": "BRK.B",
                    "name": "Berkshire Hathaway Inc. Class B Common Stock",
                    "sector": "Finance",
                    "country": "United States",
                    "marketCap": "1000000000000",
                },
            ]
        }
    }
    import json

    path = tmp_path / "screener.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    rows = load_nasdaq_screener_rows(path)
    assert rows["AAPL"]["gics_sector"] == "Information Technology"
    assert rows["AAPL"]["market_cap"] == 3450000000000
    assert rows["BRK.B"]["gics_sector"] == "Financials"


def test_load_screener_handles_missing_sector(tmp_path):
    """sector 缺失（实测 656 行）不能抛异常，落 Unknown 即可。"""
    import json

    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"data": {"rows": [{"symbol": "ZZZZ", "name": "X Inc.", "sector": ""}]}}),
        encoding="utf-8",
    )
    rows = load_nasdaq_screener_rows(path)
    assert rows["ZZZZ"]["gics_sector"] == "Unknown"


def test_load_symdir_rows_reads_both_header_layouts(tmp_path):
    """
    nasdaqlisted.txt 和 otherlisted.txt 的表头不一样：
    前者是 Symbol|Security Name|...，后者是 ACT Symbol|Security Name|...
    """
    nasdaq = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "File Creation Time: 1022202603:03\n"
        "AAPL|Apple Inc. Common Stock|Q|N|N|100|N|\n"
        "TEST|Test Issue Inc. Common Stock|Q|Y|N|100|N|\n"
    )
    other = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "File Creation Time: 1022202603:03\n"
        "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|\n"
    )
    (tmp_path / "nasdaqlisted.txt").write_text(nasdaq, encoding="utf-8")
    (tmp_path / "otherlisted.txt").write_text(other, encoding="utf-8")

    rows = load_symdir_rows(tmp_path)
    assert rows["AAPL"]["is_etf"] is False
    assert rows["SPY"]["is_etf"] is True
    assert rows["SPY"]["exchange"] == "NYSE Arca"
    assert "TEST" not in rows, "Test Issue 必须剔掉"


def test_merge_sources_prefers_screener_sector_over_none(tmp_path):
    screener = {
        "data": {
            "rows": [
                {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "sector": "Technology"}
            ]
        }
    }
    import json

    path = tmp_path / "s.json"
    path.write_text(json.dumps(screener), encoding="utf-8")

    symdir = load_symdir_rows_from_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "AAPL|Apple Inc. Common Stock|Q|N|N|100|N|\n"
    )
    merged = merge_sources(load_nasdaq_screener_rows(path), symdir)

    assert "AAPL" in merged
    assert merged["AAPL"]["gics_sector"] == "Information Technology"
    assert merged["AAPL"]["source"] == "nasdaq_screener+symdir"


def test_merge_drops_non_stock_and_benchmarks():
    screener_rows = {
        "data": {
            "rows": [
                {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "sector": "Technology"},
                {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "sector": ""},
                {"symbol": "BRKHW", "name": "Berkshire Hathaway Inc. Warrant", "sector": ""},
            ]
        }
    }
    symdir = load_symdir_rows_from_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "AAPL|Apple Inc. Common Stock|Q|N|N|100|N|\n"
        "SPY|SPDR S&P 500 ETF Trust|Q|N|N|100|Y|\n"
        "BRKHW|Berkshire Hathaway Inc. Warrant|Q|N|N|100|N|\n"
    )
    merged = merge_sources(load_nasdaq_screener_rows_from_obj(screener_rows), symdir)
    assert set(merged) == {"AAPL"}, f"不该留下的都留下了: {sorted(merged)}"


def test_etf_carries_no_gics_sector():
    """
    ETF 没有 GICS 行业，必须落 Unknown —— 它不进板块排名。

    硬塞一个板块会让「板块排名」里混进一堆不可比的同类分母。
    （这里用 SCHD 而不是 SPY：SPY 是基准 ETF，本来就被剔在池子外。）
    """
    symdir = load_symdir_rows_from_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "SCHD|Schwimbg US Broad Market ETF|Q|N|N|100|Y|\n"
    )
    merged = merge_sources({}, symdir)
    assert merged["SCHD"]["gics_sector"] == "Unknown"
    assert merged["SCHD"]["is_etf"] is True


def test_leveraged_etf_flagged_in_merged_output():
    symdir = load_symdir_rows_from_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "TQQQ|ProShares UltraPro QQQ|Q|N|N|100|Y|\n"
    )
    merged = merge_sources({}, symdir)
    assert merged["TQQQ"]["is_leveraged"] is True


# ════════════════════════════════════════════════════════════════
# 名字里的关键词 ≠ 证券类别（实测 3 处误判）
# ════════════════════════════════════════════════════════════════
#
# 规则都是「按名字里的关键词」判的，而关键词会撞车：
#   · 「Preferred Bank」是**银行的名字**，不是优先股
#   · 「Global Depositary Shares」是 GDS（等价 ADR），不是优先股
#   · 「RAC/WS」里的 WS 是**权证**后缀，不是普通股
# 三处都实测过，犯错的��向还相反：前两处把真股票踢出池子，
# 最后一处把权证放进池子（权证混进横截面分母最伤，见 test_rs_pool_whitelist.py）。


def test_preferred_bank_common_stock_is_not_a_preferred_share():
    """
    「Preferred Bank - Common Stock」是普通股，不能因为名字里有 Preferred 就被踢掉。

    实测：PFBC（NASDAQ 上市的小型银行控股公司）名字里自带 "Preferred"，
    被优先股规则误杀 —— 这就是「按关键词一刀切」的经典代价。
    """
    kind = classify_security("Preferred Bank - Common Stock", assume_equity=True)
    assert kind is not None, "PFBC 是普通股，不该被优先股规则杀掉"
    assert kind["security_type"] == "common_stock"


def test_screener_declared_common_stock_wins_over_the_company_name():
    """名字同时含 "Preferred" 和 "Common Stock" 时，以股份类别声明为准。"""
    kind = classify_security(
        "Cohen & Steers Select Preferred and Income Fund, Inc. Common Stock",
        assume_equity=True,
    )
    assert kind is not None
    assert kind["security_type"] == "common_stock"


def test_real_preferred_stock_stays_excluded_even_when_it_says_common_stock():
    """
    反向保护：明确写了 "Preferred Series" / "Trust Preferred Securities" 的
    仍然是优先股，不能因为规则放宽而混进池子。
    """
    for name in (
        "MetLife, Inc. Preferred Series A Floating Rate",
        "SCE Trust II Trust Preferred Securities",
        "Flaherty & Crumrine Preferred and Income Securities Fund Incorporated",
    ):
        assert classify_security(name, assume_equity=True) is None, name


def test_global_depositary_shares_are_treated_as_adr():
    """
    「Global Depositary Shares（each representing ten shares of Common Stock）」
    是 GDS —— 和 ADR 一样是股权，只是存托机构不同。不能按 "Depositary Shares" 判成优先股。

    实测只有 IRS 一只，但规则要对：ADR 的写法实测有五种，漏一种就少收一批。
    """
    kind = classify_security(
        "IRSA Inversiones Y Representaciones S.A. Global Depositary Shares "
        "(Each representing ten shares of Common Stock)",
        assume_equity=True,
    )
    assert kind is not None, "GDS 是股权，不该被优先股规则杀掉"
    assert kind["is_adr"] is True
    assert kind["security_type"] == "common_stock"


def test_screener_slash_warrant_form_is_rejected():
    """
    screener 里 SPAC 的权证写成 `RAC/WS`（WS=warrant），
    `to_ranking_symbol` 只认 `.U` / `.W` 后缀，这种写法会漏过去。

    实测：RAC/WS = Rithm Acquisition Corp. 的权证，被当成普通股收进池子。
    """
    assert to_ranking_symbol("RAC/WS") is None
    assert to_ranking_symbol("RAC/W") is None
    assert to_ranking_symbol("RAC/R") is None
    assert to_ranking_symbol("RAC/U") is None
    # 类别股（A/B）必须照常保留
    assert to_ranking_symbol("BRK/B") == "BRK.B"
    assert to_ranking_symbol("AKO/A") == "AKO.A"


# 便于测试里直接喂解析好的对象
def load_symdir_rows_from_text(text: str) -> dict:
    """写到正确的文件名下 —— load_symdir_rows 只认这两个名。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "nasdaqlisted.txt"
        p.write_text(text, encoding="utf-8")
        return load_symdir_rows(Path(d))


def load_nasdaq_screener_rows_from_obj(payload: dict) -> dict:
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return load_nasdaq_screener_rows(p)