import aiohttp
from datetime import datetime, timezone
import time
import config
from sentiment import analyze_sentiment
from ta_tools import TATools
import math

try:
    import numpy as np
    import xgboost as xgb
    class ScamFilter:
        def __init__(self, path='scam_filter_model.json'):
            try:
                self.model = xgb.XGBClassifier()
                self.model.load_model(path)
                self.enabled = True
                print('AI Scam Filter: загружен (XGBoost native, без sklearn-pickle)')
            except Exception as e:
                self.enabled = False
                print(f'AI Scam Filter: пропущен ({e})')
        def is_scam(self, data) -> tuple:
            if not self.enabled:
                return False, 0.0
            try:
                feat = np.array([[data.get('dev_holding_pct', 0), data.get('tx_velocity_1m', 0),
                                  data.get('volume_to_liq_ratio', 0), data.get('funded_from_cex', 0)]])
                return bool(self.model.predict(feat)[0]), float(self.model.predict_proba(feat)[0][1])
            except Exception:
                return False, 0.0
    SCAM_FILTER = ScamFilter()
except Exception as e:
    SCAM_FILTER = None
    print(f'AI Filter ошибка: {e}')


class Analyzer:
    def __init__(self):
        self.session = None
        self.pump_model = None
        self.raydium_model = None
        self.last_signal = ""  # Метка последнего решения: "VIP RAY-XGB 98%", "PULLBACK", "ROBINHOOD rule 72%"...
        self.signals = {}  # Метки по mint: НЕ делит состояние между параллельными петлями (иначе FOMO:? в дашборде)

    def _set_sig(self, key: str, text: str):
        """Метка решения для монеты. Per-mint словарь вместо общего поля:
        scanner/fomo/robinhood гоняют analyze параллельно на одном Analyzer."""
        self.signals[key] = text
        if len(self.signals) > 2000:
            self.signals.clear()
            self.signals[key] = text
        self.last_signal = text

    def get_signal(self, key: str) -> str:
        # Только своя метка монеты. Чужую (last_signal) не подставляем - давала FOMO:? и чужие метки
        return self.signals.get(key) or "?"
        
        # Предзагрузка моделей в память один раз при старте
        try:
            import xgboost as xgb
            self.pump_model = xgb.XGBClassifier()
            self.pump_model.load_model("pump_model.json")
        except: pass
        
        try:
            import xgboost as xgb
            self.raydium_model = xgb.XGBClassifier()
            self.raydium_model.load_model("raydium_model_dex.json")
        except: pass
        
    async def get_session(self):
        import aiohttp
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=300, use_dns_cache=True)
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session

    async def fetch_latest_tokens(self) -> list:
        tokens = []
        session = await self.get_session()
        if True:
            # 1. Сканируем топовые (Boosted) монеты
            try:
                async with session.get(config.DEXSCREENER_LATEST, timeout=5) as response:
                    if response.status == 200:
                        tokens.extend(await response.json())
            except Exception as e:
                print(f"Dexscreener boosts fetch error: {e}")
                
            # 2. Сканируем новые профили, чтобы не пропускать свежие ракеты
            try:
                async with session.get(config.DEXSCREENER_PROFILES, timeout=5) as response:
                    if response.status == 200:
                        tokens.extend(await response.json())
            except Exception as e:
                print(f"Dexscreener profiles fetch error: {e}")
                
        # Возвращаем уникальные токены (по tokenAddress)
        seen = set()
        unique_tokens = []
        for t in tokens:
            addr = t.get("tokenAddress")
            chain = t.get("chainId")
            if addr and addr not in seen and chain == "solana":
                seen.add(addr)
                unique_tokens.append(t)
        return unique_tokens
                
    async def fetch_token_data(self, mint: str) -> dict:
        url = f"{config.DEXSCREENER_SEARCH}{mint}"
        session = await self.get_session()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        if True:
            try:
                async with session.get(url, headers=headers, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        pairs = data.get("pairs", [])
                        if pairs:
                            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                            if sol_pairs:
                                return sorted(sol_pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0), reverse=True)[0]
                    return await self.fetch_token_data_gecko(mint)
            except Exception as e:
                return await self.fetch_token_data_gecko(mint)

    async def fetch_token_data_gecko(self, mint: str) -> dict:
        import asyncio
        await asyncio.sleep(2)  # Жесткий лимит: не спамить GeckoTerminal (макс 30/мин)
        session = await self.get_session()
        try:
            url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools?page=1"
            async with session.get(url, timeout=8, headers={"Accept": "application/json"}) as response:
                if response.status != 200:
                    return {}
                data = await response.json()
                pools = data.get("data", [])
                if not pools:
                    return {}
                best = max(pools, key=lambda p: float((p.get("attributes") or {}).get("reserve_in_usd", 0) or 0))
                a = best.get("attributes", {})
                pc = a.get("price_change_percentage") or {}
                tx = a.get("transactions") or {}
                vu = a.get("volume_usd") or {}
                created = a.get("pool_created_at")
                created_ms = 0
                if created:
                    from datetime import datetime
                    created_ms = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000)
                symbol = (a.get("name") or "UNKNOWN").split("/")[0].strip()

                def _tx(key):
                    t = tx.get(key) or {}
                    return {"buys": int(t.get("buys", 0) or 0), "sells": int(t.get("sells", 0) or 0)}

                print(f"🦎 GeckoTerminal fallback для {mint[:8]}: пул найден.")
                return {
                    "baseToken": {"symbol": symbol, "name": symbol},
                    "priceUsd": str(a.get("base_token_price_usd") or 0),
                    "priceChange": {"m5": float(pc.get("m5") or 0), "m1": 0.0,
                                    "h1": float(pc.get("h1") or 0), "h24": float(pc.get("h24") or 0)},
                    "txns": {"m5": _tx("m5"), "h1": _tx("h1"), "h24": _tx("h24")},
                    "volume": {"m5": float(vu.get("m5") or 0), "h1": float(vu.get("h1") or 0),
                               "h24": float(vu.get("h24") or 0)},
                    "liquidity": {"usd": float(a.get("reserve_in_usd") or 0)},
                    "fdv": float(a.get("fdv_usd") or a.get("market_cap_usd") or 0),
                    "pairCreatedAt": created_ms,
                    "dexId": "pump" if mint.endswith("pump") else "raydium",
                    "info": {"socials": [], "websites": []},
                }
        except Exception as e:
            print(f"GeckoTerminal token data error: {type(e).__name__} {e}")
            return {}

    async def is_clone(self, symbol: str, current_mint: str, current_created_at: int, current_fdv: float) -> bool:
        """Проверяет, является ли этот токен дешевой копией (клоном) более старого или крупного оригинала."""
        if not symbol or len(symbol) <= 2:
            return False 
            
        url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
        session = await self.get_session()
        if True:
            try:
                async with session.get(url, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        pairs = data.get("pairs", [])
                        
                        for p in pairs:
                            if p.get("chainId") == "solana":
                                p_symbol = p.get("baseToken", {}).get("symbol", "").upper()
                                p_mint = p.get("baseToken", {}).get("address", "")
                                
                                if p_symbol == symbol.upper() and p_mint != current_mint:
                                    p_created_at = p.get("pairCreatedAt", float('inf'))
                                    p_fdv = p.get("fdv", 0)
                                    
                                    # Если мы нашли другой токен с таким же именем, который был создан РАНЬШЕ нас
                                    # и имеет какую-то ЗНАЧИТЕЛЬНУЮ капитализацию (а не просто мертвый токен)
                                    if p_created_at < current_created_at and p_fdv > 250000:
                                        return True
                                        
                                    # Либо если другой токен имеет огромную капу (в 10 раз больше нашей),
                                    # значит он - оригинал, а мы клон.
                                    if p_fdv > (current_fdv * 10) and p_fdv > 500000:
                                        return True
            except Exception as e:
                pass
        return False

    async def check_rugcheck(self, mint: str) -> bool:
        url = config.RUGCHECK_API.format(mint=mint)
        session = await self.get_session()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        if True:
            try:
                async with session.get(url, headers=headers, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        score = data.get("score", 1000)
                        # Защита от моментальных дампов (-37%). Строгий фильтр скама.
                        if score >= 400: # было 150 (слишком строго, блокировало почти всё). 400 - оптимально.
                            return False
                            
                        token_info = data.get("token", {})
                        if token_info.get("mintAuthority") is not None:
                            return False
                        if token_info.get("freezeAuthority") is not None:
                            return False
                        
                        # Фильтр по названию токена
                        name = token_info.get("name", "").lower()
                        symbol = token_info.get("symbol", "").lower()
                        bad_words = ["test", "scam", "fuck", "nigger", "pump and dump", "rug"]
                        if any(w in name for w in bad_words) or any(w in symbol for w in bad_words):
                            return False
                            
                        # Индекс Херфиндаля-Хиршмана (HHI) для выявления скрытых монополий (как Bubble Map)
                        top_holders = data.get("topHolders", [])
                        hhi_index = sum([(h.get("pct", 0) * 100) ** 2 for h in top_holders[:15] if not h.get("isContract", False)])
                        
                        top_10_pct = sum([h.get("pct", 0) for h in top_holders[:10] if not h.get("isContract", False)])
                        
                        # По статистике: скамы имеют 6% удержания топ-10, ракеты - 34.5%.
                        # Блокируем только очевидный снайперский скам (>75% у одного кабала)
                        if top_10_pct >= 75 or hhi_index > 4000:
                            return False
                            
                        return True
                    print(f"⚠️ RugCheck HTTP {response.status} для {mint}. Fail-Closed: пропускаем подозрительный токен.")
                    return False
            except Exception as e:
                print(f"⚠️ RugCheck fetch error ({type(e).__name__}): {e}. Fail-Closed: пропускаем подозрительный токен.")
                return False

    async def get_helius_transaction_metrics(self, mint: str) -> tuple:
        """ Возвращает (unique_buyers_m5, smart_money_inflow) """
        unique_buyers = 0
        smart_money_inflow = 0
        
        # Читаем smart wallets
        smart_wallets = set()
        import os
        if os.path.exists("smart_wallets.txt"):
            with open("smart_wallets.txt", "r") as f:
                smart_wallets = {line.strip() for line in f if line.strip()}
                
        payload_sigs = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [mint, {"limit": 30}]
        }
        
        import aiohttp
        session = await self.get_session()
        if True:
            try:
                # 1. Получаем сигнатуры
                async with session.post(config.HELIUS_RPC_URL, json=payload_sigs, timeout=3) as resp:
                    data = await resp.json()
                    signatures = [item["signature"] for item in data.get("result", [])]
                
                if not signatures:
                    return 0, 0
                    
                # 2. Получаем детали транзакций
                payload_txs = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransactions",
                    "params": [signatures, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                }
                async with session.post(config.HELIUS_RPC_URL, json=payload_txs, timeout=5) as resp:
                    tx_data = await resp.json()
                    transactions = tx_data.get("result", [])
                    
                    buyers = set()
                    for tx in transactions:
                        if not tx or not tx.get("transaction"):
                            continue
                            
                        account_keys = tx["transaction"]["message"]["accountKeys"]
                        for acc in account_keys:
                            if acc.get("signer"):
                                pubkey = acc.get("pubkey")
                                buyers.add(pubkey)
                                if pubkey in smart_wallets:
                                    smart_money_inflow += 1
                                break 
                                
                    unique_buyers = len(buyers)
                    
            except Exception as e:
                print(f"⚠️ Ошибка Helius RPC при парсинге транзакций: {e}")
                
        return unique_buyers, smart_money_inflow

    def check_hyper_rocket_momentum(self, pair_data: dict) -> bool:
        """
        VIP-полоса для Гипер-Ракет:
        Ищет аномальные всплески покупок (>50 покупок) и объема (>$30,000) в первые 5 минут.
        Позволяет пропустить стандартные жесткие фильтры.
        """
        txns_m5 = pair_data.get("txns", {}).get("m5", {})
        buys_m5 = txns_m5.get("buys", 0)
        volume_m5 = pair_data.get("volume", {}).get("m5", 0)
        
        # > 50 покупок И > $30k объема в 5-минутном окне (Реальное FOMO)
        if buys_m5 >= 50 and volume_m5 >= 30000:
            return True
        return False
        
    async def analyze_token(self, mint: str) -> bool:
        # Smart Router
        self.last_signal = ""  # сброс метки решения
        self.signals.pop(mint, None)
        
        # Мы больше не используем глючный RugCheck API.
        # Вместо этого проверка на снайперов/бандлы идет напрямую через блокчейн (Helius RPC)
        # в функции extract_features_for_moonshot.
        # Игнорируем базовые монеты и стейблкоины
        if mint in ["So11111111111111111111111111111111111111112", 
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"]:
            return False

        pair_data = await self.fetch_token_data(mint)
        if not pair_data:
            # Токен слишком новый (API еще не проиндексировал его)
            return None
            
        # Блэклист тикеров и названий (Защита от фейковых токенов)
        base_token = pair_data.get("baseToken", {})
        name = base_token.get("name", "").upper()
        symbol = base_token.get("symbol", "").upper()
        
        scam_keywords = ["AAPL", "S&P", "SP500", "MSFT", "TSLA", "NVDA", "GOOG", "AMZN", "META", "NFLX", 
                         "PEPE", "SHIB", "DOGE", "FLOKI", "BONK", "WIF", "BOME", "POPCAT", "TRUMP", "BIDEN"]
                         
        if any(keyword in symbol for keyword in scam_keywords) or any(keyword in name for keyword in scam_keywords):
            print(f"🚫 Мусор: Токен {symbol} мимикрирует под известный бренд/мем. Это 100% scam.")
            return False
            
        is_vip = self.check_hyper_rocket_momentum(pair_data)

        # === VIP OVERHEAT GUARD: не покупаем вершину вертикали ===
        if is_vip and pair_data:
            _pc = pair_data.get("priceChange") or {}
            _m5 = _pc.get("m5", 0) or 0
            _m1 = _pc.get("m1", 0) or 0
            if _m5 > getattr(config, "VIP_MAX_M5_PCT", 0.40) * 100:
                print(f"🚫 [VIP OVERHEAT] {mint}: m5 {_m5:+.0f}% — вертикаль уже прошла, вход = вершина.")
                return False
            if _m1 < 0:
                print(f"🚫 [VIP REVERSAL] {mint}: m1 {_m1:+.1f}% — всплеск откатывает, ждём pullback.")
                return False
            # VIP БЕЗ ИМПУЛЬСА = объём без направления: 20-мин прогон показал,
            # такие входы (score 97-99%, m5 ~0%) стоят флетом 7 мин и сливают ~3% на комиссиях
            if _m5 < 10.0:
                print(f"🚫 [VIP FLAT] {mint}: объём есть, а импульса нет (m5 {_m5:+.1f}% < +10%) — флет съест комиссиями.")
                return False

        # ══════════════════════════════════════════════════════
        # ══════════════════════════════════════════════════════
        
        # === PULLBACK ENTRY (не-VIP): входим в ОТКАТ после импульса, не в вершину ===
        _lottery = False
        if not is_vip and pair_data:
            _pc = pair_data.get("priceChange") or {}
            _m5 = _pc.get("m5", 0) or 0
            _m1 = _pc.get("m1", 0) or 0
            _h1 = _pc.get("h1", 0) or 0
            _tx = (pair_data.get("txns") or {}).get("h1", {}) or {}
            _b, _s = _tx.get("buys", 0) or 0, _tx.get("sells", 0) or 0
            _v24 = (pair_data.get("volume") or {}).get("h24", 0) or 0
            _txm5_pre = (pair_data.get("txns") or {}).get("m5", {}) or {}
            _vm5_pre = (pair_data.get("volume") or {}).get("m5", 0) or 0
            _created_pre = pair_data.get("pairCreatedAt") or 0
            _age_min_pre = (time.time() * 1000 - _created_pre) / 60000.0 if _created_pre else 999
            if _age_min_pre < 10 and (_txm5_pre.get("buys", 0) + _txm5_pre.get("sells", 0)) == 0 and _vm5_pre == 0:
                print(f"⏳ [ENTRY] {mint}: возраст {_age_min_pre:.1f} мин, m5-окно API ещё пустое — повторю позже, снайпер ведёт его по WSS.")
                return None
            if _m5 >= getattr(config, "LOTTERY_MIN_M5_PCT", 1.0) * 100:
                if _m1 < 0:
                    print(f"🚫 [LOTTERY] {mint}: m5 {_m5:+.0f}%, но m1 {_m1:+.1f}% — вертикаль откатывает, это вершина.")
                    return False
                print(f"🎰 [LOTTERY] {mint}: вертикаль m5 {_m5:+.0f}%, m1 {_m1:+.1f}% — кандидат на вход уменьшенным сайзом.")
                _lottery = True
            else:
                if _m5 < getattr(config, "PULLBACK_MIN_M5_PCT", 0.03) * 100:
                    print(f"🚫 [ENTRY] {mint}: m5 {_m5:+.1f}% < импульса не было, пропуск.")
                    return False
                if _m1 > getattr(config, "PULLBACK_M1_MAX_PCT", 0.10) * 100:
                    print(f"🚫 [ENTRY] {mint}: m1 {_m1:+.1f}% — вертикаль в процессе, купим вершину. Ждём откат.")
                    return False
                if _m1 < getattr(config, "PULLBACK_M1_MIN_PCT", -0.15) * 100:
                    print(f"🚫 [ENTRY] {mint}: m1 {_m1:+.1f}% — импульс схлопнулся, это дамп, не откат.")
                    return False
                if _h1 > getattr(config, "PULLBACK_MAX_H1_PCT", 1.5) * 100:
                    print(f"🚫 [ENTRY] {mint}: h1 {_h1:+.0f}% — уже улетел, поздно.")
                    return False
            # АНТИ-ВЕРШИНА h24: токены типа Drip +3152%, SI +33009% уже отстреляли. Вход = вершина
            _h24 = (_pc.get("h24", 0) or 0)
            if _h24 > 500:
                print(f"🚫 [OVERHEAT-H24] {mint}: h24 {_h24:+.0f}% > +500% — ракета уже улетела, поздно.")
                return False
            if _s > 0 and _b < _s * 1.1:
                print(f"🚫 [ENTRY] {mint}: buys {_b} / sells {_s} — нет давления покупателей.")
                return False
            if _v24 < 10000:
                print(f"🚫 [ENTRY] {mint}: vol24h ${_v24:,.0f} < $10k — совсем нет объёма.")
                return False
            _txm5 = (pair_data.get("txns") or {}).get("m5", {}) or {}
            _b5, _s5 = _txm5.get("buys", 0) or 0, _txm5.get("sells", 0) or 0
            
            if (_b5 + _s5) < 30:
                print(f"🚫 [VELOCITY] {mint}: txns m5 {_b5 + _s5} < 30 — слишком медленно, нет органического FOMO.")
                return False
            if _s5 > 0:
                mult = 1.0
                if _b5 < _s5 * mult:
                    print(f"🚫 [VELOCITY] {mint}: buy/sell m5 {_b5}/{_s5} < {mult}x — {'(лотерея, ослаблено)' if _lottery else 'нет буфера покупателей'}")
                    return False
            _socials = (pair_data.get("info") or {}).get("socials") or []
            _created = pair_data.get("pairCreatedAt") or 0
            if _created:
                _age_h = (time.time() * 1000 - _created) / 3.6e6
                if _age_h < 6 and isinstance(_socials, list) and len(_socials) == 0 and not _lottery:
                    print(f"🚫 [SOCIAL] {mint}: нет соцсетей при возрасте {_age_h:.1f}ч — высокий скам-риск.")
                    return False
            print(f"✅ [ENTRY-CANDIDATE] {mint}: PULLBACK — импульс m5 {_m5:+.1f}%, откат m1 {_m1:+.1f}%, h1 {_h1:+.0f}%, b/s {_b}/{_s}. Кандидат на вход.")
        
        # Защита от микро-пулов (Scam сетки типа Fly)
        liquidity = pair_data.get("liquidity", {}).get("usd", 0)
        min_liq = getattr(config, "MIN_LIQUIDITY", 15000)
        if liquidity < min_liq and not is_vip and pair_data.get("dexId") != "pump":
            print(f"📉 Изоляция: {symbol} имеет микро-пул (${liquidity:.0f} < ${min_liq//1000}k). Риск 100% проскальзывания.")
            return False
            
        if is_vip:
            print(f"🚀 [VIP] {symbol}: Пропуск проверок клонов и соцсетей из-за гипер-моментума!")
            
        # 1.5 Защита от вторичных клонов (Copycat Filter)
        current_created_at = pair_data.get("pairCreatedAt", 0)
        current_fdv = pair_data.get("fdv", 0)
        if not is_vip and await self.is_clone(symbol, mint, current_created_at, current_fdv):
            print(f"🚫 Мусор: Токен {symbol} является клоном! На DexScreener найден более старый/крупный оригинал.")
            return False
            
        # 2. Обязательное наличие соцсетей (Proof of Effort: Twitter + Website/TG)
        info = pair_data.get("info", {})
        socials = info.get("socials", [])
        websites = info.get("websites", [])
        
        has_twitter = any("twitter" in s.get("type", "").lower() or "x.com" in s.get("url", "").lower() for s in socials)
        has_tg = any("telegram" in s.get("type", "").lower() or "t.me" in s.get("url", "").lower() for s in socials)
        has_website = len(websites) > 0
        
        # Соцсети обязательны для всех включая VIP: объём накрутить можно, сайт - нет
        if not (has_twitter or has_tg or has_website):
            print(f"🚫 Мусор: У {mint} вообще нет ни одной соцсети или сайта.")
            return False
            
        # 🔴 ГЛОБАЛЬНЫЙ АНТИСКАМ БЛОК: MINT + FREEZE AUTHORITY
        # Работает для ЛЮБЫХ токенов (и Pump, и Raydium)
        # ══════════════════════════════════════════════════════
        rpc_url = getattr(config, "HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=9efda6f4-fddb-42d3-a2b1-098bbbecd299")
        
        # Проверенные бесплатные RPC (2026): Helius (ключ), Solana Foundation, PublicNode, Alchemy (ключ юзера).
        # Мёртвые удалены: projectserum, rpcpool, solscan, api.mainnet.solana.com-дубль, Ankr без ключа 403.
        fallback_rpcs = [
            "https://api.mainnet-beta.solana.com",
            "https://solana-rpc.publicnode.com",
        ]
        
        mint_info_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}]
        }
        
        fake_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://explorer.solana.com",
            "Referer": "https://explorer.solana.com/"
        }
        
        mint_data = None
        try:
            import aiohttp
            session = await self.get_session()
            
            # Пробуем основной Helius
            try:
                async with session.post(rpc_url, json=mint_info_payload, timeout=5) as resp:
                    if resp.status == 200:
                        mint_data = await resp.json(content_type=None)
                    else:
                        raise Exception(f"HTTP {resp.status} - {await resp.text()}")
            except Exception as e:
                # Если Helius упал, перебираем резервные узлы
                for fallback_url in fallback_rpcs:
                    try:
                        async with session.post(fallback_url, json=mint_info_payload, headers=fake_headers, timeout=10) as resp:
                            if resp.status == 200:
                                mint_data = await resp.json(content_type=None)
                                break
                            else:
                                err_txt = await resp.text()
                                print(f"⚠️ Резервный {fallback_url} выдал {resp.status}: {err_txt[:100]}")
                    except Exception as ex:
                        print(f"⚠️ Ошибка резервного {fallback_url}: {ex}")
                        continue
                        
            if mint_data:
                parsed = mint_data.get("result", {}).get("value", {}).get("data", {}).get("parsed", {})
                mint_info = parsed.get("info", {})
                
                mint_authority = mint_info.get("mintAuthority")
                freeze_authority = mint_info.get("freezeAuthority")
                
                # Официальная программа Pump.fun — её authority разрешена
                PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
                SYSTEM_PROGRAM = "11111111111111111111111111111111"
                SAFE_AUTHORITIES = {PUMPFUN_PROGRAM, SYSTEM_PROGRAM, None, ""}
                
                if mint_authority and mint_authority not in SAFE_AUTHORITIES:
                    print(f"🚫 [АНТИСКАМ] Mint Authority у ДЕВ-кошелька {mint_authority[:8]} у {mint[:8]} → СКАМ")
                    return False
                
                if freeze_authority and freeze_authority not in SAFE_AUTHORITIES:
                    print(f"🚫 [АНТИСКАМ] Freeze Authority у ДЕВ-кошелька {freeze_authority[:8]} у {mint[:8]} → СКАМ")
                    return False
            else:
                # Если после перебора ВСЕХ узлов мы так и не получили данные
                print(f"⚠️ Не удалось проверить Mint Authority (все RPC недоступны). Блокируем вход от греха подальше.")
                return False
        except Exception as e:
            print(f"⚠️ Критическая ошибка при проверке Mint Authority: {str(e)[:50]}. Блокируем вход.")
            return False

        # === ГЛОБАЛЬНЫЙ JITO BUNDLE (SYBIL) CHECK ===
        top10_payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]}
        try:
            bundle_data = None
            
            # 1. Helius (ключ) 2. Alchemy (пользовательский) 3. Foundation 4. PublicNode
            heavy_rpcs = [
                rpc_url,
                "https://solana-mainnet.g.alchemy.com/v2/alch_wwSmrv5RZmrq66-lSNekM",
                "https://api.mainnet-beta.solana.com",
                "https://solana-rpc.publicnode.com",
            ]
            
            for heavy_url in heavy_rpcs:
                try:
                    async with session.post(heavy_url, json=top10_payload, headers=fake_headers, timeout=8) as resp:
                        if resp.status == 200:
                            bundle_data = await resp.json(content_type=None)
                            if bundle_data and "result" in bundle_data:
                                break
                except Exception as e:
                    print(f"⚠️ Ошибка Jito RPC {heavy_url}: {type(e).__name__} {e}")
                    continue
            
            # Если платные/выделенные ключи отвалились, пробуем публичные (но они часто банят)
            if not bundle_data or "result" not in bundle_data:
                for fallback_url in fallback_rpcs:
                    try:
                        async with session.post(fallback_url, json=top10_payload, headers=fake_headers, timeout=5) as resp:
                            if resp.status == 200:
                                res = await resp.json(content_type=None)
                                if res and "result" in res:
                                    bundle_data = res
                                    break
                    except Exception: continue

            if bundle_data:
                accounts = bundle_data.get("result", {}).get("value", [])
                if accounts:
                    non_curve_accounts = [float(acc["uiAmount"]) for acc in accounts if float(acc["uiAmount"]) < 800_000_000]
                    top_10_amounts = non_curve_accounts[:10]
                    if len(top_10_amounts) >= 3:
                        rounded_amounts = [round(amt, -6) for amt in top_10_amounts if amt > 1000000]
                        if rounded_amounts:
                            from collections import Counter
                            counts = Counter(rounded_amounts)
                            if counts.most_common(1)[0][1] >= 3:
                                print(f"🚫 [АНТИСКАМ] Обнаружен Jito-бандл (Сивил атака) у {mint}. Блокируем.")
                                return False
                    
                    top_10_sum_pct = (sum(top_10_amounts) / 1_000_000_000.0) * 100
                    dev_holding_pct = (top_10_amounts[0] / 1_000_000_000.0) * 100 if top_10_amounts else 0.0
                    self._last_top10 = top_10_sum_pct
                    self._last_dev = dev_holding_pct
                    
                    is_pump = pair_data and pair_data.get("dexId") == "pump"
                    max_allowed_pct = 20.0 if is_pump else 45.0
                    if top_10_sum_pct > 100:
                        # Баг данных: сапплай не 1B (meteora/raydium), проценты >100% невозможны - пропускаем проверку
                        print(f"⚠️ [HOLDERS] {mint[:8]}: топ-10 {top_10_sum_pct:.1f}% > 100% — битые данные сапплая, пропускаю проверку.")
                    elif top_10_sum_pct > max_allowed_pct:
                        print(f"🚫 [АНТИСКАМ] Топ-10 держат {top_10_sum_pct:.1f}% (Лимит {max_allowed_pct}%). Блокируем.")
                        return False
            else:
                print(f"⚠️ Не удалось проверить Jito-бандлы (RPC недоступны). Блокируем вход.")
                return False
        except Exception as e:
            print(f"⚠️ Ошибка Jito-bundle: {e}")
            return False

        if is_vip or _lottery:
            print(f"🚀 [FAST TRACK] {symbol}: гейты пройдены, передаем на проверку холдеров (Снайперы/Бандлы).")
            self._set_sig(mint, "VIP" if is_vip else "LOTTERY")

        dex_id = pair_data.get("dexId")
        created_at = pair_data.get("pairCreatedAt", 0)
        age_minutes = (time.time() * 1000 - created_at) / (1000 * 60) if created_at else 999
        
        if dex_id == "pump" and age_minutes <= 15:
            return await self.analyze_token_xgboost(mint, pair_data)
        else:
            return await self.analyze_token_raydium(mint, pair_data)

    async def analyze_robinhood_token(self, address: str, chain: str = "robinhood") -> bool:
        """Вход по EVM-мемам (Robinhood Chain 4663, Base).
        Solana-проверки неприменимы — rule-based скоринг на тех же воротах импульса.
        Возвращает True/False, метка решения в signals[address]."""
        import evm_data
        self.last_signal = ""
        self.signals.pop(address, None)
        tag = (evm_data.CHAINS.get(chain) or {}).get("tag", chain.upper())
        min_liq = (evm_data.CHAINS.get(chain) or {}).get("min_liq",
                  getattr(config, "ROBINHOOD_MIN_LIQUIDITY", 8000))
        pair_data = await evm_data.get_token_data(address, chain)
        if not pair_data:
            return None
        base = pair_data.get("baseToken", {}) or {}
        symbol = base.get("symbol", address[:6])
        pc = pair_data.get("priceChange") or {}
        m5 = pc.get("m5", 0) or 0
        m1 = pc.get("m1", 0) or 0
        h1 = pc.get("h1", 0) or 0
        h24 = pc.get("h24", 0) or 0
        txh1 = (pair_data.get("txns") or {}).get("h1", {}) or {}
        b, s = txh1.get("buys", 0) or 0, txh1.get("sells", 0) or 0
        txm5 = (pair_data.get("txns") or {}).get("m5", {}) or {}
        b5, s5 = txm5.get("buys", 0) or 0, txm5.get("sells", 0) or 0
        vol24 = (pair_data.get("volume") or {}).get("h24", 0) or 0
        liq = (pair_data.get("liquidity") or {}).get("usd", 0) or 0
        info = pair_data.get("info") or {}
        links = (info.get("socials") or []) + (info.get("websites") or [])

        if liq < min_liq:
            print(f"🚫 [{tag}] {symbol}: ликва ${liq:,.0f} < ${min_liq:,.0f} — микро-пул.")
            return False
        # LOTTERY TIER: вертикаль m5 60-150% (HYPERCAT +73% мазал мимо кэпа 60%).
        # Билет $1.5, не позиция: риск bounded, верх открыт. h24-вершины (>500%) всё равно мимо.
        if 60.0 <= m5 <= 150.0 and h24 <= 500.0:
            if (b5 + s5) >= 20 and (s5 == 0 or b5 >= s5) and liq >= 20000 and links:
                print(f"🎰 [{tag}-LOTTERY] {symbol}: вертикаль m5 {m5:+.1f}% — лотерейный билет.")
                self._set_sig(address, f"{tag} LOTTERY {m5:+.0f}%")
                return True
            print(f"🚫 [{tag}] {symbol}: вертикаль без давления/ликвы/ссылок — не лотерея.")
            return False
        _evm_min_m5 = getattr(config, "EVM_MIN_M5_PCT", 7.0)
        if m5 < _evm_min_m5:  # импульса нет — флет съест комиссиями
            return False
        if m5 > 60.0 or h24 > 500.0:  # вершина уже прошла
            print(f"🚫 [{tag}-OVERHEAT] {symbol}: m5 {m5:+.1f}% h24 {h24:+.0f}% — поздно.")
            return False
        if m1 > 0:
            print(f"🚫 [{tag}] {symbol}: m1 {m1:+.1f}% зелёная — ждём откат.")
            return False
        if m1 < -8.0 or h1 > 300.0:
            print(f"🚫 [{tag}] {symbol}: m1 {m1:+.1f}% h1 {h1:+.0f}% — дамп/улетел.")
            return False
        if s > 0 and b < s * 1.5:
            print(f"🚫 [{tag}] {symbol}: buys {b} / sells {s} — нет давления.")
            return False
        if (b5 + s5) < 20 or vol24 < 10000:
            print(f"🚫 [{tag}] {symbol}: тихо (tx5 {(b5+s5)}, vol24 ${vol24:,.0f}).")
            return False
        if not links:
            print(f"🚫 [{tag}] {symbol}: нет ни одной ссылки — скам-риск.")
            return False

        # Блэклист мимикрии под бренды (как в Solana-пути)
        _up = symbol.upper()
        _scam_kw = ["AAPL", "MSFT", "TSLA", "NVDA", "GOOG", "AMZN", "META", "NFLX",
                    "PEPE", "SHIB", "DOGE", "FLOKI", "BONK", "WIF", "BOME", "POPCAT", "TRUMP", "BIDEN"]
        if any(k in _up for k in _scam_kw):
            print(f"🚫 [{tag}] {symbol}: мимикрия под бренд/мем — 100% скам.")
            return False

        # Клон-чек: двойник С ТОЙ ЖЕ сети старше/жирнее = скам.
        # Та же монета на ДРУГОЙ сети (MONITOR sol+robinhood) = мультчейн, разрешаем.
        if len(symbol) > 2:
            try:
                from http_client import fetch_json as _fj
                _st, _sd = await _fj(f"https://api.dexscreener.com/latest/dex/search?q={symbol}",
                                     timeout=8, retries=1)
                if _st == 200 and _sd:
                    _mine_addr = (base.get("address") or "").lower()
                    _mine_created = pair_data.get("pairCreatedAt", 0) or 0
                    _mine_fdv = pair_data.get("fdv", 0) or 0
                    for _p in (_sd.get("pairs") or []):
                        if (_p.get("chainId") or "").lower() != chain.lower():
                            continue  # другая сеть - мультчейн, ок
                        _ps = ((_p.get("baseToken") or {}).get("symbol") or "").upper()
                        _pa = ((_p.get("baseToken") or {}).get("address") or "").lower()
                        if _ps == _up and _pa and _pa != _mine_addr:
                            _pc_at = _p.get("pairCreatedAt", 0) or 0
                            _pf = _p.get("fdv", 0) or 0
                            if (_pc_at and _mine_created and _pc_at < _mine_created and _pf > 250000) or \
                                    (_pf > (_mine_fdv * 10) and _pf > 500000):
                                print(f"🚫 [{tag}] {symbol}: клон (оригинал {str(_pa)[:10]} старше/жирнее).")
                                return False
            except Exception:
                pass

        score = 50.0
        score += min(m5, 60.0) * 0.4          # импульс до +24
        if s > 0:
            score += min(b / s, 3.0) * 6.0   # давление до +18
        score += min(liq / 50000.0, 1.0) * 8.0  # ликва до +8
        if len(links) >= 2:
            score += 5.0
        score = min(score, 100.0)
        print(f"🔵 [{tag}] {symbol}: m5 {m5:+.1f}% b/s {b}/{s} liq ${liq:,.0f} → score {score:.0f}")
        if score >= 60.0:
            self._set_sig(address, f"{tag} rule {score:.0f}%")
            return True
        return False
        
    async def analyze_growth_token(self, mint: str) -> bool:
        """GROWTH MODE: вход в откат часового тренда зрелого капа.
        Окна H1/H24 (не m5!), широкие допуски. Возвращает True/False,
        метка в signals[mint] = 'GROWTH h1+..%'."""
        import config as _c
        self.signals.pop(mint, None)
        pair_data = await self.fetch_token_data(mint)
        if not pair_data:
            return None
        base = pair_data.get("baseToken", {}) or {}
        symbol = base.get("symbol", mint[:6])
        pc = pair_data.get("priceChange") or {}
        h1 = pc.get("h1", 0) or 0
        h6 = pc.get("h6", 0) or 0
        h24 = pc.get("h24", 0) or 0
        txh1 = (pair_data.get("txns") or {}).get("h1", {}) or {}
        b, s = txh1.get("buys", 0) or 0, txh1.get("sells", 0) or 0
        liq = (pair_data.get("liquidity") or {}).get("usd", 0) or 0
        if liq < getattr(_c, "GROWTH_MIN_LIQ", 200000):
            return False
        if h24 < getattr(_c, "GROWTH_MIN_H24_PCT", 5.0):
            print(f"🌱 [GROWTH] {symbol}: h24 {h24:+.1f}% — тренда вверх нет.")
            return False
        if not (getattr(_c, "GROWTH_MIN_H1_PCT", 1.0) <= h1 <= getattr(_c, "GROWTH_MAX_H1_PCT", 15.0)):
            print(f"🌱 [GROWTH] {symbol}: h1 {h1:+.1f}% вне окна входа.")
            return False
        if h6 > getattr(_c, "GROWTH_MAX_H6_PCT", 80.0):
            print(f"🌱 [GROWTH] {symbol}: h6 {h6:+.0f}% — перегрев.")
            return False
        # Ускорение: часовой темп не слабее среднего за 6ч (h1 >= h6/6).
        # SPX/BOME входили в затухающий тренд и гнили - теперь только разгон.
        if h6 > 0 and h1 < h6 / 6.0:
            print(f"🌱 [GROWTH] {symbol}: h1 {h1:+.1f}% < h6/6 ({h6/6.0:+.1f}%) — тренд затухает.")
            return False
        _mult = getattr(_c, "GROWTH_MIN_BUYSELL", 1.2)
        if s > 0 and b < s * _mult:
            print(f"🌱 [GROWTH] {symbol}: buys {b} / sells {s} — покупатели слабее {_mult}x.")
            return False
        if (b + s) < 50:
            print(f"🌱 [GROWTH] {symbol}: тихо ({b+s} сделок/час).")
            return False
        self._set_sig(mint, f"GROWTH h1{h1:+.1f}% h24{h24:+.0f}%")
        print(f"🌱 [GROWTH-CANDIDATE] {symbol}: h1 {h1:+.1f}% h24 {h24:+.0f}% b/s {b}/{s} liq ${liq:,.0f}")
        return True

    def conviction_size_mult(self, pair_data: dict) -> float:
        """Множитель сайза по подтверждённому импульсу (НЕ по скору модели -
        модель всем ставит 100%, а катастрофы были VIP-100%).
        Conviction = m5 в окне + давление покупок + толстый пул. Иначе 1.0."""
        try:
            import config as _c
            pc = pair_data.get("priceChange") or {}
            m5 = (pc.get("m5", 0) or 0) / 100.0
            txm5 = (pair_data.get("txns") or {}).get("m5", {}) or {}
            b5, s5 = txm5.get("buys", 0) or 0, txm5.get("sells", 0) or 0
            liq = (pair_data.get("liquidity") or {}).get("usd", 0) or 0
            if (getattr(_c, "CONVICTION_MIN_M5_PCT", 0.20) <= m5 <= getattr(_c, "CONVICTION_MAX_M5_PCT", 0.60)
                    and s5 > 0 and b5 >= s5 * getattr(_c, "CONVICTION_MIN_BUYSELL", 2.0)
                    and liq >= getattr(_c, "CONVICTION_MIN_LIQ", 30000)):
                return float(getattr(_c, "CONVICTION_MULT", 2.0))
        except Exception:
            pass
        return 1.0

    async def analyze_token_xgboost(self, mint: str, pair_data: dict) -> bool:
        if not pair_data:
            return False
            
        # 1. Проверяем, что токен все еще на Pump.fun (не ушел на Raydium)
        if pair_data.get("dexId") != "pump":
            return False
            
        is_vip = self.check_hyper_rocket_momentum(pair_data)
        if is_vip:
            print(f"🚀🚀🚀 [HYPER-ROCKET BYPASS] Токен {mint} летит в космос! Игнорируем карантин возраста и соцсетей.")
            
        # 2. Проверяем возраст токена (только для обычных монет)
        created_at = pair_data.get("pairCreatedAt")
        if created_at and not is_vip:
            age_minutes = (time.time() * 1000 - created_at) / (1000 * 60)
            if age_minutes > 15:  # Игнорируем токены старше 15 минут
                return False
                
        import pandas as pd
        import joblib
        from pump_fun_sniper import PumpFunSniper
        
        txns_m5 = pair_data.get("txns", {}).get("m5", {})
        buys_m5 = txns_m5.get("buys", 0)
        sells_m5 = txns_m5.get("sells", 0)
            
        tx_velocity_1m = (buys_m5 + sells_m5) / 5.0
        
        info = pair_data.get("info", {})
        socials = info.get("socials", [])
        websites = info.get("websites", [])
        has_twitter = any("twitter" in s.get("type", "").lower() or "x.com" in s.get("url", "").lower() for s in socials)
        has_tg = any("telegram" in s.get("type", "").lower() or "t.me" in s.get("url", "").lower() for s in socials)
        has_website = len(websites) > 0
        has_socials = 1 if (has_twitter and (has_website or has_tg)) else 0
        
        top_10_holding_pct = getattr(self, '_last_top10', 0.0)
        dev_holding_pct = getattr(self, '_last_dev', 0.0)
        
        funded_from_cex = 0
        
        features = pd.DataFrame([{
            "dev_holding_pct": dev_holding_pct,
            "top_10_holding_pct": top_10_holding_pct,
            "tx_velocity_1m": tx_velocity_1m,
            "has_socials": has_socials,
            "funded_from_cex": funded_from_cex
        }])
        
        if self.pump_model is None:
            return False # Fail-safe если модель не загрузилась
        prob = self.pump_model.predict_proba(features)[0][1]
        conf = float(prob) * 100
        print(f"🤖 XGBoost [DEX Poller]: {mint} | Score: {conf:.1f}%")
        import config
        threshold = 45.0  # Было 15.0 - пропускало мусор. 45% - компромисс: не 65% чтобы не зажать, но режет скам
        if is_vip:
            threshold = 45.0  # Было 30.0: все катастрофы (FIBONACCI -68%, Goblin, CATANA) - VIP-входы. Та же планка для всех
            print(f"🔥 [VIP] Порог XGBoost как у всех: {threshold}%")

        ok = bool(conf >= threshold)
        if ok:
            self._set_sig(mint, f"{'VIP-' if is_vip else ''}PUMP-XGB {conf:.0f}%")
        return ok

    async def analyze_token_raydium(self, mint: str, pair_data: dict) -> bool:
        # Безлимитный режим: используем ТОЛЬКО данные DexScreener
        if not pair_data:
            return False

        # FRESHNESS GATE (не-VIP): модель смотрит h24-окно и ставит 100% даже дохлым
        # монетам (FIBONACCI -68%, paws -51%, HUSKY -30%). Живой токен торгуется СЕЙЧАС:
        # требуем свежего m5-объёма. Победитель MC +83% шёл с живым m5 - он проходит.
        _hyper_fresh = self.check_hyper_rocket_momentum(pair_data)
        if not _hyper_fresh:
            _txm5 = (pair_data.get("txns") or {}).get("m5", {}) or {}
            _b5, _s5 = _txm5.get("buys", 0) or 0, _txm5.get("sells", 0) or 0
            _vm5 = (pair_data.get("volume") or {}).get("m5", 0) or 0
            if (_b5 + _s5) < 20 or _vm5 < 2000 or (_s5 > 0 and _b5 < _s5):
                print(f"🚫 [FRESH] {mint[:8]}: m5 мёртв (b/s {_b5}/{_s5}, vol ${_vm5:,.0f}) — h24 может врать, модель пропустит.")
                return False

        import pandas as pd
        import joblib
        
        # Извлекаем признаки
        txns_h24 = pair_data.get("txns", {}).get("h24", {})
        buys_h24 = txns_h24.get("buys", 0)
        sells_h24 = txns_h24.get("sells", 0)
        
        volume_h24 = pair_data.get("volume", {}).get("h24", 0)
        price_change_h24 = pair_data.get("priceChange", {}).get("h24", 0)
        
        liquidity = pair_data.get("liquidity", {}).get("usd", 0)
        fdv = pair_data.get("fdv", 0)
        
        buy_sell_ratio = buys_h24 / (sells_h24 + 1)
        vol_to_liq = volume_h24 / (liquidity + 1)
        
        # Формируем DataFrame для XGBoost
        features = ['price_change_h24', 'volume_h24', 'buys_h24', 'sells_h24', 'liquidity', 'fdv', 'buy_sell_ratio', 'vol_to_liq']
        df = pd.DataFrame([{
            'price_change_h24': price_change_h24,
            'volume_h24': volume_h24,
            'buys_h24': buys_h24,
            'sells_h24': sells_h24,
            'liquidity': liquidity,
            'fdv': fdv,
            'buy_sell_ratio': buy_sell_ratio,
            'vol_to_liq': vol_to_liq
        }])
        
        try:
            if self.raydium_model is None: return False
            prob = self.raydium_model.predict_proba(df)[0][1]
            conf = prob * 100
            
            # --- ИНТЕГРАЦИЯ LUNARCRUSH ---
            symbol = pair_data.get("baseToken", {}).get("symbol", "")
            if symbol:
                lc_data = await self.fetch_lunarcrush_sentiment(symbol)
                if lc_data:
                    interactions = lc_data.get("interactions", 0)
                    sentiment = lc_data.get("sentiment", 50)
                    print(f"🌕 [LunarCrush] {symbol}: Interactions: {interactions}, Sentiment: {sentiment}%")
                    
                    if sentiment >= 70 and interactions > 500:
                        conf += 15.0 # Бустим уверенность ИИ за счет сильного социального хайпа!
                        print(f"📈 [LunarCrush] Хайп подтвержден! Буст +15% к Score.")
                    elif sentiment < 30:
                        conf -= 20.0
                        print(f"📉 [LunarCrush] Негативный сентимент! Штраф -20% к Score.")
            # -------------------------------
            
            print(f"🧠 Raydium XGBoost (Безлимит): {mint} | Score: {conf:.1f}%")
            import config; threshold = 45.0  # Было 15.0 - пропускало мусор
            _hyper = self.check_hyper_rocket_momentum(pair_data)
            # VIP-скидок больше нет: FIBONACCI/CATANA/Goblin зашли по сниженному порогу и слили. Та же планка.

            is_buy = bool(conf >= threshold)
            if is_buy:
                self._set_sig(mint, f"{'VIP-' if _hyper else ''}RAY-XGB {conf:.0f}%")
            
            if not is_buy:
                try:
                    from shadow_tracker import ShadowTracker
                    shadow = ShadowTracker()
                    price = float(pair_data.get("priceUsd", 0))
                    # Пишем Raydium FOMO-монеты в ту же таблицу shadow_log для дальнейшего анализа
                    shadow.log_rejection(
                        mint=mint,
                        reason=f"FOMO XGBoost low score: {conf:.1f}%",
                        score=conf,
                        price=price,
                        features=df.iloc[0].to_dict()
                    )
                except Exception as e:
                    print(f"Ошибка записи в ShadowTracker (Raydium): {e}")
                    
            return is_buy
        except Exception as e:
            print(f"⚠️ Ошибка XGBoost (analyze_token_raydium) для {mint}: {e}")
            return False
    async def analyze_token_ws(self, ws_data: dict) -> bool:
        mint = ws_data.get("mint")
        symbol = ws_data.get("symbol", "UNKNOWN")
        name = ws_data.get("name", "Unknown")
        
        # Читаем соцсети прямо из смарт-контракта (создатель обязан их указать при деплое на Pump.fun)
        has_twitter = bool(ws_data.get("twitter"))
        has_telegram = bool(ws_data.get("telegram"))
        has_website = bool(ws_data.get("website"))
        socials_count = sum([has_twitter, has_telegram, has_website])
        
        # Расчет стартовой ликвидности из кривой Bonding Curve
        v_sol = ws_data.get("vSolInBondingCurve", 30.0)
        from sol_price import get_sol_price_sync
        sol_price = get_sol_price_sync()
        liq_usd = v_sol * sol_price
        
        initial_buy = ws_data.get("initialBuy", 0)
        
        safety_score = 40
        momentum_score = 40 if initial_buy > 0 else 20
        
        # Social Score теперь зависит от того, сколько ссылок создатель прикрепил к контракту
        social_score = 10
        if has_twitter: social_score += 30
        if has_telegram: social_score += 30
        if has_website: social_score += 30
        social_score = min(100, social_score)
        
        alpha_score = int((safety_score * 0.35) + (momentum_score * 0.40) + (social_score * 0.25))
        
        self._save_scanned_token({
            "symbol": symbol,
            "mint": mint,
            "score": alpha_score,
            "safety": safety_score,
            "momentum": momentum_score,
            "social": social_score,
            "liquidity": liq_usd,
            "vol_24h": 0,
            "buys": 1 if initial_buy > 0 else 0,
            "sells": 0,
            "m5_change": 0,
            "age_mins": "0m (WSS)",
            "time": time.time()
        })
        
        print(f"📡 [WSS SNIPER] Пойман токен: {name} (${symbol}) | Liq: ${liq_usd:.0f} | Socials: {socials_count}")
        
        # СТРОГИЕ ФИЛЬТРЫ ДЛЯ 0-СЕКУНДНЫХ МОНЕТ
        
        # 1. Защита от ленивых скаммеров (мусор без соцсетей)
        if socials_count == 0:
            print(f"🚫 [WSS] Отказ: Создатель {symbol} даже не прикрепил соцсети. 100% мусор.")
            return False
            
        # 2. Skin in the game & Анти-монополия (Initial Buy)
        # PumpPortal отдает initialBuy в SOL. Требуем от 0.1 до 5 SOL.
        if initial_buy < 0.1:
            print(f"🚫 [WSS] Отказ: Создатель вкинул слишком мало ({initial_buy} SOL). У него нет 'шкуры на кону'.")
            return False
        if initial_buy > 5.0:
            print(f"🚫 [WSS] Отказ: Создатель выкупил слишком много токенов ({initial_buy} SOL). Высокий риск монопольного дампа.")
            return False
            
        # 3. Проверка разработчика и бандлов будет произведена позже или через Helius.
            
        # 4. Проверка кошелька разработчика (Helius RPC) и метаданных IPFS
        trader_pubkey = ws_data.get("traderPublicKey")
        uri = ws_data.get("uri")
        
        dev_balance_sol = 0
        description = ""
        
        import aiohttp
        session = await self.get_session()
        if True:
            # Запрос баланса к Helius
            if trader_pubkey:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getAccountInfo",
                    "params": [trader_pubkey, {"encoding": "jsonParsed"}]
                }
                try:
                    async with session.post(config.HELIUS_RPC_URL, json=payload, timeout=2) as resp:
                        data = await resp.json()
                        lamports = data.get("result", {}).get("value", {}).get("lamports", 0) if data.get("result", {}).get("value") else 0
                        dev_balance_sol = lamports / 1e9
                except Exception as e:
                    print(f"⚠️ Ошибка RPC баланса: {e}")
                    
            # Загрузка метаданных IPFS
            if uri:
                try:
                    async with session.get(uri, timeout=2) as resp:
                        meta = await resp.json()
                        description = meta.get("description", "").lower()
                except Exception:
                    pass
                    
        if dev_balance_sol < 0.1:
            print(f"🚫 [WSS] Отказ: Кошелек разработчика пуст ({dev_balance_sol:.2f} SOL). Скаммер-однодневка.")
            return False
            
        bad_words = ["test", "scam", "fuck", "shit", "nigger", "pump and dump", "rug"]
        if any(word in description for word in bad_words) or len(description) < 5:
            print(f"🚫 [WSS] Отказ: Мусорное описание на IPFS (спам/короткое).")
            return False
            
        print(f"🚀 [WSS СИГНАЛ] Входим в токен {symbol} на нулевой секунде! (Dev Wallet: {dev_balance_sol:.2f} SOL, Socials: {socials_count})")
        
        # Эмуляция цены (записываем цену в usd в словарь, чтобы main.py мог ее взять)
        v_tok = ws_data.get("vTokensInBondingCurve", 1073000000.0)
        ws_data["priceUsd"] = (v_sol / v_tok) * sol_price
        return True

    async def fetch_lunarcrush_sentiment(self, symbol: str) -> dict:
        """
        Проверяет хайп (Social Sentiment) монеты в Twitter через LunarCrush.
        """
        import config
        api_key = getattr(config, "LUNARCRUSH_API_KEY", "")
        if not api_key:
            return {}
            
        url = f"https://lunarcrush.com/api4/public/coins/{symbol}/v1"
        headers = {"Authorization": f"Bearer {api_key}"}
        
        session = await self.get_session()
        if True:
            try:
                async with session.get(url, headers=headers, timeout=3) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        coin_data = data.get("data", {})
                        
                        return {
                            "social_volume": coin_data.get("social_volume_24h", 0),
                            "interactions": coin_data.get("interactions_24h", 0),
                            "sentiment": coin_data.get("sentiment", 50)
                        }
            except Exception as e:
                pass
        return {}
