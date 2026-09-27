"""AI Moonshot Sniper — Strategy C: гибрид, фильтр скама + агрессивный выход."""
import numpy as np, joblib, sys
sys.path.insert(0, '.')
import config

MOONSHOT_CONFIG = {
    'initial_capital': 1000,
    'ai_threshold': 0.85,
    'min_liquidity': 15000,
    'max_age_minutes': 30,
    'min_holders': 20,
    'position_size_pct': 0.12,
    'max_positions': 5,
    'hard_stop_loss': -0.40,
    'trailing_stop_pct': 0.30,
    'take_profit_levels': [{'pct': 1.0, 'sell_pct': 0.30}, {'pct': 3.0, 'sell_pct': 0.30}, {'pct': 10.0, 'sell_pct': 0.40}],
}

class AIMoonshotStrategy:
    def __init__(self):
        try:
            self.filter = joblib.load('scam_filter_model.pkl')
            print("AI Moonshot: Scam filter загружен")
        except Exception as e:
            print(f"AI Moonshot: Filter пропущен ({e})")
            self.filter = None
        self.positions = {}
        self.total_pnl = 0
        self.peak_capital = MOONSHOT_CONFIG['initial_capital']

    def should_enter(self, token_data: dict) -> tuple:
        # Жёсткие фильтры
        if token_data.get('liquidity_usd', 0) < MOONSHOT_CONFIG['min_liquidity']:
            return False, f"Low liquidity: ${token_data.get('liquidity_usd', 0)}"
        if token_data.get('age_minutes', 999) > MOONSHOT_CONFIG['max_age_minutes']:
            return False, f"Too old: {token_data.get('age_minutes', 0)} min"
        if token_data.get('holders', 0) < MOONSHOT_CONFIG['min_holders']:
            return False, f"Too few holders: {token_data.get('holders', 0)}"

        # AI фильтр (только при высокой уверенности)
        if self.filter is not None:
            try:
                import numpy as np
                feat = np.array([[
                    token_data.get('dev_holding_pct', 0),
                    token_data.get('tx_velocity_1m', 0),
                    token_data.get('volume_to_liq_ratio', 0),
                    token_data.get('funded_from_cex', 0)
                ]])
                pred = self.filter.predict(feat)[0]
                prob = self.filter.predict_proba(feat)[0][1]
                if pred == 1:  # Скам
                    return False, f"AI blocked: scam confidence {prob:.0%}"
                if prob < MOONSHOT_CONFIG['ai_threshold']:
                    return False, f"AI confidence low: {prob:.0%}"
                return True, f"AI approved: confidence {prob:.0%}"
            except Exception as e:
                print(f"AI filter error: {e}")
                pass  # Продолжаем без фильтра

        return True, "Hard filters passed"

    def manage(self, token_data, current_price, mint):
        # Упрощённая логика для демонстрации
        # В полной версии это заменит run_scenario
        pass

print("AI Moonshot Strategy V1 загружен:")
print(f"  AI threshold: {MOONSHOT_CONFIG['ai_threshold']}")
print(f"  Position size: {MOONSHOT_CONFIG['position_size_pct']*100}%")
print(f"  TP levels: {MOONSHOT_CONFIG['take_profit_levels']}")
print(f"  Trailing: {MOONSHOT_CONFIG['trailing_stop_pct']*100}%")
print(f"  Stop loss: {MOONSHOT_CONFIG['hard_stop_loss']*100}%")

def calculate_dynamic_position(capital, confidence, base_pct=0.05):
    multiplier = 0.5 + (confidence * 1.5)
    position = capital * base_pct * multiplier
    return min(position, capital * 0.15)
