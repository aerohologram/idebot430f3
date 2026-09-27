import asyncio
import json
import time
import websockets
import tornado.websocket
import tornado.iostream
import aiohttp
import pandas as pd
import numpy as np
import config
from ai_brain import ask_pro_oracle
from sol_price import get_sol_price_sync

class TokenTrackerState:
    def __init__(self, mint: str, symbol: str, trader_pubkey: str):
        self.mint = mint
        self.symbol = symbol
        self.trader_pubkey = trader_pubkey
        
        self.trades = []
        self.unique_buyers = set()
        self.buy_wallets = {}
        self.total_volume_sol = 0
        self.start_time = time.time()
        
        self.is_ai_evaluated = False
        self.is_entered = False
        self.ai_confidence = 0
        self.ml_features_dict = {}
        
        self.max_curve_progress = 0.0
        self.is_migrated = False # Попала на Raydium (ракета)
        self.is_dead = False # Прошло 30 мин, не мигрировала (скам)
        self.entry_price_sol = 0.0

class PumpFunSniper:
    def __init__(self, tracker=None):
        self.tracker = tracker
        self.running = False
        self.trackers = {} # mint -> TokenTrackerState

    async def evaluate_and_enter(self, state: TokenTrackerState):
        if not getattr(config, "SNIPER_ENTRIES_ENABLED", False):
            state.is_ai_evaluated = True
            return
        try:
            df = pd.DataFrame(state.trades)
            df['curve_sol_diff'] = df['curve_sol'].diff().fillna(0)
            
            df['volume_buy'] = np.where(df['type'] == 'buy', df['curve_sol_diff'].abs(), 0)

            df['curve_sol_diff'] = df['curve_sol'].diff().fillna(0)
            df['volume_sell'] = np.where(df['type'] == 'sell', df['curve_sol_diff'].abs(), 0)
            df['price'] = df['curve_sol'] / 1_000_000_000 
            
            total_vol = df['volume_buy'] + df['volume_sell']
            df['ofi'] = np.where(total_vol > 0, (df['volume_buy'] - df['volume_sell']) / total_vol, 0)
            df['ofi_ema_5'] = df['ofi'].ewm(span=5, adjust=False).mean()
            df['total_vol'] = total_vol
            df['vol_change'] = df['total_vol'].diff().fillna(0)
            df['vol_acceleration'] = df['vol_change'].diff().fillna(0)
            
            df['log_return'] = np.log(df['price'] / df['price'].shift(1).replace(0, np.nan)).fillna(0)
            df['volatility_15m'] = df['log_return'].rolling(window=min(15, len(df))).std() * np.sqrt(15)
            df['volatility_15m'] = df['volatility_15m'].fillna(0)
            
            df['momentum_5m'] = df['price'].pct_change(min(5, len(df)-1)).fillna(0)
            df['momentum_15m'] = df['price'].pct_change(min(15, len(df)-1)).fillna(0)
            df['tx_count'] = 1
            
            # --- DEEP FEATURE ENGINEERING (Micro-structure) ---
            buy_mask = df['type'] == 'buy'
            sell_mask = df['type'] == 'sell'
            
            buys_count = buy_mask.sum()
            sells_count = sell_mask.sum()
            df['sell_buy_count_ratio'] = sells_count / (buys_count + 1)
            
            total_buy_vol = df.loc[buy_mask, 'curve_sol_diff'].abs().sum()
            total_sell_vol = df.loc[sell_mask, 'curve_sol_diff'].abs().sum()
            df['sell_buy_vol_ratio'] = total_sell_vol / (total_buy_vol + 1e-9)
            
            df['trade_size_variance'] = df['curve_sol_diff'].abs().var()
            df['trade_size_variance'] = df['trade_size_variance'].fillna(0)
            
            # Time diffs
            df['time_diff_ms'] = df['timestamp'].diff() * 1000
            df['avg_time_between_trades_ms'] = df['time_diff_ms'].mean()
            df['avg_time_between_trades_ms'] = df['avg_time_between_trades_ms'].fillna(0)
            
            # Whale activity
            top_5_vol = df['curve_sol_diff'].abs().nlargest(5).sum()
            df['whale_activity_index'] = top_5_vol / (df['total_vol'].iloc[-1] + 1e-9) if len(df) > 0 else 0
            
            # Price acceleration (2nd derivative)
            df['price_vel'] = df['price'].diff().fillna(0)
            df['price_acceleration'] = df['price_vel'].diff().fillna(0)
            # ------------------------------------------------
            
            # --- ВНЕДРЕНИЕ АНТИ-СКАМ ФИЛЬТРОВ ОТ ПОЛЬЗОВАТЕЛЯ ---
            try:
                import aiohttp
                rpc_url = "https://mainnet.helius-rpc.com/?api-key=9efda6f4-fddb-42d3-a2b1-098bbbecd299"
                from http_client import get_session
                session = await get_session()
                async with session.post(rpc_url, json=payload, timeout=10) as resp:
                        data = await resp.json()
                        accounts = data.get("result", {}).get("value", [])
                        total_supply = 1_000_000_000
                        if accounts:
                            # Исключаем самый первый аккаунт (это всегда Bonding Curve на Pump.fun)
                            if len(accounts) > 1:
                                non_curve_accounts = [float(acc["uiAmount"]) for acc in accounts[1:]]
                                dev_holding_pct = (non_curve_accounts[0] / total_supply) * 100 if non_curve_accounts else 0
                                top_10_holding_pct = (sum(non_curve_accounts[:10]) / total_supply) * 100
                            else:
                                dev_holding_pct, top_10_holding_pct = 0, 0
                                
                            if dev_holding_pct > 7.0:
                                print(f"🚫 [DEV DUMP RISK] {state.symbol}: Создатель держит {dev_holding_pct:.1f}% (>7%). Пропуск!")
                                state.is_ai_evaluated = True
                                return
                                
                            if top_10_holding_pct > 30.0:
                                print(f"🚫 [SYBIL RISK] {state.symbol}: Топ-10 холдеров держат {top_10_holding_pct:.1f}% (>30%). Пропуск!")
                                state.is_ai_evaluated = True
                                return
            except Exception as e:
                print(f"⚠️ Ошибка проверки холдеров: {e}")
            # --- КОНЕЦ ФИЛЬТРОВ ---

            pro_res = await ask_pro_oracle(df)
            state.ai_confidence = pro_res.get("score", 0)
            is_pro_approved = pro_res.get("is_approved", False)
            
            for k, v in df.iloc[-1].to_dict().items():
                if isinstance(v, pd.Timestamp): state.ml_features_dict[k] = str(v)
                elif hasattr(v, 'item'): state.ml_features_dict[k] = v.item()
                else: state.ml_features_dict[k] = v
                
        except Exception as e:
            print(f"⚠️ Ошибка подготовки PRO фичей: {e}")
            is_pro_approved = False
            state.ai_confidence = 0

        state.is_ai_evaluated = True

        if is_pro_approved:
            print(f"✅ [PRO AI ОДОБРЕНО] {state.symbol} прошел анализ! Уверенность: {state.ai_confidence}%")
            state.is_entered = True
            
            from tracker import PaperTracker
            from exit_manager import ExitManager
            tracker = self.tracker if self.tracker else PaperTracker()
            exit_mgr = ExitManager(config.HELIUS_RPC_URL)
            
            sol_amount = state.trades[-1]["curve_sol"] if state.trades else 0
            # ИСПРАВЛЕНИЕ ВХОДНОЙ ЦЕНЫ: берем marketCapSol из последнего трейда
            market_cap_sol = state.trades[-1].get("market_cap_sol", 0) if state.trades else 0
            if market_cap_sol > 0:
                actual_price = (market_cap_sol / 1_000_000_000.0) * get_sol_price_sync()
            else:
                # Фоллбэк (хотя marketCapSol должен быть всегда)
                actual_price = (sol_amount / 1_000_000_000.0) * get_sol_price_sync() 
            
            capital = tracker.get_total_capital()
            base_position = capital * (config.REINVEST_PERCENT / 100.0)
            liq_usd = sol_amount * get_sol_price_sync() 
            max_allowed = liq_usd * 0.05
            position_size = max(4.0, min(base_position, max_allowed, 100.0))
            
            if position_size >= 4.0:
                print(f"🚀 PAPER СНАЙП {state.symbol}! Входим на {position_size}$")
                tracker.add_position(state.symbol, state.mint, actual_price, position_size, ml_features=state.ml_features_dict, ml_confidence=state.ai_confidence, source=f"SNIPER:{state.ai_confidence}%")
                
                async def panic_sell_callback(token_mint, reason):
                    pos = tracker.positions.get(token_mint)
                    if pos and pos.status == "open":
                        exit_price = pos.current_price_usd if pos.current_price_usd > 0 else pos.entry_price_usd
                        tracker.close_position(token_mint, exit_price, reason)
                        
                dev_wallet_pubkey = state.trader_pubkey if state.trader_pubkey else "11111111111111111111111111111111"
                asyncio.create_task(
                    exit_mgr.start_monitoring(
                        token_mint=state.mint,
                        dev_wallet=dev_wallet_pubkey, 
                        initial_dev_balance=1_000_000_000, 
                        on_panic_sell=panic_sell_callback
                    )
                )
        else:
            print(f"🚫 [AI ОТКАЗ] {state.symbol} (Уверенность: {state.ai_confidence}%). Следим для сбора метрик.")
            # === SHADOW TRADING HOOK ===
            try:
                from shadow_tracker import ShadowTracker
                shadow = ShadowTracker()
                current_price = (state.trades[-1]["curve_sol"] / 1_000_000_000.0) * get_sol_price_sync() if state.trades else 0
                shadow.log_rejection(
                    mint=state.mint,
                    reason=f"AI Score too low: {state.ai_confidence:.1f}%",
                    score=state.ai_confidence,
                    price=current_price,
                    features=state.ml_features_dict
                )
            except Exception as e:
                print(f"Ошибка вызова ShadowTracker: {e}")



    async def sync_fomo_positions(self, ws):
        """Подхватывает позиции, открытые FOMO-сканером, чтобы трекать их цены в реальном времени через вебсокет"""
        import asyncio
        from tracker import PaperTracker
        p_tracker = self.tracker if self.tracker else PaperTracker()
        
        while self.running:
            try:
                open_positions = p_tracker.get_open_positions()
                for mint, pos in open_positions.items():
                    if mint not in self.trackers:
                        print(f"🔗 [WSS SYNC] Подключаем лайв-трекинг для {pos.symbol} (куплен сканером)")
                        state = TokenTrackerState(mint, pos.symbol, "")
                        state.is_entered = True
                        self.trackers[mint] = state
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
            except Exception as e:
                print(f"Ошибка синхронизации WSS: {e}")
            await asyncio.sleep(3)

    async def garbage_collector(self, ws):
        """Очищает мертвые трекеры и отписывается от WSS"""
        while self.running:
            await asyncio.sleep(60)
            now = time.time()
            to_remove = []
            
            for mint, state in list(self.trackers.items()):
                # Если прошло 30 минут, считаем что токен умер
                if now - state.start_time > 1800:
                    state.is_dead = True
                    print(f"💀 [Очистка] Токен {state.symbol} мертв (30 мин без миграции).")
                    
                    # Если был отвергнут ИИ, логируем неудачу (True Negative)
                    if state.is_ai_evaluated and not state.is_entered:
                        pass
                        
                    to_remove.append(mint)
                    
            for mint in to_remove:
                del self.trackers[mint]
                try:
                    await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
                except:
                    pass

    async def connect_and_listen(self):
        self.running = True
        uri = config.PUMPPORTAL_WSS
        
        while self.running:
            try:
                print("🟢 Подключение к ЕДИНОМУ PumpPortal WSS...")
                headers = {
                    "User-Agent": "Mozilla/5.0",
                    "Origin": "https://pumpportal.fun"
                }
                async with websockets.connect(uri, extra_headers=headers) as ws:
                    # 1. Подписка на новые токены
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    print("🚀 Подписка на InitializeMint оформлена!")
                    
                    # 2. Запуск сборщика мусора и Shadow Watcher
                    asyncio.create_task(self.garbage_collector(ws))
                    asyncio.create_task(self.sync_fomo_positions(ws))
                    try:
                        from shadow_tracker import ShadowTracker
                        shadow = ShadowTracker()
                        asyncio.create_task(shadow.price_watcher_loop())
                    except Exception as e:
                        print(f"Ошибка запуска Shadow Watcher: {e}")
                    
                    while self.running:
                        message = await ws.recv()
                        data = json.loads(message)
                        
                        tx_type = data.get("txType")
                        
                        # --- НОВЫЙ ТОКЕН ---
                        if "mint" in data and tx_type == "create":
                            mint = data["mint"]
                            symbol = data.get("symbol", "UNKNOWN")
                            trader_pubkey = data.get("traderPublicKey", "")
                            initial_buy = data.get("initialBuy", 0)
                            
                            if initial_buy > 200_000_000:
                                continue # Пропускаем мега-дампы
                                
                            print(f"\n🔔 [НОВЫЙ ТОКЕН] {symbol} | Mint: {mint}")
                            
                            self.trackers[mint] = TokenTrackerState(mint, symbol, trader_pubkey)
                            # Динамически добавляем подписку на торги этого токена в ЭТОТ ЖЕ сокет
                            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
                            
                        # --- СДЕЛКА ПО ТОКЕНУ ---
                        elif tx_type in ["buy", "sell"]:
                            mint = data.get("mint")
                            state = self.trackers.get(mint)
                            if not state:
                                continue
                                
                            sol_amount = data.get("vSolInBondingCurve", 0)
                            if sol_amount == 0:
                                continue
                                
                            # Фиксация трейда
                            state.trades.append({
                                'timestamp': time.time(),
                                'type': tx_type,
                                'curve_sol': sol_amount,
                                'market_cap_sol': data.get("marketCapSol", 0),
                                'wallet': data.get('traderPublicKey')
                            })
                            state.unique_buyers.add(data.get('traderPublicKey'))
                            if tx_type == "buy":
                                state.buy_wallets[data.get('traderPublicKey')] = time.time()
                            
                            progress = (sol_amount / 85.0) * 100
                            if progress > state.max_curve_progress:
                                state.max_curve_progress = progress
                            
                            # Проверка на миграцию (Ракета)
                            if progress >= 100.0 and not state.is_migrated:
                                state.is_migrated = True
                                print(f"🚀🚀🚀 [РАКЕТА] Токен {state.symbol} мигрировал на Raydium!")
                                if state.is_ai_evaluated and not state.is_entered:
                                    # ИИ отверг, а она взлетела! Логируем (False Negative)
                                    pass
                                    
                                # Отписываемся, чтобы не засорять сокет Raydium торгами
                                del self.trackers[mint]
                                await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
                                continue

                            # Обновляем LIVE цену в трекере, если мы в позиции!
                            if state.is_entered:
                                from tracker import PaperTracker
                                from sol_price import get_sol_price_sync
                                p_tracker = self.tracker if self.tracker else PaperTracker()
                                pos = p_tracker.positions.get(mint)
                                if pos and pos.status == "open":
                                    # ИСПРАВЛЕНИЕ: берем marketCapSol, а не vSolInBondingCurve, для правильного расчета цены!
                                    market_cap_sol = data.get("marketCapSol", 0)
                                    if market_cap_sol > 0:
                                        live_price = (market_cap_sol / 1_000_000_000.0) * get_sol_price_sync()
                                        pos.current_price_usd = live_price
                                        pos.price_updated_at = time.time()
                                    if live_price > pos.max_price_usd:
                                        pos.max_price_usd = live_price
                                    # Рассчитываем PNL для логов (Stop-Loss все равно сработает в главном цикле, но быстрее)
                                    pnl_pct = (live_price - pos.entry_price_usd) / pos.entry_price_usd
                                    pos.current_pnl_usd = pos.amount_usd * pnl_pct
                                    p_tracker.save_portfolio()
                                    
                            
                            
                            
                            # Quarantine: Если достигли 80% (Proof of Traction ~ $20k MC), оцениваем ИИ
                            # Задержка 5 сек — минимальная защита от самых быстрых Block 0 бандлов.
                            time_alive = time.time() - state.start_time
                            if not state.is_ai_evaluated and progress >= 80.0 and len(state.trades) > 5 and time_alive >= 5.0:
                                cutoff = time.time() - config.SNIPER_BUYERS_WINDOW_MIN * 60
                                buyers_window = sum(1 for ts in state.buy_wallets.values() if ts >= cutoff)
                                if buyers_window < config.SNIPER_MIN_UNIQUE_BUYERS:
                                    continue
                                await self.evaluate_and_enter(state)
                                
            except websockets.exceptions.ConnectionClosed:
                print("⚠️ WSS соединение закрыто. Переподключение через 2 секунды...")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"❌ WSS Ошибка: {e}")
                await asyncio.sleep(2)

# === DUMMY HTTP SERVER FOR RENDER ===
from aiohttp import web
import os

async def health_check(request):
    return web.Response(text="Sniper Bot is running securely!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', health_check)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    print(f"🌐 [Render Server] Запуск HTTP сервера на порту {port} (Health-check)")
    await site.start()

async def main():
    # Запускаем dummy-сервер для Render в фоне
    await start_web_server()
    
    # === ГИБРИДНЫЙ РЕЖИМ (СНАЙПЕР + ПАРСЕР СТАРЫХ МОНЕТ) ===
    from main import scanner_loop, position_manager_loop
    from analyzer import Analyzer
    from tracker import PaperTracker
    
    analyzer = Analyzer()
    tracker = self.tracker if self.tracker else PaperTracker()
    
    # Запускаем фоновые задачи для старых монет
    print("🧬 [HYBRID MODE] Запуск сканера DexScreener...")
    asyncio.create_task(scanner_loop(analyzer, tracker))
    asyncio.create_task(position_manager_loop(analyzer, tracker))
    
    # Запускаем основной луп снайпера (новые монеты по WSS)
    sniper = PumpFunSniper()
    await sniper.connect_and_listen()

if __name__ == "__main__":
    asyncio.run(main())
