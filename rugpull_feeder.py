import sqlite3
import pandas as pd
import json
import os
import xgboost as xgb
import joblib
import config

try:
    from supabase import create_client, Client
except ImportError:
    pass

def feed_rugs_and_retrain():
    print("🧹 Поиск скам-рагпулов в логах...")
    
    df_trades = pd.DataFrame()
    use_supabase = bool(getattr(config, 'SUPABASE_URL', None) and getattr(config, 'SUPABASE_KEY', None))
    
    if use_supabase:
        print("Подключение к Supabase...")
        supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        
        # Тянем из trades_pump
        res_pump = supabase.table("trades_pump").select("*").eq("status", "CLOSED").execute()
        df_pump = pd.DataFrame(res_pump.data) if res_pump.data else pd.DataFrame()
        
        # Тянем из trades_raydium
        res_raydium = supabase.table("trades_raydium").select("*").eq("status", "CLOSED").execute()
        df_raydium = pd.DataFrame(res_raydium.data) if res_raydium.data else pd.DataFrame()
        
        # Объединяем оба датафрейма для поиска рагпулов
        df_trades = pd.concat([df_pump, df_raydium], ignore_index=True)
    else:
        print("Используем локальную SQLite базу...")
        if os.path.exists("trade_journal.db"):
            with sqlite3.connect("trade_journal.db") as conn:
                df_trades = pd.read_sql_query("SELECT * FROM trades WHERE status='CLOSED'", conn)
                
    if df_trades.empty:
        print("Нет закрытых сделок.")
        return
        
    # Ищем убыток более $3.00 (при позиции $4)
    # Если столбец pnl или pnl_usd
    if 'pnl_usd' in df_trades.columns:
        rugs = df_trades[(df_trades['pnl_usd'] <= -3.0)].copy()
    elif 'pnl' in df_trades.columns:
        # Иногда pnl хранится как доля (например -0.99) или процент (-99)
        rugs = df_trades[(df_trades['pnl'] <= -3.0) | (df_trades['pnl'] <= -0.75)].copy()
    else:
        rugs = pd.DataFrame()
        
    print(f"Обнаружено {len(rugs)} рагпулов для обучения.")
    
    if rugs.empty:
        print("Пока нет рагпулов в базе.")
        return
        
    features_list = []
    for f_str in rugs['features']:
        try:
            if isinstance(f_str, str):
                features_list.append(json.loads(f_str))
            elif isinstance(f_str, dict):
                features_list.append(f_str)
            else:
                features_list.append({})
        except:
            features_list.append({})
            
    df_rugs_features = pd.DataFrame(features_list)
    df_rugs_features['target'] = 0  # СКАМ!
    
    # ---------------- ОБУЧАЕМ МОДЕЛЬ ДЛЯ PUMP.FUN ----------------
    if os.path.exists("pump_dataset.csv"):
        df_base = pd.read_csv("pump_dataset.csv")
        common_cols = [c for c in df_base.columns if c in df_rugs_features.columns]
        
        if 'target' not in common_cols:
            common_cols.append('target')
            
        if common_cols:
            # Убираем возможные дубликаты из common_cols
            common_cols = list(dict.fromkeys(common_cols))
            df_rugs_pump = df_rugs_features[common_cols]
            df_combined = pd.concat([df_base, df_rugs_pump], ignore_index=True)
            df_combined.to_csv("pump_dataset.csv", index=False)
            
            features = ["dev_holding_pct", "top_10_holding_pct", "tx_velocity_1m", "has_socials", "funded_from_cex"]
            for col in features:
                if col not in df_combined.columns:
                    df_combined[col] = 0.0
                    
            X = df_combined[features]
            y = df_combined['target']
            
            model = xgb.XGBClassifier(n_estimators=300, learning_rate=0.03, max_depth=5, random_state=42)
            weights = [10.0 if idx >= len(df_base) else 1.0 for idx in range(len(df_combined))]
                    
            model.fit(X, y, sample_weight=weights)
            model.save_model("pump_model.json")
            print("✅ pump_model.json обновлен! Нейросеть стала умнее.")

if __name__ == "__main__":
    feed_rugs_and_retrain()
