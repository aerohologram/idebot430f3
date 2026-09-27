"""
🧠 AI Brain v2 — Встроенный Интеллект (Zero-API)
Все правила "Agent Gary" закодированы в математическую модель.
Без ключей, без API, без задержек. Работает мгновенно.
"""
import math


def ask_ai_oracle_sync(token_context: dict) -> dict:
    """
    Локальный ИИ-анализатор. Оценивает монету по 7 критериям,
    генерирует score от 0 до 100 и выносит вердикт BUY/SKIP.
    """
    symbol = token_context.get("symbol", "???")
    age = token_context.get("age_minutes", 0)
    liq = token_context.get("liquidity_usd", 0)
    vol = token_context.get("volume_24h_usd", 0)
    buys = token_context.get("m5_buys", 0)
    sells = token_context.get("m5_sells", 0)
    m5_pct = token_context.get("price_change_m5_pct", 0)
    h1_pct = token_context.get("price_change_h1_pct", 0)
    socials = token_context.get("social_networks_count", 0)
    rsi = token_context.get("rsi_14")
    macd_hist = token_context.get("macd_histogram")
    safety = token_context.get("safety_score", 0)

    score = 50  # Нейтральная отправная точка
    reasons = []

    # ============================
    # 1. КРИТИЧЕСКИЕ СТОП-ФИЛЬТРЫ (мгновенный SKIP)
    # ============================

    # Honeypot / Скам: нет соцсетей + мизерная ликвидность
    if socials == 0 and liq < 5000:
        return _skip(5, "Нет соцсетей + микро-ликвидность = 99% скам")

    # Жёсткий даунтренд
    if h1_pct < -25:
        return _skip(8, f"Дамп -{ abs(h1_pct):.0f}% за час. Ловить нож опасно")

    # RSI экстремумы
    if rsi is not None and not math.isnan(rsi):
        if rsi > 80:
            return _skip(10, f"RSI {rsi:.0f} — экстремальный перегрев. Откат неизбежен")
        if rsi < 20:
            return _skip(10, f"RSI {rsi:.0f} — мёртвый даунтренд. Монета сдохла")

    # Низкий safety score (RugCheck забраковал)
    if safety < 30:
        return _skip(12, f"Safety Score {safety}/100 — высокий риск скама")

    # ============================
    # 2. МОМЕНТУМ (до +30 баллов)
    # ============================
    buy_sell_ratio = buys / max(sells, 1)

    if buy_sell_ratio >= 3.0:
        score += 25
        reasons.append(f"Мощный моментум: покупки x{buy_sell_ratio:.1f}")
    elif buy_sell_ratio >= 2.0:
        score += 18
        reasons.append(f"Хороший моментум: покупки x{buy_sell_ratio:.1f}")
    elif buy_sell_ratio >= 1.3:
        score += 10
        reasons.append(f"Умеренный моментум: x{buy_sell_ratio:.1f}")
    elif buy_sell_ratio < 0.7:
        score -= 20
        reasons.append(f"Продажи доминируют: x{buy_sell_ratio:.1f}")
    elif buy_sell_ratio < 1.0:
        score -= 10
        reasons.append(f"Слабый моментум: x{buy_sell_ratio:.1f}")

    # Объём покупок за 5 минут
    if buys >= 50:
        score += 10
        reasons.append(f"Шквал покупок ({buys} за 5м)")
    elif buys >= 20:
        score += 5

    # ============================
    # 3. ЦЕНОВОЙ ТРЕНД (до +20 баллов)
    # ============================
    if 0 < m5_pct <= 15:
        score += 12
        reasons.append(f"Здоровый рост +{m5_pct:.1f}% за 5м")
    elif 15 < m5_pct <= 40:
        score += 8
        reasons.append(f"Быстрый рост +{m5_pct:.1f}% (осторожно)")
    elif m5_pct > 40:
        score -= 5
        reasons.append(f"Слишком быстрый рост +{m5_pct:.1f}% — риск отката")
    elif m5_pct < -10:
        score -= 15
        reasons.append(f"Падение {m5_pct:.1f}% за 5м")

    if 0 < h1_pct <= 50:
        score += 8
    elif h1_pct > 100:
        score -= 5
        reasons.append("Рост >100% за час — поздно входить")

    # ============================
    # 4. RSI / MACD (до +15 баллов)
    # ============================
    if rsi is not None and not math.isnan(rsi):
        if 40 <= rsi <= 60:
            score += 10
            reasons.append(f"RSI {rsi:.0f} — идеальная зона входа")
        elif 60 < rsi <= 75:
            score += 5
            reasons.append(f"RSI {rsi:.0f} — бычий, но ещё не перегрет")
        elif 30 <= rsi < 40:
            score += 3
            reasons.append(f"RSI {rsi:.0f} — возможный отскок")

    if macd_hist is not None:
        if macd_hist > 0:
            score += 8
            reasons.append("MACD бычий (гистограмма > 0)")
        elif macd_hist < 0:
            score -= 5

    # ============================
    # 5. 🎓 GRADUATION DETECTOR (до +20 баллов)
    # ============================
    # Pump.fun → Raydium миграция при капе ~$65k
    if 40000 <= liq <= 80000 and buy_sell_ratio >= 1.5:
        score += 20
        reasons.append("🎓 Близко к Graduation! FOMO-зона Pump→Raydium")
    elif 25000 <= liq < 40000 and buy_sell_ratio >= 2.0:
        score += 12
        reasons.append("Подход к Graduation с сильным моментумом")

    # ============================
    # 6. ЛИКВИДНОСТЬ И БЕЗОПАСНОСТЬ (до +10 баллов)
    # ============================
    if liq >= 50000:
        score += 8
    elif liq >= 20000:
        score += 5
    elif liq < 5000:
        score -= 10
        reasons.append(f"Микро-ликвидность ${liq:.0f} — высокий риск проскальзывания")

    if socials >= 3:
        score += 5
        reasons.append("Полный набор соцсетей")
    elif socials >= 1:
        score += 2

    if safety >= 70:
        score += 5
    elif safety >= 50:
        score += 2

    # ============================
    # 7. ВОЗРАСТ (штраф за слишком старые монеты)
    # ============================
    if age > 4320:  # >3 дня
        score -= 5
        reasons.append("Монета старше 3 дней — хайп мог пройти")

    # ============================
    # 8. МИКРОСТРУКТУРА КНИГИ И SMART MONEY
    # ============================
    unique_buyers = token_context.get("unique_buyers_m5", 0)
    smart_money_score = token_context.get("smart_money_inflow", 0)
    
    # Защита от Wash Trading (когда 1-2 кошелька накручивают объем и метрику buys)
    if buys > 20 and unique_buyers > 0 and unique_buyers < buys * 0.3:
        score -= 30
        reasons.append("🚨 Wash Trading: объем искусственно накручивается ботами создателя.")
        
    if unique_buyers > 50:
        score += 15
        reasons.append(f"🔥 Органик-спрос: {unique_buyers} уникальных холдеров за 5 мин.")
        
    if smart_money_score > 0:
        score += 25
        reasons.append("⚡ Зафиксированы покупки от кошельков из Smart Money.")

    # ============================
    # ВЕРДИКТ
    # ============================
    score = max(0, min(100, score))  # Ограничиваем 0-100

    if score >= 65:
        decision = "BUY"
        main_reason = " | ".join(reasons[:3]) if reasons else "Совокупность факторов положительная"
    else:
        decision = "SKIP"
        main_reason = " | ".join(reasons[:3]) if reasons else "Недостаточно сигналов для входа"

    print(f"🧠 AI Brain v2: {decision} (Score: {score}/100) | {main_reason[:120]}")
    return {"decision": decision, "confidence": score, "reason": main_reason[:150]}


def _skip(score: int, reason: str) -> dict:
    """Быстрый отказ с объяснением"""
    print(f"🧠 AI Brain v2: SKIP (Score: {score}/100) | {reason}")
    return {"decision": "SKIP", "confidence": score, "reason": reason}


import os

class AIBrainML:
    def __init__(self, model_path="pump_model.json", pro_model_path="pro_model.json"):
        self.model_path = model_path
        self.pro_model_path = pro_model_path
        self.model = None
        self.pro_model = None
        
        import xgboost as xgb
        if os.path.exists(self.model_path):
            try:
                self.model = xgb.XGBClassifier()
                self.model.load_model(self.model_path)
                print("🧠 [AI Brain] ML Модель (pump_model) успешно загружена!")
            except Exception as e:
                print(f"⚠️ [AI Brain] Ошибка загрузки ML модели: {e}")
                
        if os.path.exists(self.pro_model_path):
            try:
                self.pro_model = xgb.XGBClassifier()
                self.pro_model.load_model(self.pro_model_path)
                print("🧠 [AI Brain] PRO ML Модель (pro_model) успешно загружена!")
            except Exception as e:
                print(f"⚠️ [AI Brain] Ошибка загрузки PRO ML модели: {e}")
                
    def evaluate_pump_token(self, token_data: dict) -> dict:
        """
        Оценивает токен через ML-модель XGBoost с Hard-фильтрами.
        """
        if not self.model:
            return {"score": 0, "is_approved": False, "reasons": ["Модель не загружена"]}
            
        import pandas as pd
        
        # ================= HARD FILTERS =================
        dev_holding = token_data.get("dev_holding_pct", 0)
        top_10_holding = token_data.get("top_10_holding_pct", 0)
        
        if top_10_holding > 30.0:
            return {"score": 0, "is_approved": False, "reasons": [f"🚫 [HARD FILTER] Топ-10 держат {top_10_holding}%. Слишком высокий риск раг-пула."]}
            
        if dev_holding > 10.0:
            return {"score": 0, "is_approved": False, "reasons": [f"🚫 [HARD FILTER] Dev держит {dev_holding}%. Риск дампа."]}
        # ================================================
        
        # Подготовка фичей в том же порядке, что и при обучении
        features = {
            "dev_holding_pct": dev_holding,
            "top_10_holding_pct": top_10_holding,
            "tx_velocity_1m": token_data.get("tx_velocity_1m", 0),
            "has_socials": token_data.get("has_socials", 0),
            "funded_from_cex": token_data.get("funded_from_cex", 0)
        }
        
        df_features = pd.DataFrame([features])
        
        try:
            # predict_proba возвращает [prob_0, prob_1]
            success_probability = self.model.predict_proba(df_features)[0][1]
            score = int(success_probability * 100)
        except Exception as e:
            print(f"⚠️ ML Predict Error: {e}")
            score = 0
            
        reasons = []
        is_approved = False
        
        if score >= 60:  # Порог уверенности снижен до 70% для микро-депозитов
            is_approved = True
            reasons.append(f"✅ XGBoost уверен на {score}% в успехе (Pump -> Raydium).")
        else:
            reasons.append(f"❌ Низкая вероятность успеха: {score}%. Пропускаем.")
            
        return {
            "score": score,
            "is_approved": is_approved,
            "reasons": reasons
        }

    def evaluate_pro_model(self, df_features) -> dict:
        """
        Оценивает тиковые данные (micro-structure) через pro_model.pkl
        """
        if not self.pro_model:
            return {"score": 0, "is_approved": False, "reasons": ["pro_model.pkl не найдена или не загружена"]}
            
        try:
            # В датафрейме берем последнюю строку (самый свежий тик)
            expected_cols = ['volume_buy', 'volume_sell', 'tx_count', 'ofi', 'ofi_ema_5', 'total_vol', 'vol_change', 'vol_acceleration', 'volatility_15m', 'momentum_5m', 'momentum_15m']
            
            # ЗАЩИТА ОТ СДВИГА ДАННЫХ (Data Leakage)
            for col in expected_cols:
                if col not in df_features.columns:
                    df_features[col] = 0.0
                    
            X = df_features[expected_cols]
            last_row = X.iloc[-1:]
            
            prob = self.pro_model.predict_proba(last_row)[0][1]
            score = int(prob * 100)
            
            if score >= 60:
                return {"score": score, "is_approved": True, "reasons": [f"✅ [PRO ИИ] Микроструктура одобрена (Уверенность: {score}%)!"]}
            else:
                return {"score": score, "is_approved": False, "reasons": [f"❌ [PRO ИИ] Низкий потенциал (Уверенность: {score}%)"]}
                
        except Exception as e:
            print(f"⚠️ Pro ML Predict Error: {e}")
            return {"score": 0, "is_approved": False, "reasons": [f"Ошибка Pro ML: {e}"]}

# Инициализируем ML-мозг как синглтон
ml_brain = AIBrainML()

# Обёртка для совместимости с async кодом в analyzer.py
async def ask_ai_oracle(token_context: dict) -> dict:
    """Async обёртка. ИИ работает локально, задержка = 0 мс."""
    
    # Если мы собираем данные для ML (есть нужные фичи), используем новую модель
    if "tx_velocity_1m" in token_context and ml_brain.model is not None:
        result = ml_brain.evaluate_pump_token(token_context)
        decision = "BUY" if result["is_approved"] else "SKIP"
        reason_str = " | ".join(result["reasons"])
        print(f"🤖 [ML XGBoost] Вердикт: {decision} (Score: {result['score']}%) | {reason_str}")
        return {"decision": decision, "confidence": result["score"], "reason": reason_str, "is_approved": result["is_approved"]}
        
    # Иначе используем старую rule-based систему
    return ask_ai_oracle_sync(token_context)

async def ask_pro_oracle(df_features) -> dict:
    return ml_brain.evaluate_pro_model(df_features)
