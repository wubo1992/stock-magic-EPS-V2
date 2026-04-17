"""
strategies/v_eps_v2/eps_v2_strategy.py — Weinstein Stage 2 + EPS 基本面过滤

继承 WeinsteinStrategy（复用 Stage 2 技术分析框架），
在 _check_entry 中添加 EPS 增长筛选条件：

筛选顺序：
  1. Weinstein Stage 2 技术条件（SMA150 上升 + 突破整理区 + 放量）
  2. EPS 增长检查（基本面过滤）
     - 无 EPS 数据（Finnhub/Alpha Vantage 均无）→ 跳过，不出信号
     - 有 EPS 数据 → 正常筛选

对应 config.yaml 的 strategies.v_eps_v2 段。
"""

from events import SignalEvent
from strategies.v_weinstein.weinstein_strategy import WeinsteinStrategy, Position
from data.fundamentals import check_eps_filter


class EPSV2Strategy(WeinsteinStrategy):
    """Weinstein Stage 2 + EPS 基本面策略"""

    strategy_id = "v_eps_v2"
    strategy_name = "Weinstein + EPS"

    def _check_entry(self, symbol, df, date):
        # 1. 执行 Weinstein Stage 2 技术分析
        signal_or_none = super()._check_entry(symbol, df, date)
        if signal_or_none is None:
            return None

        signal = signal_or_none

        # 2. Weinstein 技术条件通过后，检查 EPS 增长
        # check_eps_filter 内部会尝试从缓存/Finnhub/Alpha Vantage 获取数据
        eps_quarters = self.cfg.get("eps_quarters_required", 3)
        eps_yoy = self.cfg.get("eps_yoy_required", True)
        eps_qoq = self.cfg.get("eps_qoq_required", True)

        eps_passed, eps_reason = check_eps_filter(
            symbol,
            quarters=eps_quarters,
            require_yoy=eps_yoy,
            require_qoq=eps_qoq
        )

        if not eps_passed:
            # EPS 抓不到或筛选未通过，跳过
            return None

        # 3. EPS 通过后，自动建仓（live mode 下自动计算 $1000 建仓）
        # live mode 下：如果已有仓位但 entry_date 不是今天，视为 stale 数据，用新数据覆盖
        existing = self.positions.get(symbol)
        needs_update = (
            existing is None
            or (self.live_mode and existing.entry_date.date() != date.date())
        )
        if needs_update:
            entry_price = float(df["close"].iloc[-1])
            # live mode: 自动按 $1000 建仓（取整）
            shares = int(1000 / entry_price) if self.live_mode else 0
            self.positions[symbol] = Position(
                symbol=symbol,
                entry_price=entry_price,
                entry_date=date,
                highest_price=entry_price,
                shares=shares,
            )

        # 4. 全部通过，组装信号
        close = float(df["close"].iloc[-1])
        stop_loss = round(close * (1 - self.stop_loss_pct), 2)
        Weinstein_reason = signal.data.get("reason", "")

        # 从 Weinstein_reason 中提取量能部分（如 "2.5x均量"）
        vol_part = ""
        if "[量能]" in Weinstein_reason:
            vol_part = Weinstein_reason.split("[量能]")[-1].strip()
        else:
            vol_part = "达标"

        reason = (
            f"[EPS] {eps_reason} | "
            f"[Stage2] SMA{self.sma_long}上升{self.trend_lookback}天 | "
            f"[量能] {vol_part}"
        )

        return SignalEvent.create(
            symbol=symbol, timestamp=date, direction="buy",
            strength=signal.data["strength"], reason=reason, stop_loss=stop_loss,
        )
