# Phase 5 — SA Quant 扫描器接入

**状态：已完成 ✓**
**日期：2026-03-05**
**目标：通过 Seeking Alpha Quant Rating 自动发现强势候选股，扩充股票池**

---

## 背景

Phase 4 完成多策略架构后，股票池仍依赖手动维护（145只）。
样本内每月仅 0.8 个信号，主要原因是**股票池覆盖不足**。

解决方案：接入 Seeking Alpha Quant Rating API，自动筛选 Quant Rating ≥ 4.5（Strong Buy）的股票，定期写入 UNIVERSE.md。

---

## 技术方案

### SA Quant Rating API
- 端点：`https://seekingalpha.com/api/v3/symbols/{TICKER}/ratings`
- 无需登录账号，但 SA 会 Cloudflare 限频
- 使用 `curl_cffi` 模拟 Chrome 120 浏览器请求绕过基本检测
- 返回字段：`quant_rating`（1.0-5.0），≥ 4.5 为 Strong Buy

### 扫描流程
```
构建候选池（手动池 + 自动池，排除已在 UNIVERSE.md 的）
    → 逐 ticker 查询 SA API
    → 评分 ≥ 4.5 → 标记为 Strong Buy
    → dry-run：只打印，不写文件
    → 正式运行：写入 UNIVERSE.md「自动扫描新增」板块
```

---

## TODO 清单

- [x] 新建 `universe/sa_scanner.py`（逐 ticker 查询 SA Quant Rating）
- [x] 新建 `universe/updater.py`（扫描结果写入 UNIVERSE.md）
- [x] `main.py` 新增 `--mode scan` 和 `--dry-run` 参数
- [x] `main.py` 实现 `run_scan_mode()`
- [x] 更新 `UNIVERSE.md`（新增「板块：自动扫描新增」节）
- [x] 更新 `USAGE.md`（5.3 SA Quant 扫描使用说明）
- [x] 更新 `CLAUDE.md`（文件结构添加 sa_scanner.py / updater.py）
- [x] 手动补录：用户截图 SA Strong Buy 列表（41 只），新增 37 只到 UNIVERSE.md
- [x] 总数更新：145 → 182

---

## 关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 请求方式 | curl_cffi Chrome120 | 绕过 SA Cloudflare 基本检测 |
| 限速 | 每次请求间隔约 1-2s | 避免被封 IP |
| 写入方式 | 追加到 UNIVERSE.md 专用板块 | 不破坏手动维护的板块结构 |
| 去重 | 写入前检查 UNIVERSE.md 已存在的 ticker | 避免重复 |
| dry-run 模式 | `--dry-run` 只打印不写入 | 先预览再决定 |
| Strong Buy 阈值 | ≥ 4.5 | SA Quant 定义的 Strong Buy 边界 |

---

## 已知限制

| 问题 | 说明 | 处理方式 |
|------|------|---------|
| HTTP 403 | SA 偶发 Cloudflare 拦截 | 等待重试，或用户手动截图告知 Claude |
| 覆盖率 | 只能查已知 ticker 的评分，无法全市场扫描 | 候选池来源于手动池 + Alpaca 新闻 |
| 评分滞后 | SA Quant 评分每周更新一次 | 每周运行一次 scan 即可 |

---

## 运行命令

```bash
# 预览（不写文件）
uv run python main.py --mode scan --dry-run

# 正式扫描写入 UNIVERSE.md
uv run python main.py --mode scan
```

---

## 验证结果

首次手动录入 37 只 SA Strong Buy 股票后：
- UNIVERSE.md 总数：145 → 182
- 扩池后重新回测（2020-01 ~ 2026-03 OOS）：样本外年化收益 57.6%，夏普 2.12，**6/6 全部达标**

---

## 本次新增文件

| 文件 | 类型 | 说明 |
|------|------|------|
| `universe/sa_scanner.py` | 新建 | SA Quant Rating 逐 ticker 查询 |
| `universe/updater.py` | 新建 | 扫描结果写入 UNIVERSE.md |
