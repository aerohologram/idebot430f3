import asyncio
import threading
import time
import json
import streamlit as st
import pandas as pd
import config
from analyzer import Analyzer
from tracker import PaperTracker
from fomo_scanner import fomo_loop

# === 1. ФОНОВЫЙ ТОРГОВЫЙ БОТ ===
async def position_manager_loop(analyzer, tracker):
    from jupiter import JupiterAPI
    print("🛡️ Запуск менеджера позиций (быстрый трекинг Stop-Loss)...")
    while True:
        try:
            open_positions = tracker.get_open_positions()
            
            # ОПТИМИЗАЦИЯ СКОРОСТИ: Запрашиваем цены для ВСЕХ позиций ОДНИМ запросом.
            # Только Solana-позиции этого менеджера (0x и GROWTH ведут свои петли).
            mints_to_fetch = [m for m, p in open_positions.items()
                              if not m.startswith("0x")
                              and getattr(p, "chain", "solana") == "solana"
                              and not str(getattr(p, "source", "")).startswith("GROWTH")]
            bulk_prices = await JupiterAPI.get_prices(mints_to_fetch) if mints_to_fetch else {}
            
            for mint, position in list(open_positions.items()):
                # Robinhood (0x...) ведёт отдельный robinhood_loop со своими ценами DS
                if mint.startswith("0x") or getattr(position, "chain", "solana") != "solana":
                    continue
                # GROWTH-позиции ведёт growth_loop (медленные выходы) - пропускаем
                if str(getattr(position, "source", "")).startswith("GROWTH"):
                    continue
                # 1. Берем цену из Raydium/Gecko (запросили разом для всех)
                api_price = bulk_prices.get(mint, 0.0)
                
                # 2. Берем цену из WebSocket (если она СВЕЖАЯ, а не записанная этим же циклом)
                ws_price = position.current_price_usd if hasattr(position, 'current_price_usd') else 0.0
                ws_ts = getattr(position, "price_updated_at", 0.0)
                ws_fresh = ws_price > 0.0 and (time.time() - ws_ts) < 8

                if ws_fresh:
                    current_price = ws_price
                elif api_price > 0.0:
                    current_price = api_price
                elif ws_price > 0.0:
                    current_price = ws_price
                else:
                    current_price = 0.0

                prev_price = position.current_price_usd
                if current_price <= 0.0:
                    minutes_held = (time.time() - position.entry_time) / 60
                    if minutes_held > 180:
                        tracker.close_position(mint, 0.0, "Rug Pull / No Liquidity")
                    continue
                    
                if current_price > position.max_price_usd:
                    position.max_price_usd = current_price
                    position.peak_time = time.time()
                    
                pnl_pct = (current_price - position.entry_price_usd) / position.entry_price_usd
                max_pnl_pct = (position.max_price_usd - position.entry_price_usd) / position.entry_price_usd
                minutes_held = (time.time() - position.entry_time) / 60
                
                peak_ts = getattr(position, "peak_time", position.entry_time)
                minutes_since_peak = (time.time() - peak_ts) / 60 if peak_ts else 0

                prev_ts = getattr(position, "price_checked_at", 0.0)
                if prev_ts > 0 and (time.time() - prev_ts) < 60 and prev_price > 0 \
                        and current_price <= prev_price * 0.80:
                    tracker.close_position(mint, current_price,
                                           f"Crash Guard (-{(1 - current_price/prev_price)*100:.0f}% за {(time.time()-prev_ts):.0f} сек)")
                    continue
                position.price_checked_at = time.time()
                
                # === ТАЙМАУТ ПОСЛЕ РАКЕТЫ (По просьбе пользователя) ===
                # У мемкоинов есть фаза импульса. Если после взлета прошло 5 минут, а нового перехая нет,
                # и цена ползет вниз (или просто стоит), закрываем в безубыток или мелкий минус.
                if max_pnl_pct >= 0.10 and minutes_since_peak >= 15:
                    tracker.close_position(mint, current_price, f"Post-Rocket Fade Cut ({minutes_since_peak:.0f}m after peak)")
                    continue

                # === STAGNANT EXIT: минус режем на STAGNANT_LOSS_MIN, мелкий плюс держим до HOLD_MIN ===
                _loss_min = getattr(config, "STAGNANT_LOSS_MIN", 7)
                _hold_min = getattr(config, "STAGNANT_HOLD_MIN", 25)
                if minutes_held >= _loss_min and pnl_pct < 0 and max_pnl_pct < 0.05:
                    tracker.close_position(mint, current_price, f"Stagnant Loss Cut ({minutes_held:.0f}m, {pnl_pct*100:.1f}%)")
                    continue
                if minutes_held >= _hold_min and abs(pnl_pct) < 0.05 and max_pnl_pct < 0.05:
                    tracker.close_position(mint, current_price, f"Stagnant Near Zero ({_hold_min} min flat)")
                    continue
                # Profit lock удален, используется трейлинг-стоп из config.py
                
                # === СКАЛЬП-ТРЕЙЛИНГ (Забираем мелкие плюсы) ===
                # Если ракета не долетела до +25%, но дала +15% и начала падать, забираем свое.
                if max_pnl_pct >= 0.15 and max_pnl_pct < getattr(config, "TRAILING_ACTIVATION_PCT", 0.25):
                    drop_from_max = (position.max_price_usd - current_price) / position.max_price_usd
                    if drop_from_max >= 0.05:
                        tracker.close_position(mint, current_price, f"Scalp Profit (peak +{max_pnl_pct*100:.0f}%)")
                        continue
                
                # Обновляем текущие значения для отображения в интерфейсе
                # (сохраняем один раз за цикл ниже, а не на каждой позиции,
                # чтобы не делать N записей в Supabase каждые 3 секунды)
                position.current_price_usd = current_price
                position.current_pnl_usd = position.amount_usd * pnl_pct
                
                # === ИНТЕГРАЦИЯ МАТЕМАТИКИ ДЛЯ ЗРЕЛЫХ МОНЕТ (SWING TRADING) ===
                if getattr(position, "is_mature", False):
                    # MOONBAG для mature: RAFFLE дал +61% пик без частичной фиксации.
                    # На триггере продаём половину сразу - дальше едет бесплатно.
                    _mb = getattr(config, "MOONBAG_TRIGGER_PCT", 0.50)
                    if max_pnl_pct >= _mb and not getattr(position, "is_moonbag", False):
                        tracker.partial_close_position(mint, current_price, 0.50, f"Take Profit +{_mb*100:.0f}% (Risk Free)")
                        continue
                    from exit_managers import MatureExitManager
                    mature_exit_reason = MatureExitManager.evaluate_exit(position, current_price)
                    if mature_exit_reason:
                        tracker.close_position(mint, current_price, mature_exit_reason)
                    continue # Если это mature монета, скальперская логика ниже к ней не применяется!
                
                # === ЖЕСТКИЙ RISK MANAGEMENT (CRO LEVEL) ===
                
                # Умный расчет минимального порога для покрытия комиссий
                priority_fee_usd = 0.075 if position.amount_usd < 10.0 else 0.45
                min_fee_pct = (priority_fee_usd + 0.02 * position.amount_usd) / position.amount_usd
                
                # 1. СКАЛЬП-ТРЕЙЛИНГ (Забираем мелкие плюсы)
                # Если ракета не долетела до +25%, но дала +15% и начала падать, забираем свое.
                if max_pnl_pct >= 0.15 and max_pnl_pct < getattr(config, "TRAILING_ACTIVATION_PCT", 0.25):
                    drop_from_max = (position.max_price_usd - current_price) / position.max_price_usd
                    if drop_from_max >= 0.05:
                        tracker.close_position(mint, current_price, f"Scalp Profit (peak +{max_pnl_pct*100:.0f}%)")
                        continue
                        
                # === MOONBAG: Возврат инвестиций (Жесткий Take Profit) ===
                # На триггере продаем 50% позиции. Забираем свои деньги.
                _mb2 = getattr(config, "MOONBAG_TRIGGER_PCT", 0.50)
                if max_pnl_pct >= _mb2 and not getattr(position, "is_moonbag", False):
                    tracker.partial_close_position(mint, current_price, 0.50, f"Take Profit +{_mb2*100:.0f}% (Risk Free)")
                    continue

                # 2. ОСНОВНОЙ ТРЕЙЛИНГ-СТОП (Динамическая фиксация позиции)
                drop_from_max = (position.max_price_usd - current_price) / position.max_price_usd
                
                # Трейлинг: до Moonbag — 25% (стандарт для мемкоинов).
                # После Moonbag — 35% (деньги уже в кармане, остаток бесплатный — пусть летит!).
                trail_distance = 0.35 if getattr(position, "is_moonbag", False) else 0.25
                
                # Активируем трейлинг из config.py
                if max_pnl_pct >= getattr(config, "TRAILING_ACTIVATION_PCT", 0.30):
                    if drop_from_max >= trail_distance:
                        tracker.close_position(mint, current_price, f"Trailing Stop (peak +{max_pnl_pct*100:.0f}%, drop {drop_from_max*100:.0f}%)")
                        continue
                            
                # 3. АВАРИЙНЫЙ КЭП ПЕРВЫМ: FIBONACCI -68% проскочили стоп в тонком пуле.
                # Больше -30% одну сделку не держим. (Стоял после стопа - был недостижим.)
                if pnl_pct <= -0.30:
                    tracker.close_position(mint, current_price, f"Emergency Cap ({pnl_pct*100:.0f}%)")
                    continue

                # 3.5 ЖЕСТКИЙ Stop Loss из config.py
                if pnl_pct <= config.STOP_LOSS_PCT:
                    tracker.close_position(mint, current_price, f"Hard Stop Loss ({config.STOP_LOSS_PCT*100:.0f}%)")
                    continue
                    
                # 4. УМНЫЙ ВЫХОД ПО ВРЕМЕНИ (Stagnant / Bleeding cut)
                # Вернули по просьбе: режем мертвые через 15 мин
                if minutes_held >= 15 and pnl_pct < 0:
                    tracker.close_position(mint, current_price, f"Dead Coin Cut ({minutes_held:.0f}m, {pnl_pct*100:.1f}%)")
                    continue
                    
                # Если монета болтается около нуля больше 25 минут - выходим, освобождаем капитал
                if minutes_held >= 25 and pnl_pct < 0.10:
                    tracker.close_position(mint, current_price, f"Stagnant Cut ({minutes_held:.0f}m, {pnl_pct*100:.1f}%)")
                    continue
                    
                # Старый Time Exit (резервный)
                if minutes_held >= config.TIME_EXIT_MINUTES and pnl_pct < config.TIME_EXIT_PROFIT_REQ:
                    tracker.close_position(mint, current_price, "Time-based Exit")
                    continue
            # Сохраняем обновлённые цены одним разом за цикл (файл + Supabase)
            tracker.save_portfolio()
        except Exception as e:
            print(f"Ошибка в менеджере позиций: {e}")
        # Балк-цены Jupiter+DS: 1 запрос на все позиции за цикл, квоту не жрёт.
        # 2 сек вместо 3: стоп -20% исполнялся как -59% (INFERENCE) из-за проскока между тиками.
        await asyncio.sleep(2.0)

from birth_tracker import BirthTracker
birth_tracker = BirthTracker()

async def birth_wss_loop(analyzer, tracker):
    import websockets
    import json
    print("👶 Запуск Роддома: сбор базы данных новых токенов для сканера (без авто-покупки на 0-секунде)...")
    uri = config.PUMPPORTAL_WSS
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Origin": "https://pumpportal.fun"
    }
    while True:
        try:
            async with websockets.connect(uri, extra_headers=headers) as ws:
                payload = {"method": "subscribeNewToken"}
                await ws.send(json.dumps(payload))
                async for message in ws:
                    data = json.loads(message)
                    mint = data.get("mint")
                    if mint:
                        # Просто сохраняем монету в базу для scanner_loop (чтобы она "настоялась" 5-20 мин)
                        birth_tracker.add_token(mint)
        except Exception as e:
            print(f"Ошибка WSS Роддома: {e}. Переподключение через 5 секунд...")
            await asyncio.sleep(5)

async def scout_loop(analyzer, tracker):
    """🔭 SCOUT: ранний рукав по возрасту + velocity (SPEC-кейс).
    Ловит токены 1-12 мин с живой тягой WSS ДО вертикали m5, пока DS-индекс пуст.
    Триггер: возраст + buyers/trades/vol по PumpPortal WSS (без DS).
    Подтверждение: DS/Gecko pair_data (liq, m1 не дамп, h24 не перегрев).
    Сайз $3 билет, макс 3 скаута открыто, кулдаун 300с. Выходы — общие mature."""
    print("🔭 Scout Loop запущен: возраст 1-12 мин + velocity (ранние ракеты).")
    if not getattr(config, "SCOUT_ENABLED", True):
        print("🔭 Scout отключён в config (SCOUT_ENABLED=False).")
        return
    processed = {}
    interval = int(getattr(config, "SCOUT_INTERVAL", 20) or 20)
    while True:
        try:
            import time as _t
            # Kill-switch общий
            day_start = _t.time() - (_t.time() % 86400)
            day_pnl = sum(getattr(p, "pnl_usd", 0) or 0 for p in tracker.positions.values()
                          if getattr(p, "status", "") == "closed" and getattr(p, "exit_time", 0) and p.exit_time >= day_start)
            if getattr(config, "KILL_SWITCH_ENABLED", True) and day_pnl <= -config.MAX_DAILY_LOSS_USD:
                await asyncio.sleep(300)
                continue
            scout_open = sum(1 for p in tracker.get_open_positions().values()
                             if str(getattr(p, "source", "")).startswith("SCOUT"))
            if len(tracker.get_open_positions()) >= config.MAX_CONCURRENT_POSITIONS or \
                    scout_open >= int(getattr(config, "SCOUT_MAX_POS", 3) or 3):
                await asyncio.sleep(interval)
                continue
            # Кандидаты: WSS-реестр 1-12 мин с тягой
            cands = []
            _birth_strict = False
            try:
                import fast_stream as _fs
                _min_age = int(getattr(config, "SCOUT_MIN_AGE_SEC", 60) or 60)
                _max_age = int(getattr(config, "SCOUT_MAX_AGE_SEC", 720) or 720)
                _min_buy = int(getattr(config, "SCOUT_MIN_BUYERS", 5) or 5)
                _min_tr = int(getattr(config, "SCOUT_MIN_TRADES", 15) or 15)
                for _m, _r in list(_fs._registry.items()):
                    _age = _t.time() - _r.get("first_seen", 0)
                    if _age < _min_age or _age > _max_age:
                        continue
                    if len(_r.get("buyers", ())) >= _min_buy or _r.get("trades", 0) >= _min_tr:
                        cands.append((_m, _age, len(_r.get("buyers", ())), _r.get("trades", 0)))
            except Exception as _e:
                cands = []
            # Старше WSS-окна — добор из birth (DS уже проиндексировал).
            # НО: без WSS-velocity это дублирует scanner_loop с более слабыми гейтами
            # (так купили AMM v0/0 → -100%). Поэтому birth-кандидатов требуем ЖЁСТЧЕ:
            # velocity по DS как у сканера (tx5≥30, buys≥sells×1.5), иначе скип.
            if not cands:
                try:
                    _bm = birth_tracker.get_mature_tokens(1, 12)
                    cands = [(_m, 0, 0, 0) for _m in _bm[:20]]
                    _birth_strict = True
                except Exception:
                    pass
            for _mint, _age, _buyers, _trades in cands[:20]:
                if not _mint or _mint in tracker.positions:
                    continue
                if _t.time() - processed.get(_mint, 0.0) < int(getattr(config, "SCOUT_RECOOLDOWN", 300) or 300):
                    continue
                # DS ещё пуст на 1-3 мин — не хороним на 600с, повторим пока возраст <12 мин
                try:
                    _td = await analyzer.fetch_token_data(_mint)
                except Exception:
                    _td = {}
                if not _td:
                    continue
                try:
                    _pc = _td.get("priceChange") or {}
                    _m1 = float(_pc.get("m1", 0) or 0)
                    _h1 = float(_pc.get("h1", 0) or 0)
                    _h24 = float(_pc.get("h24", 0) or 0)
                    _tx5 = (_td.get("txns") or {}).get("m5", {}) or {}
                    _b5, _s5 = int(_tx5.get("buys", 0) or 0), int(_tx5.get("sells", 0) or 0)
                    _liq = float((_td.get("liquidity") or {}).get("usd", 0) or 0)
                    _vol = _td.get("volume") or {}
                    _vm5 = float(_vol.get("m5", 0) or 0)
                    _price = float(_td.get("priceUsd", 0) or 0)
                    _sym = ((_td.get("baseToken") or {}).get("symbol", "") or _mint[:4])[:12]
                except Exception:
                    continue
                if _liq < float(getattr(config, "SCOUT_MIN_LIQ", 15000) or 0):
                    processed[_mint] = _t.time()
                    continue
                if _m1 <= float(getattr(config, "SCOUT_MAX_DUMP_M1", -0.15) or -0.15) * 100:
                    processed[_mint] = _t.time()
                    continue
                if _h1 > 300.0 or _h24 > 500.0:
                    processed[_mint] = _t.time()
                    continue
                if (_b5 + _s5) < 10 or (_s5 > 0 and _b5 < _s5):
                    processed[_mint] = _t.time()
                    continue
                if _birth_strict and _trades == 0 and _buyers == 0:
                    # Нет WSS-доказательства тяги — требуем как сканер, иначе это AMM-кейс
                    if (_b5 + _s5) < 30 or (_s5 > 0 and _b5 < _s5 * 1.5):
                        processed[_mint] = _t.time()
                        continue
                _up = str(_sym).upper()
                if any(k in _up for k in ("TEST", "SCAM", "HONEYPOT")):
                    processed[_mint] = _t.time()
                    continue
                if _price <= 0:
                    continue
                # Экспозиция: не больше 60% капитала в рынке
                _size = float(getattr(config, "SCOUT_SIZE_USD", 3.0) or 3.0)
                try:
                    _deployed = sum(p.amount_usd for p in tracker.get_open_positions().values())
                    _cap = tracker.get_total_capital() * getattr(config, "MAX_DEPLOYED_PCT", 0.60)
                    if _deployed + _size > _cap:
                        print(f"🚫 SCOUT отказ (Exposure): занято ${_deployed:.0f}.")
                        break
                except Exception:
                    pass
                _age_m = f"{_age/60:.0f}m" if _age else "birth"
                tracker.add_position(_sym, _mint, _price, _size, is_mature=True,
                                     source=f"SCOUT:{_age_m} v{_trades}/{_buyers}")
                print(f"🔭 SCOUT BUY {_sym} {_age_m} buyers={_buyers} tr={_trades} @ ${_price}")
                processed[_mint] = _t.time()
                break
            if len(processed) > 1000:
                processed.clear()
        except Exception as e:
            print(f"Ошибка в scout_loop: {e}")
        await asyncio.sleep(interval)


async def scanner_loop(analyzer, tracker):
    print("🚀 Запуск PhantBot Scanner (Поиск новых монет)...")
    processed_mints = {}
    while True:
        try:
            open_count = len(tracker.get_open_positions())
            # Дневной kill-switch: -$18 за день -> стоп входов на 24ч
            import time as _t
            day_start = _t.time() - (_t.time() % 86400)
            day_pnl = sum(getattr(p, "pnl_usd", 0) or 0 for p in tracker.positions.values()
                          if getattr(p, "status", "") == "closed" and getattr(p, "exit_time", 0) and p.exit_time >= day_start)
            if getattr(config, "KILL_SWITCH_ENABLED", True) and day_pnl <= -config.MAX_DAILY_LOSS_USD:
                print(f"🛑 KILL-SWITCH: дневной PnL ${day_pnl:.2f} <= -${config.MAX_DAILY_LOSS_USD}. Торги остановлены на 24ч.")
                await asyncio.sleep(3600)
                continue
            if open_count < config.MAX_CONCURRENT_POSITIONS:
                print(f"🔎 Сканируем монеты... (Открыто: {open_count}/{config.MAX_CONCURRENT_POSITIONS})")
                
                # 0. FAST свежие (0-сек discovery) — приоритет, без 600с бана
                mints_to_scan = []
                try:
                    import fast_stream
                    if getattr(config, "FAST_STREAM_ENABLED", True):
                        hot = fast_stream.get_hot_fresh()
                        fresh = fast_stream.get_fresh_mints(180)
                        if hot:
                            print(f"⚡ FAST: {len(hot)} горячих свежих (buyers>= {getattr(config,'FAST_MIN_BUYERS',6)}) — проверяю первыми!")
                            mints_to_scan.extend(hot)
                        if fresh:
                            mints_to_scan.extend(fresh)
                except Exception as _e:
                    print(f"⚡ FAST fallback: {_e}")

                # 1. VIP Токены (DexScreener API)
                tokens = await analyzer.fetch_latest_tokens()
                mints_to_scan.extend([p.get("tokenAddress") for p in tokens if p.get("tokenAddress")])

                # 2. Уличные Токены (Берем молодые ракеты от 5 до 20 минут)
                mature_mints = birth_tracker.get_mature_tokens(3, 25)  # Было 5-20: шире окно молодняка = больше ракет
                if mature_mints:
                    print(f"🎂 Найдено {len(mature_mints)} перспективных монет (возраст 5-20 минут)!")
                    mints_to_scan.extend(mature_mints)
                
                # Удаляем дубликаты
                mints_to_scan = list(set(mints_to_scan))
                
                for mint in mints_to_scan:
                    if not mint or mint in tracker.positions:
                        continue
                    if time.time() - processed_mints.get(mint, 0.0) < 600:
                        continue
                    
                    # Держим память в чистоте
                    if len(processed_mints) > 1000:
                        processed_mints.clear()
                        
                    try:
                        # Используем умный маршрутизатор (сам выберет XGBoost или Raydium модель)
                        is_good = await analyzer.analyze_token(mint)
                        if is_good is None:
                            continue
                        processed_mints[mint] = time.time()
                            
                        if is_good:
                            pair_data = await analyzer.fetch_token_data(mint)
                            entry_price = float(pair_data.get("priceUsd", 0)) if pair_data else 0
                            actual_symbol = pair_data.get("baseToken", {}).get("symbol", "UNKNOWN") if pair_data else "UNKNOWN"
                            if entry_price > 0:
                                # Динамический сайзинг
                                capital = tracker.get_total_capital()
                                base_position = capital * (config.REINVEST_PERCENT / 100.0)
                                
                                liq_usd = pair_data.get("liquidity", {}).get("usd", 0) if pair_data else 0
                                # INFERENCE -59%: сайз был велик для тонкого пула, стоп проскочил.
                                # Не больше 0.5% пула (было 1%) - иначе сами двигаем цену и не выходим.
                                max_allowed_by_pool = liq_usd * 0.005 if liq_usd > 0 else 99999.0  
                                
                                fixed = getattr(config, "TRADE_AMOUNT_USD", 0)
                                position_size = min(fixed, max_allowed_by_pool) if fixed else max(4.0, min(base_position, max_allowed_by_pool, 100.0))
                                
                                _lot_m5 = ((pair_data.get("priceChange") or {}).get("m5", 0) or 0)
                                _is_lot = _lot_m5 >= getattr(config, "LOTTERY_MIN_M5_PCT", 1.0) * 100
                                
                                # Если это настоящая VIP-ракета (огромный объем), входим на полный сайз
                                _txns_m5 = pair_data.get("txns", {}).get("m5", {})
                                _buys = _txns_m5.get("buys", 0)
                                _vol = pair_data.get("volume", {}).get("m5", 0)
                                _is_vip = _buys >= 50 and _vol >= 30000
                                
                                if _is_lot and not _is_vip:
                                    position_size *= getattr(config, "LOTTERY_SIZE_MULT", 0.25)

                                # Conviction-тиринг: подтверждённый импульс (m5 20-60%, buys 2x+, пул $30к+) едет x2.
                                # Тир решает рынок, НЕ скор модели и НЕ голый VIP-объём (катастрофы были VIP-100%).
                                if not _is_lot:
                                    position_size *= analyzer.conviction_size_mult(pair_data)
                                position_size = min(position_size, 100.0)

                                if position_size < (1.0 if _is_lot else 4.0):
                                    print(f"🚫 Отказ (Ликвидность): Недостаточно ликвидности (${liq_usd}) для безопасного входа.")
                                    continue

                                # Защита от переразгона: в рынке не больше 60% капитала
                                _deployed = sum(p.amount_usd for p in tracker.get_open_positions().values())
                                _cap = tracker.get_total_capital() * getattr(config, "MAX_DEPLOYED_PCT", 0.60)
                                if _deployed + position_size > _cap:
                                    print(f"🚫 Отказ (Exposure): занято ${_deployed:.0f} + ${position_size:.0f} > лимит ${_cap:.0f}.")
                                    continue
                                    
                                tracker.add_position(actual_symbol, mint, entry_price, position_size, is_mature=True,
                                                     source=f"SCANNER:{analyzer.get_signal(mint)}")
                                break # Ждем следующего цикла после покупки
                            else:
                                print(f"⚠️ Ошибка: Не удалось получить цену для {mint} (entry_price=0). Возможно, Rate Limit (429).")
                        
                        # Пауза между монетами
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        print(f"⚠️ Ошибка анализа {mint[:8]}: {type(e).__name__}: {e}. Сканирую дальше.")
        except Exception as e:
            print(f"Ошибка в цикле сканера: {e}")
        await asyncio.sleep(15)  # было 30: импульсы m5 живут минуты, опрос чаще = раньше вход

from copytrader import CopyTrader
import os

def parse_signal_line(raw: str):
    """Разбор строки очереди: 'sol:<mint>[@канал]' | 'evm:<chain>:<addr>[@канал]' | '0x...' | '<mint>'.
    Тег @канал ставит tg_preview (видно в дашборде как TG-SIGNAL:канал:...)."""
    raw = (raw or "").strip()
    channel = ""
    if "@" in raw:
        raw, _, channel = raw.rpartition("@")
        raw, channel = raw.strip(), channel.strip()
    if raw.startswith("evm:"):
        rest = raw[4:].strip()
        if ":" in rest:
            _chn, mint = rest.split(":", 1)
            return f"evm:{_chn.strip().lower()}", mint.strip(), channel
        return "evm", rest, channel
    if raw.startswith("sol:"):
        return "sol", raw[4:].strip(), channel
    if raw.startswith("0x"):
        return "evm", raw, channel
    return "sol", raw, channel


async def fomo_signal_loop(analyzer, tracker):
    print("📲 Запуск обработчика сигналов FOMO...")
    while True:
        try:
            if os.path.exists('fomo_signals.txt'):
                with open('fomo_signals.txt', 'r') as f:
                    mints = f.read().splitlines()

                if mints:
                    # Очищаем файл после прочтения
                    with open('fomo_signals.txt', 'w') as f:
                        f.write('')
                    # Бёрст-контроль: очередь могла копиться — старые коллы = чужие вершины.
                    # Берём 25 самых свежих, покупаем максимум 3 за проход.
                    _maxpass = int(getattr(config, "MAX_FOMO_BUYS_PER_PASS", 3) or 3)
                    mints = [m for m in mints if (m or "").strip()][-25:]
                    _buys = 0
                    for raw in mints:
                        kind, mint, _src_ch = parse_signal_line(raw)
                        if not mint or mint in tracker.positions:
                            continue
                        _tag = f"TG:{_src_ch} " if _src_ch else ""
                        print(f"🚨 ПРИНЯТ ВНЕШНИЙ СИГНАЛ ({_tag}FOMO): {mint}")
                        if kind == "evm" or kind.startswith("evm:"):
                            import evm_data
                            found = None
                            if ":" in kind:
                                _want = kind.split(":", 1)[1]
                                try:
                                    _td = await evm_data.get_token_data(mint, _want)
                                except Exception:
                                    _td = {}
                                if _td and float(_td.get("priceUsd", 0) or 0) > 0:
                                    found = (_want, _td)
                            if not found:
                                for _ch in ("base", "bsc", "robinhood"):
                                    try:
                                        _td = await evm_data.get_token_data(mint, _ch)
                                    except Exception:
                                        _td = {}
                                    if _td and float(_td.get("priceUsd", 0) or 0) > 0:
                                        found = (_ch, _td)
                                        break
                            if not found:
                                print(f"📲 TG EVM {mint[:10]}: ни в одной сети не найден.")
                                continue
                            _ch, _td = found
                            if _ch == "ethereum":
                                # L1-входы выкл: газ $2-6 съедает скальп. Только ведём старые.
                                print(f"🚫 TG EVM {mint[:10]}: сеть ethereum, входы выкл — скип.")
                                continue
                            ok = await analyzer.analyze_robinhood_token(mint, _ch)
                            if ok is True and mint not in tracker.positions:
                                _esig = analyzer.get_signal(mint)
                                price = float(_td.get("priceUsd", 0))
                                sym = ((_td.get("baseToken") or {}).get("symbol", "TG") or "TG")
                                if price > 0:
                                    cap = tracker.get_total_capital()
                                    size = max(4.0, min(100.0, cap * (config.REINVEST_PERCENT / 100.0))) if cap > 0 else 4.0
                                    size = min(size, float(getattr(config, "TG_MAX_SIZE_USD", 4.0) or 4.0))
                                    if "LOTTERY" in str(_esig):
                                        size = min(size, 3.0)
                                    if _ch == "robinhood":
                                        size = min(size, float(getattr(config, "ROB_MAX_SIZE_USD", 4.0) or 4.0))
                                    _tg_src = f"TG-SIGNAL:{_src_ch}:{_esig}" if _src_ch else f"TG-SIGNAL:{_esig}"
                                    tracker.add_position(sym, mint, price, size, is_mature=True,
                                                         source=_tg_src, chain=_ch)
                                    print(f"📲 TG-SIGNAL BUY {sym} [{_ch}] @ ${price}")
                                    _buys += 1
                                    if _buys >= _maxpass:
                                        break
                            continue
                        # Solana-ветка: те же гейты, что у сканера
                        is_good = await analyzer.analyze_token(mint)

                        if is_good:
                            pair_data = await analyzer.fetch_token_data(mint)
                            entry_price = float(pair_data.get("priceUsd", 0)) if pair_data else 0
                            actual_symbol = pair_data.get("baseToken", {}).get("symbol", "FOMO") if pair_data else "FOMO"

                            if entry_price > 0:
                                capital = tracker.get_total_capital()
                                position_size = max(4.0, min(100.0, capital * (config.REINVEST_PERCENT / 100.0)))
                                position_size = min(position_size, float(getattr(config, "TG_MAX_SIZE_USD", 4.0) or 4.0))
                                _ssig = analyzer.get_signal(mint)
                                _tg_src = f"TG-SIGNAL:{_src_ch}:{_ssig}" if _src_ch else f"TG-SIGNAL:{_ssig}"
                                tracker.add_position(actual_symbol, mint, entry_price, position_size,
                                                     source=_tg_src)
                                _buys += 1
                                if _buys >= _maxpass:
                                    break
        except Exception as e:
            print(f"Ошибка в fomo_signal_loop: {e}")
        await asyncio.sleep(1) # Проверяем файл каждую секунду для мгновенной реакции

async def growth_loop(analyzer, tracker):
    """🌱 GROWTH MODE: тренд зрелых капов, пока нет ракет.
    Вход в откат H1-тренда, выходы медленные: стоп -10%, трейлинг +8%/12%, стагнант 2ч."""
    print("🌱 Growth Loop запущен: тренды капов (медленные деньги).")
    if not getattr(config, "GROWTH_ENABLED", True):
        return
    interval = getattr(config, "GROWTH_INTERVAL", 300)
    while True:
        try:
            import time as _t
            day_start = _t.time() - (_t.time() % 86400)
            day_pnl = sum(getattr(p, "pnl_usd", 0) or 0 for p in tracker.positions.values()
                          if getattr(p, "status", "") == "closed" and getattr(p, "exit_time", 0) and p.exit_time >= day_start)
            if getattr(config, "KILL_SWITCH_ENABLED", True) and day_pnl <= -config.MAX_DAILY_LOSS_USD:
                await asyncio.sleep(3600)
                continue
            mine = {m: p for m, p in tracker.get_open_positions().items()
                    if str(getattr(p, "source", "")).startswith("GROWTH")}
            # --- трекинг ---
            if mine:
                from jupiter import JupiterAPI
                px = await JupiterAPI.get_prices(list(mine.keys()))
                for mint, pos in list(mine.items()):
                    cur = px.get(mint, 0) or pos.current_price_usd or pos.entry_price_usd
                    if cur > pos.max_price_usd:
                        pos.max_price_usd = cur
                    prev = pos.current_price_usd
                    pos.current_price_usd = cur
                    if not pos.entry_price_usd:
                        continue
                    pnl = (cur - pos.entry_price_usd) / pos.entry_price_usd
                    maxp = (pos.max_price_usd - pos.entry_price_usd) / pos.entry_price_usd
                    pos.current_pnl_usd = pos.amount_usd * pnl
                    held = (_t.time() - pos.entry_time) / 60
                    reason = None
                    prev_ts = getattr(pos, "price_checked_at", 0.0)
                    if prev_ts and (_t.time() - prev_ts) < 300 and prev > 0 and cur <= prev * 0.80:
                        reason = f"GROWTH Crash ({(1 - cur / prev) * 100:.0f}%)"
                    elif maxp >= getattr(config, "GROWTH_TRAIL_ACT", 0.08) and \
                            (pos.max_price_usd - cur) / pos.max_price_usd >= getattr(config, "GROWTH_TRAIL_DIST", 0.12):
                        reason = f"GROWTH Trailing (peak +{maxp * 100:.0f}%)"
                    elif pnl <= getattr(config, "GROWTH_STOP_PCT", -0.10):
                        reason = f"GROWTH Stop ({pnl * 100:.1f}%)"
                    elif held >= getattr(config, "GROWTH_STAGNANT_MIN", 120) and pnl < 0.05:
                        reason = f"GROWTH Stagnant ({held:.0f}m)"
                    pos.price_checked_at = _t.time()
                    if reason:
                        tracker.close_position(mint, cur, reason)
                tracker.save_portfolio()
            # --- скан вселенной: ядро вотчлиста + динамический топ Raydium по ликве ---
            if len(mine) < getattr(config, "GROWTH_MAX_POS", 3) and \
                    len(tracker.get_open_positions()) < config.MAX_CONCURRENT_POSITIONS:
                import market_data as _md
                universe = await _md.get_growth_universe()
                try:
                    import coingecko as _cg
                    _cg_mints = await _cg.trending_solana_mints() or []
                except Exception:
                    _cg_mints = []
                watch = list(dict.fromkeys(list(getattr(config, "GROWTH_WATCHLIST", [])) + universe + _cg_mints))
                for mint in watch:
                    if mint in tracker.positions or len(tracker.get_open_positions()) >= config.MAX_CONCURRENT_POSITIONS:
                        continue
                    try:
                        ok = await analyzer.analyze_growth_token(mint)
                    except Exception as e:
                        print(f"🌱 GROWTH analyze err: {type(e).__name__} {e}")
                        continue
                    if ok is True:
                        from jupiter import JupiterAPI
                        price = await JupiterAPI.get_price(mint)
                        if price > 0:
                            td = await analyzer.fetch_token_data(mint)
                            sym = ((td.get("baseToken") or {}).get("symbol", mint[:4]) if td else mint[:4]) or mint[:4]
                            tracker.add_position(sym, mint, price, float(getattr(config, "GROWTH_SIZE_USD", 6.0)),
                                                 is_mature=True, source=f"GROWTH:{analyzer.get_signal(mint)}")
                            print(f"🌱 GROWTH BUY {sym} @ ${price}")
                    await asyncio.sleep(2)
        except Exception as e:
            print(f"Ошибка в growth_loop: {e}")
        await asyncio.sleep(interval)


async def rugpull_feeder_loop():
    print("🧹 Запуск автоматического сборщика скам-рагпулов (раз в 6 часов)...")
    # Ждем 10 секунд перед первым запуском, чтобы не грузить систему на старте
    await asyncio.sleep(10)
    while True:
        try:
            import rugpull_feeder
            rugpull_feeder.feed_rugs_and_retrain()
        except Exception as e:
            print(f"Ошибка в rugpull_feeder: {e}")
        await asyncio.sleep(6 * 60 * 60)  # Спим 6 часов

async def _evm_chain_loop(analyzer, tracker, chain: str):
    """Обобщённая EVM-петля (Robinhood 4663, Base): boosts+profiles DexScreener.
    Цены и выходы ведёт сам через evm_data (DS), Solana-менеджер 0x-позиции пропускает."""
    import evm_data
    tag = (evm_data.CHAINS.get(chain) or {}).get("tag", chain.upper())
    emoji = {"robinhood": "🟣", "base": "🟦", "bsc": "🟨"}.get(chain, "🔵")
    print(f"{emoji} {tag} Loop запущен: мемы сети {chain}!")
    processed = {}
    _iv_key = {"base": "BASE_SCAN_INTERVAL", "bsc": "BSC_SCAN_INTERVAL"}.get(chain, "ROBINHOOD_SCAN_INTERVAL")
    interval = getattr(config, _iv_key, 25)
    _recooldown = getattr(config, "EVM_RESCAN_COOLDOWN", 300)
    while True:
        try:
            # Kill-switch общий
            import time as _t
            day_start = _t.time() - (_t.time() % 86400)
            day_pnl = sum(getattr(p, "pnl_usd", 0) or 0 for p in tracker.positions.values()
                          if getattr(p, "status", "") == "closed" and getattr(p, "exit_time", 0) and p.exit_time >= day_start)
            if getattr(config, "KILL_SWITCH_ENABLED", True) and day_pnl <= -config.MAX_DAILY_LOSS_USD:
                print(f"🛑 {tag} KILL-SWITCH: PnL ${day_pnl:.2f}. Пауза 1ч.")
                await asyncio.sleep(3600)
                continue
            # --- 1. Трекинг открытых позиций ЭТОЙ сети (строго по chain, не по префиксу!) ---
            mine = {m: p for m, p in tracker.get_open_positions().items()
                    if getattr(p, "chain", "") == chain}
            if mine:
                try:
                    px = await evm_data.get_bulk_prices(list(mine.keys()), chain)
                except Exception as e:
                    print(f"{emoji} {tag} bulk price err: {e}")
                    px = {}
                for mint, pos in list(mine.items()):
                    cur = px.get(mint, 0) or pos.current_price_usd or pos.entry_price_usd
                    if cur > pos.max_price_usd:
                        pos.max_price_usd = cur
                        pos.peak_time = _t.time()
                    prev = pos.current_price_usd
                    pos.current_price_usd = cur
                    if not pos.entry_price_usd:
                        continue
                    pnl = (cur - pos.entry_price_usd) / pos.entry_price_usd
                    maxp = (pos.max_price_usd - pos.entry_price_usd) / pos.entry_price_usd
                    pos.current_pnl_usd = pos.amount_usd * pnl
                    held = (_t.time() - pos.entry_time) / 60
                    reason = None
                    prev_ts = getattr(pos, "price_checked_at", 0.0)
                    _mbr = getattr(config, "MOONBAG_TRIGGER_PCT", 0.50)
                    if maxp >= _mbr and not getattr(pos, "is_moonbag", False):
                        tracker.partial_close_position(mint, cur, 0.50, f"{tag} Take Profit +{_mbr*100:.0f}% (Risk Free)")
                    elif prev_ts and (_t.time() - prev_ts) < 60 and prev > 0 and cur <= prev * 0.80:
                        reason = f"{tag} Crash Guard ({(1 - cur / prev) * 100:.0f}% за {_t.time() - prev_ts:.0f}с)"
                    elif maxp >= 0.15 and (pos.max_price_usd - cur) / pos.max_price_usd >= 0.10:
                        reason = f"{tag} Trailing (peak +{maxp * 100:.0f}%)"
                    elif pnl <= -0.30:
                        reason = f"{tag} Emergency Cap ({pnl * 100:.1f}%)"
                    elif pnl <= config.STOP_LOSS_PCT:
                        reason = f"{tag} Stop ({pnl * 100:.1f}%)"
                    elif held >= 15 and pnl < 0:
                        reason = f"{tag} Dead ({held:.0f}m)"
                    elif held >= 7 and abs(pnl) < 0.05 and maxp < 0.05:
                        reason = f"{tag} Stagnant ({held:.0f}m)"
                    pos.price_checked_at = _t.time()
                    if reason:
                        tracker.close_position(mint, cur, reason)
                tracker.save_portfolio()
            # --- 2. Скан новых: WSS-свежие (секунды) + бусты + профили + тренды GT + СВЕЖИЕ пулы GT ---
            if len(tracker.get_open_positions()) < config.MAX_CONCURRENT_POSITIONS:
                try:
                    import evm_wss as _evm_wss
                    _wss_list = _evm_wss.drain_fresh(chain) or []
                except Exception:
                    _wss_list = []
                boosted = await evm_data.fetch_boosted_tokens(chain)
                profiles = await evm_data.fetch_profile_tokens(chain)
                trending = await evm_data.get_trending_pools_gt(chain)
                fresh = await evm_data.get_new_pools_gt(chain)
                mints = list(dict.fromkeys(_wss_list + boosted + profiles + trending + fresh))[:35]
                for addr in mints:
                    if not addr or addr in tracker.positions:
                        continue
                    if _t.time() - processed.get(addr, 0.0) < _recooldown:
                        continue
                    processed[addr] = _t.time()
                    try:
                        ok = await analyzer.analyze_robinhood_token(addr, chain)
                    except Exception as e:
                        print(f"{emoji} {tag} analyze err {addr[:10]}: {type(e).__name__} {e}")
                        continue
                    if ok is True and len(tracker.get_open_positions()) < config.MAX_CONCURRENT_POSITIONS:
                        td = await evm_data.get_token_data(addr, chain)
                        price = float(td.get("priceUsd", 0) or 0) if td else 0
                        sym = ((td.get("baseToken") or {}).get("symbol", tag) if td else tag) or tag
                        if price > 0:
                            cap = tracker.get_total_capital()
                            size = max(4.0, min(100.0, cap * (config.REINVEST_PERCENT / 100.0))) if cap > 0 else 4.0
                            _sig = analyzer.get_signal(addr)
                            if "LOTTERY" in _sig:
                                size = min(size, 3.0)  # лотерейный билет $3, не позиция
                            tracker.add_position(sym, addr, price, size, is_mature=True,
                                                 source=f"{tag}:{_sig}",
                                                 chain=chain)
                            print(f"{emoji} {tag} BUY {sym} {addr[:10]} @ ${price}")
                    await asyncio.sleep(1.0)
                if len(processed) > 1000:
                    processed.clear()
        except Exception as e:
            print(f"Ошибка в {tag}_loop: {e}")
        await asyncio.sleep(interval)


async def robinhood_loop(analyzer, tracker):
    if not getattr(config, "ROBINHOOD_ENABLED", True):
        print("🟣 Robinhood отключён в config (ROBINHOOD_ENABLED=False).")
        return
    await _evm_chain_loop(analyzer, tracker, "robinhood")


async def base_loop(analyzer, tracker):
    if not getattr(config, "BASE_ENABLED", True):
        print("🟦 Base отключён в config (BASE_ENABLED=False).")
        return
    await _evm_chain_loop(analyzer, tracker, "base")


async def bsc_loop(analyzer, tracker):
    if not getattr(config, "BSC_ENABLED", True):
        print("🟨 BSC отключён в config (BSC_ENABLED=False).")
        return
    await _evm_chain_loop(analyzer, tracker, "bsc")


async def ethereum_loop(analyzer, tracker):
    """⬜ Ethereum: только ведём уже открытые (STOCKER/VERIDIA), новые входы выкл.
    Газ L1 $2-6 съедает скальп — ловим только дешёвые L2. Без этой петли ETH-позиции
    висят с вмёрзшей ценой (вход=сейчас=пик, $0.00)."""
    import evm_data
    print("⬜ Ethereum Loop запущен: только трекинг открытых (входы выкл).")
    while True:
        try:
            mine = {}
            for m, p in tracker.get_open_positions().items():
                _ch = getattr(p, "chain", "")
                _src = str(getattr(p, "source", ""))
                if _ch == "ethereum":
                    mine[m] = p
                elif str(m).startswith("0x") and _ch == "solana" and "ETHEREUM" in _src:
                    # Легаси-баг: старый fomo_loop открывал ETH без chain -> chain=solana.
                    # Чиним на месте, иначе позиция-сирота (вход=сейчас=пик, $0.00).
                    try:
                        p.chain = "ethereum"
                    except Exception:
                        pass
                    mine[m] = p
                    print(f"⬜ ETH heal {m[:10]}: chain solana->ethereum ({_src[:40]})")
            if mine:
                try:
                    px = await evm_data.get_bulk_prices(list(mine.keys()), "ethereum")
                except Exception as e:
                    print(f"⬜ ETH bulk price err: {e}")
                    px = {}
                import time as _t
                for mint, pos in list(mine.items()):
                    cur = px.get(mint, 0) or pos.current_price_usd or pos.entry_price_usd
                    if cur > pos.max_price_usd:
                        pos.max_price_usd = cur
                        pos.peak_time = _t.time()
                    prev = pos.current_price_usd
                    pos.current_price_usd = cur
                    if not pos.entry_price_usd:
                        continue
                    pnl = (cur - pos.entry_price_usd) / pos.entry_price_usd
                    maxp = (pos.max_price_usd - pos.entry_price_usd) / pos.entry_price_usd
                    pos.current_pnl_usd = pos.amount_usd * pnl
                    held = (_t.time() - pos.entry_time) / 60
                    reason = None
                    prev_ts = getattr(pos, "price_checked_at", 0.0)
                    if prev_ts and (_t.time() - prev_ts) < 60 and prev > 0 and cur <= prev * 0.80:
                        reason = f"ETH Crash Guard ({(1 - cur / prev) * 100:.0f}%)"
                    elif maxp >= 0.15 and (pos.max_price_usd - cur) / pos.max_price_usd >= 0.10:
                        reason = f"ETH Trailing (peak +{maxp * 100:.0f}%)"
                    elif pnl <= -0.30:
                        reason = f"ETH Emergency Cap ({pnl * 100:.1f}%)"
                    elif pnl <= config.STOP_LOSS_PCT:
                        reason = f"ETH Stop ({pnl * 100:.1f}%)"
                    elif held >= 15 and pnl < 0:
                        reason = f"ETH Dead ({held:.0f}m)"
                    elif held >= 7 and abs(pnl) < 0.05 and maxp < 0.05:
                        reason = f"ETH Stagnant ({held:.0f}m)"
                    pos.price_checked_at = _t.time()
                    if reason:
                        tracker.close_position(mint, cur, reason)
                tracker.save_portfolio()
        except Exception as e:
            print(f"Ошибка в ethereum_loop: {e}")
        await asyncio.sleep(25)

async def async_main():
    from pump_fun_sniper import PumpFunSniper
    
    # Keep-Alive задача, чтобы Render не засыпал (работает в фоне)
    async def keep_alive():
        import aiohttp, os
        port = int(os.environ.get("PORT", 10000))
        url = os.environ.get("RENDER_EXTERNAL_URL", f"http://127.0.0.1:{port}")
        print(f"🔄 Keep-Alive URL: {url}")
        async with aiohttp.ClientSession() as session:
            while True:
                await asyncio.sleep(600)  # Каждые 10 минут
                try:
                    async with session.get(url) as resp:
                        print(f"💓 Keep-Alive Ping: {resp.status}")
                except Exception as e:
                    pass
    asyncio.create_task(keep_alive())

    from trade_logger import trade_logger
    from birdeye_scanner import birdeye_loop
    from sol_price import get_sol_price

    try:
        from radar_coverage import print_coverage
        print_coverage()
    except Exception as e:
        print(f"⚠️ radar_coverage недоступен: {e}")
    
    # Получаем актуальную цену SOL при старте
    await get_sol_price()
    
    analyzer = Analyzer()
    tracker = PaperTracker()
    copy_trader = CopyTrader(tracker, analyzer)
    sniper = PumpFunSniper(tracker)
    
    async def sol_price_updater():
        """Обновляет цену SOL каждые 5 минут"""
        while True:
            await get_sol_price()
            await asyncio.sleep(300)
    
    from fast_stream import fast_stream_loop, fast_candidate_loop
    try:
        from jup_discovery import jup_loop
    except Exception as e:
        print(f"⚠️ jup_discovery недоступен ({e}) - пропускаю")
        async def jup_loop(*_a, **_k):
            return
    try:
        from evm_wss import evm_wss_loop
    except Exception as e:
        print(f"⚠️ evm_wss недоступен ({e}) - пропускаю")
        async def evm_wss_loop(*_a, **_k):
            return
    try:
        from fomo_api import fomo_api_loop
    except Exception as e:
        print(f"⚠️ fomo_api недоступен ({e}) - пропускаю")
        async def fomo_api_loop(*_a, **_k):
            return
    try:
        from tg_preview import tg_preview_loop
    except Exception as e:
        print(f"⚠️ tg_preview недоступен ({e}) - пропускаю")
        async def tg_preview_loop(*_a, **_k):
            return
    await asyncio.gather(
        position_manager_loop(analyzer, tracker),
        birth_wss_loop(analyzer, tracker),  # был определён но НЕ запущен — роддом стоял пустой!
        birdeye_loop(analyzer, tracker),  # был импортирован но не запущен
        fast_stream_loop(),  # ⚡ 0-сек discovery NewToken + trades
        fast_candidate_loop(analyzer, tracker),  # ⚡ горячие свежие мимо 600с бана
        scanner_loop(analyzer, tracker),
        scout_loop(analyzer, tracker),  # 🔭 ранние 1-12 мин по velocity (SPEC-кейс)
        copy_trader.listen(),
        fomo_signal_loop(analyzer, tracker),
        fomo_loop(analyzer, tracker),
        robinhood_loop(analyzer, tracker),  # 🟣 EVM-мемы Robinhood Chain 4663
        base_loop(analyzer, tracker),  # 🟦 EVM-мемы Base (fomo.family)
        bsc_loop(analyzer, tracker),  # 🟨 BSC-мемы (GSTOCK и co)
        ethereum_loop(analyzer, tracker),  # ⬜ Ethereum: только трекинг (входы выкл, газ)
        jup_loop(analyzer, tracker),  # 🪐 Jupiter recent + toptrending/5m (Solana)
        evm_wss_loop(analyzer, tracker),  # ⚡ WSS фабрик Base+BSC: новые пулы за секунды
        fomo_api_loop(analyzer, tracker),  # 📡 fomoapi.io: покупки топов -> очередь сигналов
        tg_preview_loop(analyzer, tracker),  # 📡 TG-коллы без ключей (t.me/s превью)
        growth_loop(analyzer, tracker),  # 🌱 тренды капов, пока нет ракет
        sniper.connect_and_listen(),  # ENABLED — с AI фильтром — sniper entry kills capital (-85.8%), mature +162.5%
        trade_logger.post_trade_watcher_loop(),
        rugpull_feeder_loop(),
        sol_price_updater()
    )

def run_background_bot():
    """Запускает асинхронный цикл в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_main())

import os
# === 2. ВЕБ-ИНТЕРФЕЙС STREAMLIT ===


st.set_page_config(page_title="PhantBot Dashboard", layout="wide")

# Запускаем торгового бота ровно один раз при старте сервера
@st.cache_resource
def start_bot():
    thread = threading.Thread(target=run_background_bot, daemon=True)
    thread.start()
    return thread

bot_thread = start_bot()

def chain_badge(chain: str, long: bool = False) -> str:
    """Бейдж сети для дашборда: 🟢 SOL / 🟣 ROB / 🟦 BASE."""
    c = str(chain or "solana")
    if c == "robinhood":
        return "🟣 ROB" if long else "🟣"
    if c == "base":
        return "🟦 BASE" if long else "🟦"
    if c == "bsc":
        return "🟨 BSC" if long else "🟨"
    if c == "ethereum":
        return "⬜ ETH" if long else "⬜"
    return "🟢 SOL" if long else "🟢"


def load_dashboard_portfolio():
    """Портфель для дашборда: сначала Supabase (переживает рестарты Render),
    потом локальный файл. Возвращает dict (возможно пустой)."""
    # 1. Supabase — главный источник правды
    try:
        url = getattr(config, 'SUPABASE_URL', None)
        key = getattr(config, 'SUPABASE_KEY', None)
        if url and key:
            from supabase import create_client
            sb = create_client(url, key)
            res = sb.table("trades_pump").select("features").eq("mint", "PORTFOLIO_STATE_V3").execute()
            if res.data and res.data[0].get("features"):
                data = json.loads(res.data[0]["features"])
                if data:
                    return data
            # Слепок пуст, но это тоже ответ — запоминаем для диагностики
            st.session_state["_pf_src"] = "supabase-empty"
        else:
            st.session_state["_pf_src"] = "no-supabase-env"
    except Exception as e:
        st.session_state["_pf_src"] = f"supabase-err: {e}"[:300]
        print(f"⚠️ Дашборд: не удалось прочитать портфель из Supabase: {e}")
    # 2. Fallback: локальный файл
    try:
        with open(config.PAPER_PORTFOLIO_FILE, 'r') as f:
            local = json.load(f)
            if local and "_pf_src" not in st.session_state:
                st.session_state["_pf_src"] = "local-file"
            return local
    except (FileNotFoundError, json.JSONDecodeError):
        if "_pf_src" not in st.session_state:
            st.session_state["_pf_src"] = "no-local-file"
        return {}
    except Exception as e:
        print(f"⚠️ Дашборд: не удалось прочитать локальный портфель: {e}")
        return {}

# Отрисовка интерфейса
st.title("🚀 PhantBot - Alpha Agent Dashboard")
st.markdown("Панель управления алгоритмическим ботом.")

tab1, tab2 = st.tabs(["📊 Портфель и История", "📡 Радар Рынка (Alpha Scanner)"])

with tab1:
    # Кнопка для ручного обновления страницы
    if st.button("🔄 Обновить портфель"):
        pass
        
    try:
        data = load_dashboard_portfolio()
            
        if data:
            df = pd.DataFrame.from_dict(data, orient='index')
            open_df = df[df['status'] == 'open'].copy()
            closed_df = df[df['status'] == 'closed'].copy()
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("🟢 Открытые позиции")
                if not open_df.empty:
                    open_df['pnl_%'] = (open_df['current_pnl_usd'] / open_df['amount_usd']) * 100
                    for index, row in open_df.iterrows():
                        pnl_usd = row['current_pnl_usd']
                        pnl_pct = row['pnl_%']
                        color = "#00C851" if pnl_usd >= 0 else "#FF4444"
                        sign = "+" if pnl_usd > 0 else ""
                        
                        st.markdown(f"""
                        <div style='background-color: #1E1E1E; padding: 15px; border-radius: 8px; border-left: 5px solid {color}; margin-bottom: 10px; font-family: sans-serif;'>
                            <div style='display: flex; justify-content: space-between; align-items: center;'>
                                <h3 style='margin:0; color: #FFF;'>{row['symbol'] if str(row['symbol']).strip() else row['mint'][:6] + '...'}</h3>
                                <h3 style='margin:0; color: {color};'>{sign}${pnl_usd:.2f} ({sign}{pnl_pct:.2f}%)</h3>
                            </div>
                            <div style='margin-top: 6px; font-size: 0.75em; color: #888;'>🔖 {row.get('source', '') if str(row.get('source', '')).strip() else '—'} {chain_badge(row.get('chain', 'solana'), True)}</div>
                            <div style='display: flex; justify-content: space-between; margin-top: 10px; font-size: 0.85em; color: #BBB;'>
                                <div><span style='color:#888;'>Вход:</span><br>${row['entry_price_usd']:.8f}</div>
                                <div><span style='color:#888;'>Сейчас:</span><br>${row['current_price_usd']:.8f}</div>
                                <div><span style='color:#888;'>Пик:</span><br>${row['max_price_usd']:.8f}</div>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    st.info("Нет активных сделок.")
                    
            with col2:
                st.subheader("📓 История сделок")
                if not closed_df.empty:
                    total_pnl = closed_df['pnl_usd'].sum()
                    st.markdown(f"""
                    <div style='background-color: #2D2D2D; padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 15px;'>
                        <div style='color: #888; font-size: 1.1em; text-transform: uppercase;'>Общий PnL</div>
                        <h1 style='margin: 0; color: {"#00C851" if total_pnl >= 0 else "#FF4444"};'>
                            {"+" if total_pnl >= 0 else ""}${total_pnl:.2f}
                        </h1>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    closed_df = closed_df.tail(15).iloc[::-1] # Показываем 15 последних в обратном порядке
                    closed_df['pnl_%'] = (closed_df['pnl_usd'] / closed_df['amount_usd']) * 100
                    
                    for index, row in closed_df.iterrows():
                        p_usd = row['pnl_usd']
                        p_pct = row['pnl_%']
                        c_color = "#00C851" if p_usd >= 0 else "#FF4444"
                        c_sign = "+" if p_usd > 0 else ""
                        
                        st.markdown(f"""
                        <div style='background-color: #1A1A1A; padding: 10px 15px; border-radius: 6px; border-right: 4px solid {c_color}; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center;'>
                            <div>
                                <div style='color: #FFF; font-weight: bold;'>{row['symbol'] if str(row['symbol']).strip() else row.name[:6] + '...'} <span style='font-size: 0.7em; color: #888;'>{chain_badge(row.get('chain', 'solana'))}</span></div>
                                <div style='color: #666; font-size: 0.75em;'>{row.get('exit_reason', 'Closed')}</div>
                                <div style='color: #555; font-size: 0.7em;'>🔖 {row.get('source', '') if str(row.get('source', '')).strip() else '—'}</div>
                            </div>
                            <div style='text-align: right; color: {c_color}; font-weight: bold;'>
                                {c_sign}${p_usd:.2f} <br> <span style='font-size: 0.8em;'>({c_sign}{p_pct:.2f}%)</span>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    st.info("История пуста.")
        else:
            _src = st.session_state.get("_pf_src", "?")
            _has_env = bool(getattr(config, 'SUPABASE_URL', None) and getattr(config, 'SUPABASE_KEY', None))
            st.warning(f"Файл портфеля пуст. Источник: {_src} | Supabase env: {'OK' if _has_env else 'НЕТ (данные живут только в RAM и умрут при рестарте Render!)'}")
            st.caption("Проверь: 1) Render → Environment → SUPABASE_URL / SUPABASE_KEY заданы и сделан Redeploy. 2) В Supabase таблица trades_pump содержит строку mint=PORTFOLIO_STATE_V3. 3) Логи Render: ищи 'Портфель успешно загружен' или 'Supabase недоступен (PGRST125)'. Закрытые сделки при этом живы в журнале trades_pump/trades_raydium (status=CLOSED) — слепок можно восстановить.")
    except FileNotFoundError:
        st.info("Бот еще не совершил первую сделку.")

with tab2:
    st.subheader("🔥 Последние проанализированные токены (Alpha Scanner)")
    st.markdown("Здесь отображаются монеты, которые бот сканирует прямо сейчас, с расчетом рейтинга в стиле **MemeSniper / GMGNAI**.")

    # Метки покупок: какие монеты уже в портфеле (открыты) или были в сделках (закрыты)
    try:
        _pf = data
    except NameError:
        _pf = load_dashboard_portfolio()
    _pf = _pf or {}
    bought_open, bought_closed = set(), set()
    for _k, _v in _pf.items():
        if not isinstance(_v, dict):
            continue
        _m = _v.get("mint", _k)
        if _v.get("status") == "open":
            bought_open.add(_k)
            bought_open.add(_m)
        elif _v.get("status") == "closed":
            bought_closed.add(_k)
            bought_closed.add(_m)
    # Архивные ключи вида mint_old_ts тоже считаем
    def _is_bought(mint: str, pool: set) -> bool:
        if mint in pool:
            return True
        return any(k.startswith(mint + "_old_") or k == mint for k in pool)
    
    try:
        with open("scanned_tokens.json", 'r') as f:
            scanned_data = json.load(f)
            
        if scanned_data:
            for t in scanned_data:
                score = t.get('score', 0)
                color = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"
                _mint = t.get('mint', '')
                if _is_bought(_mint, bought_open):
                    _badge = "<div style='margin-top:8px; font-weight:bold; color:#00C851;'>✅ КУПЛЕН — позиция открыта</div>"
                elif _is_bought(_mint, bought_closed):
                    _badge = "<div style='margin-top:8px; font-weight:bold; color:#888;'>⚪ БЫЛА СДЕЛКА — уже закрыта</div>"
                else:
                    _badge = ""
                
                with st.container():
                    st.markdown(f"""
                    <div style='background-color: #1E1E1E; padding: 15px; border-radius: 10px; border-left: 5px solid {"#00C851" if score >= 70 else "#FF8800"}; margin-bottom: 10px;'>
                        <div style='display: flex; justify-content: space-between;'>
                            <h3 style='margin:0; color: #FFF;'>{t['symbol']} <span style='font-size: 0.6em; color: #888;'>{t['mint'][:8]}...pump {t.get('age_mins', 'New')}</span></h3>
                            <h3 style='margin:0; color: {"#00C851" if t.get("m5_change",0) > 0 else "#FF4444"};'>
                                {'+' if t.get("m5_change",0) > 0 else ''}{t.get('m5_change', 0):.1f}%
                            </h3>
                        </div>
                        <div style='display: flex; justify-content: space-between; margin-top: 10px; font-size: 0.9em;'>
                            <div style='color: #888;'>
                                <div style='font-size: 0.7em; text-transform: uppercase;'>Liquidity</div>
                                <div style='color: #DDD;'>${t.get('liquidity', 0):,.0f}</div>
                            </div>
                            <div style='color: #888;'>
                                <div style='font-size: 0.7em; text-transform: uppercase;'>24H Vol</div>
                                <div style='color: #DDD;'>${t.get('vol_24h', 0):,.0f}</div>
                            </div>
                            <div style='color: #888;'>
                                <div style='font-size: 0.7em; text-transform: uppercase;'>Buys/Sells (5m)</div>
                                <div><span style='color:#00C851;'>{t.get('buys', 0)}</span> / <span style='color:#FF4444;'>{t.get('sells', 0)}</span></div>
                            </div>
                        </div>
                        <div style='margin-top: 15px; border-top: 1px solid #333; padding-top: 10px; display: flex; gap: 15px; font-weight: bold; font-size: 0.9em;'>
                            <div style='color: {"#00C851" if score >= 70 else "#FF8800"};'>🧠 {score}</div>
                            <div style='color: {"#00C851" if t.get("safety",0) >= 70 else "#FF8800"};'>🛡️ {t.get('safety', 0)}</div>
                            <div style='color: {"#00C851" if t.get("momentum",0) >= 70 else "#FF8800"};'>⚡ {t.get('momentum', 0)}</div>
                            <div style='color: {"#00C851" if t.get("social",0) >= 70 else "#FF8800"};'>📣 {t.get('social', 0)}</div>
                        </div>
                        {_badge}
                    </div>
                    """, unsafe_allow_html=True)
        else:
            st.info("Бот пока не проанализировал ни одной монеты.")
    except Exception as e:
        st.info("Ожидание данных от сканера...")

# Автообновление (если включено)
if st.checkbox("Включить автообновление (каждые 5 сек)", value=False):
    time.sleep(5)
    st.rerun()

