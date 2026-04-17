"""
update_universe_sp500.py
将 S&P 500 + Nasdaq 100 静态写入 UNIVERSE.md

安全设计：
1. 只替换板块 S 和板块 N 的内容，不碰其他板块
2. 港股台股必须放在 "## 操作说明" 之前（所有板块在同一区域）
3. 写入前后都做强制验证
"""

import json
import re

# ============================================================
# 1. 验证 UNIVERSE.md 结构
# ============================================================
with open("UNIVERSE.md", "r") as f:
    content = f.read()

REQUIRED_MARKERS = [
    "## 板块 S：标普 500",
    "## 板块 N：纳斯达克 100",
    "## 港股",
    "## 台股",
    "## 操作说明",
]

for marker in REQUIRED_MARKERS:
    if content.find(marker) < 0:
        raise ValueError(f"缺少必需 section：{marker}，拒绝写入以保护数据！")

# ============================================================
# 2. 提取现有港股和台股内容
# ============================================================
def extract_section(content, section_name):
    """提取指定 section 的完整内容（到下一个 ## 板块 之前）"""
    marker = f"## {section_name}"
    start = content.find(marker)
    if start < 0:
        return None
    # 找该 section 之后的所有内容，直到下一个 ## 板块 或 ## 操作说明
    rest = content[start + len(marker):]
    # 找到下一个 ##（出现在行首的）
    next_section = re.search(r'\n## ', rest)
    if next_section:
        end = start + len(marker) + next_section.start()
    else:
        end = len(content)
    return content[start:end]

# 提取港股 section
hk_section = extract_section(content, "港股")
tw_section = extract_section(content, "台股")

# ============================================================
# 3. 读取指数数据
# ============================================================
with open("data/index_cache.json") as f:
    cache = json.load(f)

sp500_syms = sorted(cache["sp500"]["symbols"])
ndx100_syms = sorted(cache["nasdaq100"]["symbols"])
ndx_only = sorted(set(ndx100_syms) - set(sp500_syms))

print(f"S&P 500: {len(sp500_syms)}")
print(f"Nasdaq 100: {len(ndx100_syms)}（其中 {len(ndx_only)} 只不在 S&P 500）")

# ============================================================
# 4. 替换板块 S
# ============================================================
sp500_marker = "## 板块 S：标普 500"
sp500_start = content.find(sp500_marker)
sp500_end = content.find("## 板块 N：", sp500_start)
if sp500_start < 0 or sp500_end < 0:
    raise ValueError("找不到板块 S 或板块 N 的位置")

sp500_new = "## 板块 S：标普 500 成分股（静态写入，Wikipedia 定期更新）\n\n"
sp500_new += "| 代码 | 公司 | 简介 |\n"
sp500_new += "|------|------|------|\n"
# 用代码做名称（Wikipedia 抓取成功率低，用 FALLBACK_NAMES 补常用）
FALLBACK_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "GOOGL": "Alphabet",
    "TSLA": "Tesla", "AMZN": "Amazon", "META": "Meta", "BRK-B": "Berkshire Hathaway",
    "AVGO": "Broadcom", "XOM": "Exxon Mobil", "UNH": "UnitedHealth", "LLY": "Eli Lilly",
    "JPM": "JPMorgan Chase", "V": "Visa", "MA": "Mastercard", "JNJ": "Johnson & Johnson",
    "PG": "Procter & Gamble", "HD": "Home Depot", "ABBV": "AbbVie", "MRK": "Merck",
    "BAC": "Bank of America", "KO": "Coca-Cola", "PEP": "PepsiCo", "COST": "Costco",
    "WMT": "Walmart", "MCD": "McDonald's", "CSCO": "Cisco", "ACN": "Accenture",
    "AMD": "AMD", "QCOM": "Qualcomm", "TXN": "Texas Instruments", "AMAT": "Applied Materials",
    "NOW": "ServiceNow", "CRM": "Salesforce", "ADBE": "Adobe", "PYPL": "PayPal",
    "NFLX": "Netflix", "INTC": "Intel", "IBM": "IBM", "DIS": "Walt Disney",
    "UBER": "Uber", "SNOW": "Snowflake", "DDOG": "Datadog", "TEAM": "Atlassian",
    "NET": "Cloudflare", "CRWD": "CrowdStrike", "ZS": "Zscaler", "MDB": "MongoDB",
    "OKTA": "Okta", "PANW": "Palo Alto Networks", "FTNT": "Fortinet",
    "ISRG": "Intuitive Surgical", "DXCM": "Dexcom", "IDXX": "IDEXX",
    "REGN": "Regeneron", "VRTX": "Vertex", "MRNA": "Moderna",
    "GILD": "Gilead", "AMGN": "Amgen", "PFE": "Pfizer",
}
for sym in sp500_syms:
    name = FALLBACK_NAMES.get(sym, sym)
    sp500_new += f"| {sym} | {name} | S&P 500 成分股 |\n"

new_content = content[:sp500_start] + sp500_new + "\n" + content[sp500_end:]

# ============================================================
# 5. 替换板块 N
# ============================================================
ndx_marker = "## 板块 N：纳斯达克 100"
ndx_start = new_content.find(ndx_marker)
# 找港股或台股 section（板块 N 之后第一个非美股板块）
for next_marker in ["## 港股", "## 台股"]:
    next_pos = new_content.find(next_marker, ndx_start)
    if next_pos > ndx_start:
        ndx_end = next_pos
        break
else:
    ndx_end = new_content.find("## 操作说明", ndx_start)

ndx_new = "## 板块 N：纳斯达克 100 成分股（静态写入，Wikipedia 定期更新）\n\n"
ndx_new += "| 代码 | 公司 | 简介 |\n"
ndx_new += "|------|------|------|\n"
for sym in ndx_only:
    name = FALLBACK_NAMES.get(sym, sym)
    ndx_new += f"| {sym} | {name} | Nasdaq 100 成分股 |\n"

final_content = new_content[:ndx_start] + ndx_new + "\n" + new_content[ndx_end:]

# ============================================================
# 6. 更新总数
# ============================================================
import re
us_rows = len(sp500_syms) + len(ndx_only)
# 港股：从板块 N 末尾到台股之前
ndx_pos = final_content.find("## 板块 N：")
hk_start = final_content.find("## 港股：", ndx_pos)
tw_start = final_content.find("## 台股")
hk_text = final_content[hk_start:tw_start] if hk_start > 0 and tw_start > 0 else ""
tw_text = final_content[tw_start:] if tw_start > 0 else ""
hk_rows = len(re.findall(r'^\| [0-9A-Z]', hk_text, re.MULTILINE))
tw_rows = len(re.findall(r'^\| [0-9]', tw_text, re.MULTILINE))
total_all = us_rows + hk_rows + tw_rows

# 替换整行 header，不只是数字部分
lines = final_content.split('\n')
for i, line in enumerate(lines):
    if line.startswith('## 当前手动池总数：'):
        lines[i] = f"## 当前手动池总数：{total_all} 只（含美股{us_rows} + 港股{hk_rows} + 台股{tw_rows}）"
        break
final_content = '\n'.join(lines)

# ============================================================
# 7. 写入前最终验证（防止覆盖操作说明）
# ============================================================
for marker in REQUIRED_MARKERS:
    if final_content.find(marker) < 0:
        raise ValueError(f"写入后缺少 {marker}，数据可能被破坏！拒绝保存！")

# ============================================================
# 8. 写入文件
# ============================================================
with open("UNIVERSE.md", "w") as f:
    f.write(final_content)

# ============================================================
# 9. 写入后验证
# ============================================================
with open("UNIVERSE.md", "r") as f:
    verify = f.read()
for marker in REQUIRED_MARKERS:
    if verify.find(marker) < 0:
        raise ValueError(f"写入后验证失败：缺少 {marker}！请立即检查文件！")

print(f"\n完成！")
print(f"  S&P 500: {len(sp500_syms)} 只")
print(f"  Nasdaq 100 独有: {len(ndx_only)} 只")
print(f"  港股: {hk_rows} 只")
print(f"  台股: {tw_rows} 只")
print(f"文件已保存：UNIVERSE.md")
