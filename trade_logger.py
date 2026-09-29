import sqlite3
import json
import asyncio
from datetime import datetime, timedelta
import aiohttp
import config

try:
    from supabase import create_client, Client
except ImportError:
    pass

class TradeLogger:
    """
    Trade Journal (Сборщик опыта).
    Сохраняет фичи и уверенность модели при входе.
    Обновляет запись реальным PnL и причиной выхода.
    """
    def __init__(self, db_path="trade_journal.db"):
        self.db_path = db_path
        self.use_supabase = bool(getattr(config, 'SUPABASE_URL', None) and getattr(config, 'SUPABASE_KEY', None))
        
        if self.use_supabase:
            self.supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
            print("☁️ [TradeLogger] Подключен к Supabase PostgreSQL")
        # SQLite держим всегда: при падении Supabase запись уходит туда,
        # иначе fallback падает с no such table и сделка теряется.
        self._init_db()

    def _init_db(self):
        """Создает таблицы, если их нет."""
        with sqlite3.connect(self.db_path) as conn:
            for table in ["trades_pump", "trades_raydium"]:
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        mint TEXT PRIMARY KEY,
                        entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        features TEXT,
                        confidence REAL,
                        pnl REAL DEFAULT NULL,
                        exit_reason TEXT DEFAULT NULL,
                        status TEXT DEFAULT 'OPEN',
                        exit_time TIMESTAMP DEFAULT NULL,
                        post_exit_ath REAL DEFAULT 0.0,
                        missed_pnl REAL DEFAULT 0.0,
                        check_1h_done INTEGER DEFAULT 0,
                        check_4h_done INTEGER DEFAULT 0,
                        check_24h_done INTEGER DEFAULT 0
                    )
                """)

    async def log_entry(self, mint: str, features: dict, confidence: float, is_mature: bool = False):
        """
        Асинхронная запись входа в сделку. Не блокирует event loop.
        """
        table_name = "trades_raydium" if is_mature else "trades_pump"
        
        try:
            _feat = json.dumps(features)
        except Exception:
            _feat = json.dumps(str(features))
        def _insert_sqlite():
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO {table_name} (mint, features, confidence) VALUES (?, ?, ?)",
                    (mint, _feat, confidence)
                )
        async def _run():
            if self.use_supabase:
                try:
                    data = {
                        "mint": mint,
                        "features": _feat,
                        "confidence": confidence,
                        "status": "OPEN",
                        "entry_time": datetime.utcnow().isoformat()
                    }
                    await asyncio.to_thread(self.supabase.table(table_name).upsert(data).execute)
                    return
                except Exception as e:
                    _msg = str(e)
                    if "PGRST125" in _msg or "Invalid path" in _msg:
                        self.use_supabase = False
                        print("⚠️ [TradeLogger] Supabase недоступен (PGRST125) — пишу журнал в локальный SQLite.")
                    else:
                        print(f"⚠️ [TradeLogger] Supabase ENTRY fail ({type(e).__name__}) — пишу в SQLite.")
            await asyncio.to_thread(_insert_sqlite)
        try:
            await _run()
        except Exception as e:
            print(f"⚠️ [TradeLogger] ENTRY {mint[:12]}... не записан никуда: {type(e).__name__}: {e}")
            return
        print(f"🧠 [TradeLogger] Записан опыт входа для {mint} (Conf: {confidence}%) в {table_name}")

    async def log_exit(self, mint: str, pnl_pct: float, exit_reason: str, is_mature: bool = False):
        """
        Асинхронное обновление сделки после выхода.
        """
        table_name = "trades_raydium" if is_mature else "trades_pump"
        
        def _update_sqlite():
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    f"UPDATE {table_name} SET pnl = ?, exit_reason = ?, status = 'CLOSED', exit_time = ? WHERE mint = ?",
                    (pnl_pct, exit_reason, datetime.utcnow().isoformat(), mint)
                )
                return cur.rowcount
        async def _run():
            if self.use_supabase:
                try:
                    data = {
                        "pnl": pnl_pct,
                        "exit_reason": exit_reason,
                        "status": "CLOSED",
                        "exit_time": datetime.utcnow().isoformat()
                    }
                    await asyncio.to_thread(self.supabase.table(table_name).update(data).eq("mint", mint).execute)
                    return
                except Exception as e:
                    _msg = str(e)
                    if "PGRST125" in _msg or "Invalid path" in _msg:
                        self.use_supabase = False
                        print("⚠️ [TradeLogger] Supabase недоступен (PGRST125) — закрытие пишу в SQLite.")
                    else:
                        print(f"⚠️ [TradeLogger] Supabase EXIT fail ({type(e).__name__}) — пишу в SQLite.")
            n = await asyncio.to_thread(_update_sqlite)
            if n == 0:
                print(f"⚠️ [TradeLogger] EXIT без ENTRY в журнале: {mint[:12]}... (запись потеряна раньше)")
        try:
            await _run()
        except Exception as e:
            print(f"⚠️ [TradeLogger] EXIT {mint[:12]}... не записан: {type(e).__name__}: {e}")
            return
        print(f"🧠 [TradeLogger] Опыт закрыт для {mint}. PnL: {pnl_pct}% | {table_name}")


    async def _fetch_dexscreener_prices(self, mints: list) -> dict:
        """Батч-цены: GeckoTerminal первым, DexScreener bulk как fallback."""
        prices = {}
        if not mints: return prices

        try:
            import market_data
            prices = await market_data.get_bulk_prices(mints)
            if prices:
                return prices
        except Exception as e:
            print(f"GT bulk prices error (Trades): {e}")
        
        chunk_size = 30
        for i in range(0, len(mints), chunk_size):
            chunk = mints[i:i+chunk_size]
            url = f"{config.DEXSCREENER_SEARCH}{','.join(chunk)}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for pair in data.get("pairs", []):
                                if pair.get("chainId") == "solana":
                                    m = pair.get("baseToken", {}).get("address")
                                    p = float(pair.get("priceUsd", 0))
                                    if m and p > prices.get(m, 0):
                                        prices[m] = p
            except Exception as e:
                print(f"Ошибка DexScreener Fetch (Trades): {e}")
            await asyncio.sleep(1) # Rate limit
        return prices

    async def post_trade_watcher_loop(self):
        """Асинхронный фоновый парсер закрытых сделок (Post-Trade Analysis)"""
        print("🕵️‍♂️ [TradeLogger] Post-Trade Watcher запущен. Ищу ранние выходы...")
        
        while True:
            try:
                now = datetime.utcnow()
                mints_to_check = []
                updates_by_table = {"trades_pump": [], "trades_raydium": []}
                
                # Собираем данные из обеих таблиц
                for table in ["trades_pump", "trades_raydium"]:
                    rows = []
                    if self.use_supabase:
                        response = self.supabase.table(table).select("*").eq("status", "CLOSED").eq("check_24h_done", 0).execute()
                        rows = response.data
                    else:
                        with sqlite3.connect(self.db_path) as conn:
                            conn.row_factory = sqlite3.Row
                            c = conn.cursor()
                            c.execute(f"SELECT * FROM {table} WHERE status = 'CLOSED' AND check_24h_done = 0")
                            rows = [dict(r) for r in c.fetchall()]

                    for row in rows:
                        if not row.get("exit_time"):
                            continue
                            
                        try:
                            exit_time_str = row["exit_time"]
                            if "T" in exit_time_str:
                                exit_time_str = exit_time_str.replace("Z", "")
                                exit_time = datetime.fromisoformat(exit_time_str)
                            else:
                                exit_time = datetime.strptime(exit_time_str, "%Y-%m-%d %H:%M:%S")
                        except:
                            # Если формат даты кривой, пропускаем
                            continue
                            
                        elapsed = now - exit_time
                        
                        needs_check = False
                        check_1h = row.get("check_1h_done", 0)
                        check_4h = row.get("check_4h_done", 0)
                        check_24h = row.get("check_24h_done", 0)
                        
                        if elapsed > timedelta(hours=24) and not check_24h:
                            needs_check, check_24h = True, 1
                        elif elapsed > timedelta(hours=4) and not check_4h:
                            needs_check, check_4h = True, 1
                        elif elapsed > timedelta(hours=1) and not check_1h:
                            needs_check, check_1h = True, 1

                        if needs_check:
                            mints_to_check.append(row["mint"])
                            updates_by_table[table].append({
                                "mint": row["mint"],
                                "features": row.get("features", "{}"),
                                "post_exit_ath": float(row.get("post_exit_ath", 0.0) or 0.0),
                                "check_1h": check_1h,
                                "check_4h": check_4h,
                                "check_24h": check_24h
                            })
                
                if mints_to_check:
                    # Убираем дубликаты
                    mints_to_check = list(set(mints_to_check))
                    prices = await self._fetch_dexscreener_prices(mints_to_check)
                    
                    for table, updates in updates_by_table.items():
                        for update in updates:
                            mint = update["mint"]
                            current_price = prices.get(mint)
                            if not current_price:
                                continue
                                
                            new_ath = max(update["post_exit_ath"], current_price)
                            
                            # Извлекаем цену входа из features, чтобы посчитать упущенную прибыль
                            missed_pnl = 0.0
                            try:
                                features = json.loads(update["features"])
                                # Если у нас нет entry_price_usd, мы не сможем точно посчитать, но если есть (как в Swing):
                                # Ладно, для простоты считаем упущенный профит от цены входа, но нам нужна цена входа.
                                # В features она не всегда есть. Будем использовать просто max цену (new_ath).
                                # Или просто запишем new_ath, а PnL вычислим потом.
                            except:
                                pass
                                
                            if self.use_supabase:
                                data = {
                                    "post_exit_ath": new_ath,
                                    "check_1h_done": update["check_1h"],
                                    "check_4h_done": update["check_4h"],
                                    "check_24h_done": update["check_24h"]
                                }
                                self.supabase.table(table).update(data).eq("mint", mint).execute()
                            else:
                                with sqlite3.connect(self.db_path) as conn:
                                    conn.execute(f'''
                                        UPDATE {table} SET 
                                            post_exit_ath = ?, check_1h_done = ?, check_4h_done = ?, check_24h_done = ?
                                        WHERE mint = ?
                                    ''', (
                                        new_ath, update["check_1h"], update["check_4h"], update["check_24h"], mint
                                    ))

            except Exception as e:
                _msg = str(e)
                if self.use_supabase and ("PGRST125" in _msg or "Invalid path" in _msg):
                    self.use_supabase = False
                    print("⚠️ [TradeLogger] Supabase таблица недоступна (PGRST125 — проверь таблицы/URL в Supabase). Перешёл на локальный SQLite.")
                else:
                    print(f"Ошибка в цикле Post-Trade Watcher: {e}")

            await asyncio.sleep(300)


# Глобальный инстанс для использования в проекте
trade_logger = TradeLogger()
