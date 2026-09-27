import time

import config


class MatureExitManager:
    """Exit-логика для зрелых монет (swing). Возвращает причину выхода или None."""

    @staticmethod
    def evaluate_exit(position, current_price):
        if not hasattr(position, 'entry_price_usd') or position.entry_price_usd <= 0:
            return None

        pnl_pct = (current_price - position.entry_price_usd) / position.entry_price_usd

        if not hasattr(position, 'max_price_usd') or position.max_price_usd <= 0:
            position.max_price_usd = current_price
        elif current_price > position.max_price_usd:
            position.max_price_usd = current_price

        now_ts = time.time()
        et = getattr(position, 'entry_time', now_ts)
        # entry_time может быть float (live) или datetime/Timestamp (бектест)
        if hasattr(et, 'timestamp'):
            try:
                et = et.timestamp()
            except Exception:
                et = now_ts
        try:
            minutes_held = (now_ts - float(et)) / 60
        except Exception:
            minutes_held = 0

        # Аварийный кэп ПЕРВЫМ: гэпы типа FIBONACCI -68% проскакивают обычный стоп
        if pnl_pct <= -0.30:
            return f"Mature Emergency Cap ({pnl_pct*100:.0f}%)"

        # Trailing stop for mature coins
        if hasattr(config, 'TRAILING_ACTIVATION_PCT') and hasattr(config, 'TRAILING_DISTANCE_PCT'):
            max_pnl_pct = (position.max_price_usd - position.entry_price_usd) / position.entry_price_usd
            if max_pnl_pct >= config.TRAILING_ACTIVATION_PCT:
                drop_from_max = (position.max_price_usd - current_price) / position.max_price_usd
                if drop_from_max >= config.TRAILING_DISTANCE_PCT:
                    return f"Mature Trailing (+{max_pnl_pct*100:.0f}% peak)"
                    
        # Scalp Trailing (для мелких профитов)
        if hasattr(config, 'TRAILING_ACTIVATION_PCT'):
            max_pnl_pct = (position.max_price_usd - position.entry_price_usd) / position.entry_price_usd
            if max_pnl_pct >= 0.15 and max_pnl_pct < config.TRAILING_ACTIVATION_PCT:
                drop_from_max = (position.max_price_usd - current_price) / position.max_price_usd
                if drop_from_max >= 0.05:
                    return f"Scalp Profit (peak +{max_pnl_pct*100:.0f}%)"

        # Hard stop loss
        if hasattr(config, 'STOP_LOSS_PCT') and pnl_pct <= config.STOP_LOSS_PCT:
            return f"Mature Stop Loss ({config.STOP_LOSS_PCT*100:.0f}%)"

        # Time exit - Stagnant: минус режем быстро (он тянет вниз),
        # мелкий плюс держим до STAGNANT_HOLD_MIN - даём ракете время (раньше резали и его)
        _loss_min = getattr(config, 'STAGNANT_LOSS_MIN', 7)
        _hold_min = getattr(config, 'STAGNANT_HOLD_MIN', 25)
        if minutes_held >= _loss_min and pnl_pct < 0:
            return f"Stagnant Loss Cut ({minutes_held:.0f}m)"
        if minutes_held >= _hold_min and pnl_pct < 0.05:
            return f"Stagnant Cut ({minutes_held:.0f}m)"
            
        # Old fallback
        if hasattr(config, 'TIME_EXIT_MINUTES') and hasattr(config, 'TIME_EXIT_PROFIT_REQ'):
            if minutes_held >= config.TIME_EXIT_MINUTES and pnl_pct < config.TIME_EXIT_PROFIT_REQ:
                return "Mature Time Exit"

        return None

class TrailingExitManager:
    def __init__(self, trail_pct=0.30, min_profit=0.50):
        self.trail_pct = trail_pct
        self.min_profit = min_profit
    
    def evaluate_exit(self, position, current_price):
        entry = position.entry_price_usd
        pnl = (current_price - entry) / entry
        max_pnl = (position.max_price_usd - entry) / entry
        
        # Hard stop -40% (MOONSHOT_CONFIG)
        if pnl <= -0.40:
            return "Moonshot Hard Stop (-40%)"
        
        # Частичный тейк после +50% минимального профита
        if max_pnl >= self.min_profit and not getattr(position, 'tp_1_hit', False):
            position.tp_1_hit = True
            return "Moonshot TP +50% (30% продано)"
        
        # Trailing 30% после первого TP
        if getattr(position, 'tp_1_hit', False):
            trail_stop = position.max_price_usd * (1 - self.trail_pct)
            if current_price <= trail_stop:
                return "Moonshot Trailing Stop (-30% от пика)"
        
        # Дополнительные уровни тейка (5x/20x/100x) — симулировано в backtest
        for pct, label in [(5.0, "5x"), (20.0, "20x"), (100.0, "100x")]:
            if max_pnl >= pct and not getattr(position, f'tp_{label}_hit', False):
                setattr(position, f'tp_{label}_hit', True)
                return f"Moonshot TP +{pct*100:.0f}% ({label})"
        
        return None
