import asyncio
import aiohttp
import sqlite3
import json
import pandas as pd
from datetime import datetime, timedelta
import config

try:
    from supabase import create_client, Client
except ImportError:
    pass

class ShadowTracker:
    def __init__(self, db_path="shadow_trades.db"):
        self.db_path = db_path
        self._rate_limit_lock = asyncio.Lock()
        
        self.use_supabase = bool(getattr(config, 'SUPABASE_URL', None) and getattr(config, 'SUPABASE_KEY', None))
        
        if self.use_supabase:
            self.supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
            print("☁️ [ShadowTracker] Подключен к Supabase PostgreSQL")
        else:
            self.init_db()

    def init_db(self):
        """Создает таблицы для Теневого логгера"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS shadow_log (
                    mint TEXT PRIMARY KEY,
                    rejected_at TIMESTAMP,
                    reason TEXT,
                    score REAL,
                    price_at_rejection REAL,
                    features TEXT,
                    ath_price REAL DEFAULT 0.0,
                    hypothetical_pnl REAL DEFAULT 0.0,
                    check_1h_done INTEGER DEFAULT 0,
                    check_4h_done INTEGER DEFAULT 0,
                    check_24h_done INTEGER DEFAULT 0
                )
            ''')

    def log_rejection(self, mint: str, reason: str, score: float, price: float, features: dict):
        """Хук для сохранения отбракованной монеты"""
        try:
            import numpy as np
            native_score = float(score)
            native_price = float(price)
            native_features = {}
            for k, v in features.items():
                if isinstance(v, np.generic):
                    native_features[k] = v.item()
                else:
                    native_features[k] = v

            if self.use_supabase:
                data = {
                    "mint": mint,
                    "rejected_at": datetime.utcnow().isoformat(),
                    "reason": reason,
                    "score": native_score,
                    "price_at_rejection": native_price,
                    "features": json.dumps(native_features),
                    "ath_price": 0.0,
                    "hypothetical_pnl": 0.0,
                    "check_1h_done": 0,
                    "check_4h_done": 0,
                    "check_24h_done": 0
                }
                # supabase upsert to avoid conflicts
                self.supabase.table("shadow_log").upsert(data).execute()
            else:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute('''
                        INSERT OR IGNORE INTO shadow_log (
                            mint, rejected_at, reason, score, price_at_rejection, features
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        mint, 
                        datetime.utcnow().isoformat(), 
                        reason, 
                        native_score, 
                        native_price, 
                        json.dumps(native_features)
                    ))
            print(f"👻 [Shadow Logger] Записан отказ: {mint} | Причина: {reason}")
        except Exception as e:
            print(f"Ошибка Shadow Logger: {e}")

    async def _fetch_dexscreener_prices(self, mints: list) -> dict:
        """Батч-цены: GeckoTerminal первым, DexScreener как fallback."""
        try:
            import market_data
            gt = await market_data.get_bulk_prices(mints[:30])
            if gt:
                return gt
        except Exception as e:
            print(f"GT bulk prices error (Shadow): {e}")
        prices = {}
        async with self._rate_limit_lock:
            # Ограничитель: 1 запрос в секунду для бесплатного API
            await asyncio.sleep(1.0)
            
            # DexScreener поддерживает до 30 адресов через запятую
            addresses = ",".join(mints[:30])
            url = f"https://api.dexscreener.com/latest/dex/tokens/{addresses}"
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=5) as response:
                        if response.status == 200:
                            data = await response.json()
                            for pair in data.get("pairs", []):
                                # Берем цену из пула Solana
                                if pair.get("chainId") == "solana":
                                    m = pair.get("baseToken", {}).get("address")
                                    p = float(pair.get("priceUsd", 0))
                                    if m and p > prices.get(m, 0):
                                        prices[m] = p
            except Exception as e:
                print(f"Ошибка DexScreener Fetch: {e}")
        return prices

    async def price_watcher_loop(self):
        """Асинхронный фоновый парсер (Price Watcher)"""
        print("👁️ [Shadow Watcher] Запущен фоновый парсер цен...")
        while True:
            try:
                now = datetime.utcnow()
                updates = []
                mints_to_check = []
                
                if self.use_supabase:
                    # Supabase fetch
                    response = self.supabase.table("shadow_log").select("*").eq("check_24h_done", 0).execute()
                    rows = response.data
                else:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.row_factory = sqlite3.Row
                        c = conn.cursor()
                        c.execute("SELECT * FROM shadow_log WHERE check_24h_done = 0")
                        rows = [dict(r) for r in c.fetchall()]

                for row in rows:
                    rejected_at = datetime.fromisoformat(row["rejected_at"])
                    elapsed = now - rejected_at
                    
                    needs_check = False
                    check_1h = row["check_1h_done"]
                    check_4h = row["check_4h_done"]
                    check_24h = row["check_24h_done"]
                    
                    if elapsed > timedelta(hours=24) and not check_24h:
                        needs_check, check_24h = True, 1
                    elif elapsed > timedelta(hours=4) and not check_4h:
                        needs_check, check_4h = True, 1
                    elif elapsed > timedelta(hours=1) and not check_1h:
                        needs_check, check_1h = True, 1

                    if needs_check:
                        mints_to_check.append(row["mint"])
                        updates.append({
                            "mint": row["mint"],
                            "price_at_rejection": row["price_at_rejection"],
                            "old_ath": row["ath_price"],
                            "check_1h": check_1h,
                            "check_4h": check_4h,
                            "check_24h": check_24h
                        })

                # Разбиваем на батчи по 30
                for i in range(0, len(mints_to_check), 30):
                    batch_mints = mints_to_check[i:i+30]
                    current_prices = await self._fetch_dexscreener_prices(batch_mints)
                    
                    for update in updates:
                        if update["mint"] in batch_mints:
                            mint = update["mint"]
                            current_p = current_prices.get(mint, 0)
                            new_ath = max(update["old_ath"], current_p)
                            
                            p_entry = update["price_at_rejection"]
                            pnl = ((new_ath - p_entry) / p_entry * 100) if p_entry > 0 else 0
                            
                            if self.use_supabase:
                                self.supabase.table("shadow_log").update({
                                    "ath_price": new_ath,
                                    "hypothetical_pnl": pnl,
                                    "check_1h_done": update["check_1h"],
                                    "check_4h_done": update["check_4h"],
                                    "check_24h_done": update["check_24h"]
                                }).eq("mint", mint).execute()
                            else:
                                with sqlite3.connect(self.db_path) as conn:
                                    conn.execute('''
                                        UPDATE shadow_log 
                                        SET ath_price = ?, hypothetical_pnl = ?, 
                                            check_1h_done = ?, check_4h_done = ?, check_24h_done = ?
                                        WHERE mint = ?
                                    ''', (
                                        new_ath, pnl, 
                                        update["check_1h"], update["check_4h"], update["check_24h"], 
                                        mint
                                    ))

            except Exception as e:
                print(f"Ошибка в цикле Shadow Watcher: {e}")
                
            await asyncio.sleep(300)

    def analyze_missed_opportunities(self):
        """Детектор аномалий (False Negative Analyzer)"""
        if self.use_supabase:
            response = self.supabase.table("shadow_log").select("*").execute()
            df = pd.DataFrame(response.data)
        else:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query("SELECT * FROM shadow_log", conn)
            
        if df.empty:
            print("Теневой лог пуст.")
            return

        # Ищем ракеты (>200% профита)
        rockets = df[df['hypothetical_pnl'] >= 200.0]
        
        print(f"\n📊 --- ОТЧЕТ УПУЩЕННЫХ ВОЗМОЖНОСТЕЙ (Opportunity Cost) ---")
        print(f"Всего отбраковано: {len(df)} токенов")
        print(f"Из них упущенных ракет (>200%): {len(rockets)} токенов")
        
        if not rockets.empty:
            print("\n🔍 Группировка ошибок по 'Причине отказа':")
            reasons = rockets.groupby('reason').size().sort_values(ascending=False)
            for reason, count in reasons.items():
                print(f" - {reason}: {count} упущенных ракет")
                
            print("\n📈 Средний AI Score упущенных ракет:", rockets['score'].mean())
            
    def export_for_retraining(self, filename="false_negatives_dataset.csv"):
        """Экспорт ракет для дообучения модели (target=1)"""
        if self.use_supabase:
            response = self.supabase.table("shadow_log").select("*").gte("hypothetical_pnl", 100.0).execute()
            df = pd.DataFrame(response.data)
        else:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query("SELECT * FROM shadow_log WHERE hypothetical_pnl >= 100.0", conn)
            
        if df.empty:
            print("Нет подходящих ракет для выгрузки.")
            return
            
        # Распаковываем JSON-фичи в колонки
        features_df = df['features'].apply(json.loads).apply(pd.Series)
        
        # Объединяем и ставим таргет
        export_df = pd.concat([df[['mint', 'reason', 'score', 'hypothetical_pnl']], features_df], axis=1)
        export_df['target'] = 1  # Это ракеты, которые модель должна научиться определять
        
        export_df.to_csv(filename, index=False)
        print(f"✅ Датасет упущенных ракет выгружен в {filename} (Строк: {len(export_df)})")

# Пример запуска
# if __name__ == "__main__":
#     tracker = ShadowTracker()
#     asyncio.run(tracker.price_watcher_loop())
