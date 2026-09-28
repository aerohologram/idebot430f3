"""⚡ FAST STREAM — 0-сек discovery (идея из DoradoDevs/solana-pumpfun-sniper-bot).

Проблема: scanner_loop спит 30с, fomo_loop 120с, источники — платные boosts/top.
Ракета за 2-5 мин пролетает мимо.

Что делает модуль:
1. Слушает PumpPortal WSS subscribeNewToken -> mint известен на 0-й секунде.
   Сразу кладёт в birth_tracker + свой реестр с metadata (initialBuy, dev, name).
2. Подписывается на trades свежих минтов (subscribeTokenTrade) и считает
   уникальных покупателей за первые FAST_WINDOW_SEC секунд.
3. Отдаёт get_hot_fresh() — минты с ранней тягой, их scanner_loop проверяет
   ВНЕ очереди и БЕЗ 600с бана (None от analyzer = "ещё рано", повторим).

Зависимости: websockets, config, birth_tracker. Отдельного API-ключа не надо.
"""
import asyncio
import json
import time
import collections
import config

try:
    from birth_tracker import birth_tracker
except Exception:
    birth_tracker = None

# mint -> {first_seen, name, symbol, dev, initial_buy_sol, buyers:set, trades:int, vol_sol:float}
_registry = {}
_registry_lock = asyncio.Lock() if False else None  # простой dict, GIL хватает
_seen_mints = collections.OrderedDict()  # для дедупликации логов

FAST_ENABLED = getattr(config, "FAST_STREAM_ENABLED", True)
FAST_WINDOW_SEC = getattr(config, "FAST_WINDOW_SEC", 120)       # окно ранней тяги
FAST_MIN_BUYERS = getattr(config, "FAST_MIN_BUYERS", 6)         # 6 уникальных за 2 мин = горячий
FAST_MIN_TRADES = getattr(config, "FAST_MIN_TRADES", 10)
MAX_TRACKED = 300


def _touch_seen(mint: str) -> bool:
    """True если mint новый (для лога)."""
    if mint in _seen_mints:
        return False
    _seen_mints[mint] = time.time()
    if len(_seen_mints) > 2000:
        _seen_mints.popitem(last=False)
    return True


def note_new_token(data: dict):
    """Вызывается на событие create с PumpPortal."""
    mint = data.get("mint")
    if not mint:
        return
    now = time.time()
    if mint not in _registry:
        if len(_registry) >= MAX_TRACKED:
            # чистим самые старые
            oldest = sorted(_registry.items(), key=lambda kv: kv[1]["first_seen"])[:50]
            for k, _ in oldest:
                _registry.pop(k, None)
        _registry[mint] = {
            "first_seen": now,
            "name": str(data.get("name", ""))[:40],
            "symbol": str(data.get("symbol", ""))[:20],
            "dev": data.get("traderPublicKey", ""),
            "initial_buy_sol": float(data.get("initialBuy", 0) or 0) / 1e9 if float(data.get("initialBuy", 0) or 0) > 1000 else float(data.get("initialBuy", 0) or 0),
            "buyers": set(),
            "trades": 0,
            "vol_sol": 0.0,
            "last_trade_ts": 0.0,
        }
        if birth_tracker is not None:
            try:
                birth_tracker.add_token(mint)
            except Exception:
                pass
        if _touch_seen(mint):
            print(f"⚡ [FAST] Новый токен 0-сек: {data.get('symbol','?')} {mint[:10]}... dev={str(data.get('traderPublicKey',''))[:8]}")


def note_trade(data: dict):
    """Вызывается на событие trade с PumpPortal. Формат: {mint, traderPublicKey, txType buy/sell, solAmount, ...}."""
    mint = data.get("mint")
    if not mint or mint not in _registry:
        return
    r = _registry[mint]
    trader = data.get("traderPublicKey", "")
    tx_type = str(data.get("txType", "")).lower()
    try:
        sol_amt = float(data.get("solAmount", 0) or data.get("vSolInBondingCurve", 0) or 0)
        if sol_amt > 1000:  # лампорты -> SOL
            sol_amt = sol_amt / 1e9
    except Exception:
        sol_amt = 0.0
    r["trades"] += 1
    r["last_trade_ts"] = time.time()
    if tx_type == "buy":
        if trader:
            r["buyers"].add(trader)
        r["vol_sol"] += abs(sol_amt)


def get_hot_fresh(max_age_sec: int = 0) -> list:
    """Минты с ранней тягой: возраст < FAST_WINDOW_SEC*2 и buyers>=MIN или trades>=MIN_TRADES."""
    now = time.time()
    window = max_age_sec or (FAST_WINDOW_SEC * 2)
    out = []
    for mint, r in list(_registry.items()):
        age = now - r["first_seen"]
        if age > 900:  # старше 15 мин — чистим, pump-модель всё равно режет >15 мин
            _registry.pop(mint, None)
            continue
        if age <= window and (len(r["buyers"]) >= FAST_MIN_BUYERS or r["trades"] >= FAST_MIN_TRADES):
            out.append(mint)
    return out


def get_fresh_mints(max_age_sec: int = 180) -> list:
    """Все свежие минты младше max_age_sec (для прогрева scanner_loop)."""
    now = time.time()
    return [m for m, r in list(_registry.items()) if now - r["first_seen"] <= max_age_sec]


def get_stats(mint: str) -> dict:
    r = _registry.get(mint, {})
    return {"buyers": len(r.get("buyers", ())), "trades": r.get("trades", 0), "age_sec": time.time() - r.get("first_seen", 0)}


async def fast_stream_loop():
    """Держит WSS-подписки: NewToken + TokenTrade по свежим минтам."""
    import websockets
    if not FAST_ENABLED:
        print("⚡ FAST STREAM выключен (FAST_STREAM_ENABLED=False)")
        return
    uri = getattr(config, "PUMPPORTAL_WSS", "wss://pumpportal.fun/api/data")
    headers = {"User-Agent": "Mozilla/5.0", "Origin": "https://pumpportal.fun"}
    print("⚡ FAST STREAM запущен: 0-сек discovery + трекинг ранних buyers")
    backoff = 2
    while True:
        try:
            async with websockets.connect(uri, extra_headers=headers) as ws:
                backoff = 2
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                print("⚡ [FAST] Подписан на NewToken")
                # отдельный таск: периодически подписываемся на trades свежих минтов
                async def resub_trades():
                    subscribed = set()
                    while True:
                        try:
                            fresh = get_fresh_mints(180)
                            new = [m for m in fresh if m not in subscribed][:20]
                            for m in new:
                                try:
                                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [m]}))
                                    subscribed.add(m)
                                except Exception:
                                    break
                            await asyncio.sleep(15)
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            await asyncio.sleep(15)
                resub = asyncio.create_task(resub_trades())
                try:
                    async for message in ws:
                        try:
                            data = json.loads(message)
                        except Exception:
                            continue
                        tx = str(data.get("txType", "")).lower()
                        if tx == "create" and data.get("mint"):
                            note_new_token(data)
                        elif tx in ("buy", "sell") and data.get("mint"):
                            note_trade(data)
                        elif data.get("mint") and "traderPublicKey" in data and "txType" not in data:
                            # иногда create приходит без txType
                            note_new_token({**data, "txType": "create"})
                finally:
                    resub.cancel()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"⚡ [FAST] WSS ошибка: {e}. Переподключение через {backoff}с...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def fast_candidate_loop(analyzer, tracker):
    """Каждые 10с берёт горячие свежие и сразу гонит через analyzer (мимо 600с бана)."""
    await asyncio.sleep(5)
    processed_fast = {}
    while True:
        try:
            if not FAST_ENABLED:
                await asyncio.sleep(30)
                continue
            if len(tracker.get_open_positions()) >= getattr(config, "MAX_CONCURRENT_POSITIONS", 10):
                await asyncio.sleep(10)
                continue
            hot = get_hot_fresh()
            for mint in hot:
                if mint in tracker.positions:
                    continue
                if time.time() - processed_fast.get(mint, 0) < 120:  # ретрай горячих каждые 2 мин, не 10
                    continue
                processed_fast[mint] = time.time()
                try:
                    st = get_stats(mint)
                    print(f"⚡ [FAST] Горячий свежий: {mint[:10]}... buyers={st['buyers']} trades={st['trades']} age={st['age_sec']:.0f}с — проверяю анализатором")
                    is_good = await analyzer.analyze_token(mint)
                    if is_good is True:
                        pair_data = await analyzer.fetch_token_data(mint)
                        entry_price = float((pair_data or {}).get("priceUsd", 0) or 0)
                        if entry_price > 0:
                            liq = ((pair_data or {}).get("liquidity") or {}).get("usd", 0) or 0
                            max_by_pool = liq * 0.005 if liq > 0 else 99999.0
                            fixed = getattr(config, "TRADE_AMOUNT_USD", 10.0)
                            size = min(fixed, max_by_pool) if fixed else 4.0
                            if size >= 4.0:
                                _deployed = sum(p.amount_usd for p in tracker.get_open_positions().values())
                                _cap = tracker.get_total_capital() * getattr(config, "MAX_DEPLOYED_PCT", 0.60)
                                if _deployed + size <= _cap:
                                    sym = ((pair_data or {}).get("baseToken") or {}).get("symbol", "FAST") or "FAST"
                                    tracker.add_position(sym, mint, entry_price, size, is_mature=True,
                                                         source=f"FAST:{analyzer.get_signal(mint)}")
                                    print(f"⚡ [FAST] ВХОД {sym} {mint[:10]}... ${size:.0f}")
                                    break
                except Exception as e:
                    print(f"⚡ [FAST] Ошибка {mint[:8]}: {type(e).__name__}: {e}")
            if len(processed_fast) > 1000:
                processed_fast.clear()
        except Exception as e:
            print(f"⚡ [FAST] Ошибка петли: {e}")
        await asyncio.sleep(10)
