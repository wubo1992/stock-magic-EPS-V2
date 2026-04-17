#!/bin/bash
# ============================================================
# run_daily.sh — 交易信号系统每日自动扫描脚本
#
# 触发方式：macOS launchd 每天北京时间 05:30（美股收盘后约30分钟）
# 手动运行：bash run_daily.sh
# 如需切换策略，修改下方 STRATEGY 变量
# ============================================================

STRATEGY="v1"   # 使用的策略 ID（对应 config.yaml 中 strategies: 下的键）

# 周末不运行（1=周一 … 7=周日）
DAY_OF_WEEK=$(date +%u)
if [ "$DAY_OF_WEEK" -ge 6 ]; then
    echo "$(date '+%Y-%m-%d %H:%M') 周末，跳过扫描" >> output/run.log
    exit 0
fi

# 切到项目目录（确保相对路径正确）
cd "$(dirname "$0")" || exit 1

# 创建当天日志文件
TODAY=$(date +%Y-%m-%d)
LOG_FILE="output/daily_${TODAY}.log"

echo "============================================" >> "$LOG_FILE"
echo "扫描时间：$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
echo "============================================" >> "$LOG_FILE"

# 运行实盘模式，把输出同时写到日志和控制台
PYTHONUNBUFFERED=1 uv run python -u main.py --mode live --strategy "$STRATEGY" 2>&1 | tee -a "$LOG_FILE"

# ── 解析结果，发送 macOS 通知 ──────────────────────────────

# 统计买入信号数量
BUY_COUNT=$(grep -c "买入" "$LOG_FILE" 2>/dev/null || echo 0)

if [ "$BUY_COUNT" -gt 0 ]; then
    # 提取股票代码（取最多3个，拼成一行）
    SYMBOLS=$(grep "买入" "$LOG_FILE" | grep -v "^=\|^\[" | awk '{print $2}' | head -3 | tr '\n' ' ')
    # 发送带声音的通知
    osascript -e "display notification \"📈 ${SYMBOLS}等 ${BUY_COUNT} 个买入信号\" with title \"交易信号系统 (${STRATEGY})\" subtitle \"${TODAY}\" sound name \"Glass\""
    echo "$(date '+%Y-%m-%d %H:%M') 发送通知：${BUY_COUNT} 个买入信号（${SYMBOLS}）" >> output/run.log
else
    # 无信号时也发一个安静通知
    osascript -e "display notification \"今日无买入信号，持仓检查完毕\" with title \"交易信号系统 (${STRATEGY})\" subtitle \"${TODAY}\""
    echo "$(date '+%Y-%m-%d %H:%M') 今日无买入信号" >> output/run.log
fi
