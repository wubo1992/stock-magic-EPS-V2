#!/usr/bin/env bash
# 每日一条命令：跑 livetest → 生成用户版日报 → 打印可贴片段落
#
#   ./daily.sh              # 用今天的 UTC 日期
#   ./daily.sh 2026-11-09   # 指定标日（重生成历史日报用）
#
# 标日口径与策略一致：signals.csv 的「日期」列 = 跑 live 的那个 UTC 日，
# 而行情数据 = 最近一个**已完成**的美股交易日。所以周一早上跑，标日是周一，
# 数据是上周五。
set -euo pipefail

cd "$(dirname "$0")"

DATE="${1:-$(date -u +%F)}"
LOG="output/livetest_${DATE}.log"

echo "=== [1/2] livetest（标日 ${DATE}）→ ${LOG} ==="
if ! uv run python main.py --mode live --strategy v_eps_v2 > "$LOG" 2>&1; then
  echo "❌ livetest 失败，日志尾部："
  tail -40 "$LOG"
  exit 1
fi
grep -E "^\[(总耗时|报告|信号日志|数据)\]" "$LOG" | tail -8 || true

echo
echo "=== [2/2] 用户版日报 ==="
uv run python tools/gen_user_report.py --date "$DATE"
