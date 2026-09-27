import asyncio
import time
import aiohttp
from http_client import get_session
import config
from analyzer import Analyzer


async def fetch_geckoterminal_trending():
    """Получает реальные тренды с GeckoTerminal (как в Photon)"""
    tokens = []
    url = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    
    session = await get_session()
    try:
        async with session.get(url, headers=headers, timeout=15) as response:
            if response.status == 200:
                data = await response.json()
                for pool in data.get("data", []):
                    try:
                        # GeckoTerminal хранит адрес токена в relationships
                        base_token_id = pool["relationships"]["base_token"]["data"]["id"]
                        # Формат: "solana_MintAddress"
                        mint = base_token_id.split("_")[1]
                        if mint and mint not in tokens:
                            tokens.append(mint)
                    except:
                        pass
    except Exception as e:
        print(f"Ошибка получения трендов GeckoTerminal: {type(e).__name__} - {e}")
    return tokens


async def fetch_pumpfun_top():
    """Получает топ монет Pump.fun по капе (близкие к миграции на Raydium)"""
    tokens = []
    url = "https://frontend-api.pump.fun/coins?offset=0&limit=200&sort=market_cap&order=DESC&includeNsfw=false"
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    
    session = await get_session()
    try:
        async with session.get(url, headers=headers, timeout=15) as response:
            if response.status == 200:
                data = await response.json()
                for coin in data:
                    mint = coin.get("mint")
                    if mint and mint not in tokens:
                        tokens.append(mint)
            elif response.status == 403:
                # Cloudflare режет дата-центр IP (и Render, и домашние). Молчим, есть другие источники.
                pass
            else:
                print(f"Pump.fun top: HTTP {response.status}")
    except Exception as e:
        # Тихий fail: источник необязательный (есть DexScreener boosts + GT + WSS-роддом)
        print(f"Pump.fun top недоступен ({type(e).__name__}), пропускаю источник.")
    return tokens

async def fetch_dexscreener_trending():
    """Трендовые токены: сначала GeckoTerminal (работает с Render),
    потом DexScreener endpoints как fallback."""
    tokens = []
    try:
        import market_data
        for t in await market_data.get_trending():
            m = t.get("tokenAddress")
            if m and m not in tokens:
                tokens.append(m)
    except Exception as e:
        print(f"Ошибка GT-трендов: {type(e).__name__} - {e}")
    # Эндпоинты DexScreener для поиска самого горячего (FOMO)
    urls = [
        "https://api.dexscreener.com/token-profiles/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1"
    ]
    
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    
    session = await get_session()
    for url in urls:
        try:
            # DS с Render часто висит до таймаута — короткий таймаут, это лишь fallback
            async with session.get(url, headers=headers, timeout=6) as response:
                if response.status == 200:
                    data = await response.json()
                    for item in data:
                        # Извлекаем адрес токена на Solana
                        if item.get('chainId') == 'solana':
                            mint = item.get('tokenAddress')
                            if mint and mint not in tokens:
                                tokens.append(mint)
        except Exception as e:
            print(f"Ошибка получения FOMO токенов: {type(e).__name__} - {e}")
    return tokens

async def fomo_loop(analyzer: Analyzer, tracker):
    """Цикл, который постоянно сканирует тренды (FOMO) и топ-токены"""
    print("🔥 FOMO Scanner запущен: отслеживаем ракеты и тренды DexScreener!")
    
    # Чтобы не спамить API
    processed_mints = {}
    
    while True:
        try:
            # СРОЧНО: Kill-switch был только в scanner_loop, а FOMO продолжал покупать в минус!
            import time as _t
            day_start = _t.time() - (_t.time() % 86400)
            day_pnl = sum(getattr(p, "pnl_usd", 0) or 0 for p in tracker.positions.values()
                          if getattr(p, "status", "") == "closed" and getattr(p, "exit_time", 0) and p.exit_time >= day_start)
            if getattr(config, "KILL_SWITCH_ENABLED", True) and day_pnl <= -config.MAX_DAILY_LOSS_USD:
                print(f"🛑 FOMO KILL-SWITCH: дневной PnL ${day_pnl:.2f}. Пауза 1ч.")
                await asyncio.sleep(3600)
                continue
            if len(tracker.get_open_positions()) >= config.MAX_CONCURRENT_POSITIONS:
                await asyncio.sleep(10)
                continue
                
            trending_mints = await fetch_dexscreener_trending()
            gecko_mints = await fetch_geckoterminal_trending()
            pump_mints = await fetch_pumpfun_top()
            for m in pump_mints:
                if m not in trending_mints:
                    trending_mints.append(m)
            for m in gecko_mints:
                if m not in trending_mints:
                    trending_mints.append(m)
            
            # Фильтруем уже обработанные и в кулдауне
            new_mints = []
            for mint in trending_mints:
                if time.time() - processed_mints.get(mint, 0.0) < 600:
                    continue
                
                if mint in tracker.positions:
                    pos = tracker.positions[mint]
                    if pos.status == "open" or (time.time() - pos.entry_time) < (4 * 3600):
                        continue
                
                new_mints.append(mint)
            
            # Снижаем нагрузку на сеть (Render NAT rate limits)
            for i in range(0, len(new_mints), 3):
                if len(tracker.get_open_positions()) >= config.MAX_CONCURRENT_POSITIONS:
                    break
                    
                batch = new_mints[i:i+3]
                print(f"🔍 FOMO: анализируем батч из {len(batch)} токенов...")
                
                async def analyze_one(mint):
                    try:
                        return mint, await analyzer.analyze_token(mint)
                    except Exception as e:
                        print(f"⚠️ Ошибка анализа {mint[:8]}...: {e}")
                        return mint, False
                
                # Обрабатываем ПОСЛЕДОВАТЕЛЬНО, чтобы не убивать сеть Render (NAT limits/Timeouts)
                results = []
                for m in batch:
                    res = await analyze_one(m)
                    results.append(res)
                    await asyncio.sleep(1) # Крошечная пауза между монетами
                
                # Увеличенная пауза между батчами
                await asyncio.sleep(3)
                
                for mint, is_buy in results:
                    if is_buy is None:
                        continue
                    processed_mints[mint] = time.time()
                    
                    if is_buy and len(tracker.get_open_positions()) < config.MAX_CONCURRENT_POSITIONS:
                        pair_data = await analyzer.fetch_token_data(mint)
                        if pair_data:
                            actual_symbol = pair_data.get("baseToken", {}).get("symbol", "FOMO")
                            
                            # ИСПРАВЛЕНИЕ: Берем LIVE цену без кэша (GeckoTerminal), а не отстающую цену DexScreener!
                            from sol_price import fetch_bulk_prices_sync
                            # Запускаем синхронную функцию в пуле потоков, чтобы не блокировать весь event loop бота!
                            live_prices = await asyncio.to_thread(fetch_bulk_prices_sync, [mint])
                            actual_price = float(live_prices.get(mint, 0.0))
                            
                            if actual_price <= 0:
                                actual_price = float(pair_data.get("priceUsd", 0)) # Fallback, если GeckoTerminal не знает монету
                                
                            if actual_price > 0:
                                capital = tracker.get_total_capital()
                                if capital <= 0:
                                    break
                                # База = фиксированный ордер как в сканере (цель $10/день), не голые 5%
                                fixed = getattr(config, "TRADE_AMOUNT_USD", 10.0)
                                position_size = max(4.0, min(100.0, fixed if fixed else capital * (config.REINVEST_PERCENT / 100.0)))
                                # Кэп от пула как в сканере: не больше 0.5% ликвидности (INFERENCE -59%)
                                liq_usd = (pair_data.get("liquidity") or {}).get("usd", 0) or 0
                                if liq_usd > 0:
                                    position_size = min(position_size, max(1.0, liq_usd * 0.005))
                                position_size *= analyzer.conviction_size_mult(pair_data)
                                position_size = min(position_size, 100.0)
                                _dep = sum(p.amount_usd for p in tracker.get_open_positions().values())
                                _cap = capital * getattr(config, "MAX_DEPLOYED_PCT", 0.60)
                                if _dep + position_size > _cap:
                                    print(f"🚫 FOMO Exposure: занято ${_dep:.0f}, лимит ${_cap:.0f}. Пропуск.")
                                    continue
                                print(f"🚀 СНАЙП FOMO-РАКЕТЫ {actual_symbol} ({mint})! Входим на {position_size}$ по цене {actual_price}$")
                                tracker.add_position(actual_symbol, mint, actual_price, position_size,
                                                     source=f"FOMO:{analyzer.get_signal(mint)}")
                    
            # Держим память в чистоте
            if len(processed_mints) > 1000:
                processed_mints.clear()
                
        except Exception as e:
            print(f"Ошибка в FOMO Loop: {e}")
            
        await asyncio.sleep(120) # СРОЧНО: было 60 -> 120. Меньше FOMO-сделок = меньше покупок вершин
