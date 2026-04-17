# Weinstein + EPS 美股交易信号系统

> 基于 Weinstein Stage 2 技术框架 + EPS 基本面筛选的事件驱动型交易信号系统

每天收盘后自动扫描 ~5000 只股票，输出 Weinstein Stage 2 突破 + EPS 增长双确认的买入/卖出信号。

**系统不自动下单，所有信号仅供参考。**

---

## 策略说明（v_eps_v2）

### 入场条件（需同时满足）

1. **Weinstein Stage 2 技术条件**
   - SMA150 处于上升趋势（过去 30 天持续上行）
   - 当前价格在 SMA150 均线上方
   - 价格突破过去 30 日整理区间高点
   - 成交量 ≥ 1.5 倍 20 日均量（放量确认）

2. **EPS 基本面筛选**
   - 近 2 季 EPS 同比正增长
   - 近 2 季 EPS 环比正增长

### 出场条件

- **固定止损**：入场价 × 92%
- **追踪止盈**：从最高点回落 15%
- **时间止损**：持仓 ≥ 60 天且涨幅 < 5%

---

## 快速开始

### 环境要求

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)（推荐）或 pip

### 安装

```bash
git clone <repo-url>
cd signal_system_v_eps_v2
uv sync
```

### 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，填入你的 Alpaca API Key
```

获取 API Key：
- [Alpaca](https://alpaca.markets/) — 免费行情数据（注册后获取 API Key + Secret Key）
- [Finnhub](https://finnhub.io/) — 免费 EPS 数据（注册后获取 API Token）
- [Alpha Vantage](https://www.alphavantage.co/) — EPS 备用数据源（免费 API Key）

### 运行实盘信号（收盘后）

```bash
uv run python main.py --mode live --strategy v_eps_v2
```

### 运行回测

```bash
uv run python main.py --mode backtest \
  --start 2021-01-01 --end 2026-03-01 \
  --strategy v_eps_v2
```

---

## 项目结构

```
signal_system_v_eps_v2/
├── main.py                     # 运行入口
├── config.yaml                 # 所有策略参数配置
├── UNIVERSE.md                 # 手动股票池（直接编辑生效）
├── strategies/
│   ├── v_weinstein/            # Weinstein Stage 2 父策略
│   │   └── weinstein_strategy.py
│   └── v_eps_v2/               # Weinstein + EPS 策略
│       └── eps_v2_strategy.py  # ← 发布重点
├── backtest/
│   └── engine.py               # 回测引擎
├── signals/
│   ├── generator.py            # 信号生成
│   ├── positions.py            # 持仓追踪
│   └── report.py               # Markdown 报告生成
├── data/
│   ├── fetcher.py              # 数据获取（Alpaca + Yahoo Finance）
│   ├── fundamentals.py         # EPS 数据获取 + 缓存
│   └── cache/                  # 本地缓存（不上传 Git）
└── universe/
    ├── manager.py             # 股票池管理
    └── index_fetcher.py       # S&P500/Nasdaq100/Russell2000 成分股
```

---

## 股票池

系统支持三种股票池来源，按 `config.yaml` 中的 `auto_universe` 配置自动合并：

- **手动股票池**：编辑 `UNIVERSE.md`
- **指数成分股**：S&P 500、Nasdaq 100、Russell 2000（Wikipedia 免费数据）
- **Alpaca 新闻**：每日增量发现

---

## 数据说明

运行后会首次下载 ~5000 只股票约 10 年的历史数据（约 1-2 小时，取决于网络），
之后每次运行仅增量更新，1-2 分钟内完成。

- **行情数据**：Alpaca IEX（免费）为主，Yahoo Finance 为备用
- **EPS 数据**：Finnhub（优先）+ Alpha Vantage（备用）

---

## 免责声明

本系统仅供学习和研究用途，所有输出均为技术信号，**不构成投资建议**。
使用者自行承担投资决策风险。
