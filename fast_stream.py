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

# mint -> {first_seen, name, symbol, dev, initial_buy_sol, buyers:set, trades:int, vol_sol:float, last_mcap_sol:float}
_registry = {}
TRADES_SEEN = 0
TOKENS_SEEN = 0
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
        global TOKENS_SEEN
        TOKENS_SEEN += 1
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
            "last_mcap_sol": float(data.get("marketCapSol", 0) or 0),
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
    global TRADES_SEEN
    TRADES_SEEN += 1
    r["trades"] += 1
    r["last_trade_ts"] = time.time()
    try:
        _mc = float(data.get("marketCapSol", 0) or 0)
        if _mc > 0:
            r["last_mcap_sol"] = _mc
    except Exception:
        pass
    if tx_type == "buy":
        if trader:
            r["buyers"].add(trader)
        r["vol_sol"] += abs(sol_amt)
    # Поминутные корзины для acceleration: отличаем разгон FOMO от вялой капели.
    # Ракеты до взлёта выглядят как ускорение 2-й минуты к 1-й, а не абсолютные цифры.
    try:
        _now = time.time()
        _bkt = int(_now // 60)
        _bkts = r.setdefault("buckets", {})
        _b = _bkts.get(_bkt)
        if _b is None:
            _b = {"trades": 0, "vol": 0.0, "buyers": set(), "mcap": 0.0}
            _bkts[_bkt] = _b
        _b["trades"] += 1
        if tx_type == "buy":
            _b["vol"] += abs(sol_amt)
            if trader:
                _b["buyers"].add(trader)
        try:
            _mc2 = float(data.get("marketCapSol", 0) or 0)
            if _mc2 > 0:
                _b["mcap"] = _mc2
        except Exception:
            pass
        # Чистим старше 12 мин (память под контролем при 300 минтах)
        for _k in [k for k in _bkts if _k < _bkt - 12]:
            _bkts.pop(_k, None)
    except Exception:
        pass


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
    return {"buyers": len(r.get("buyers", ())), "trades": r.get("trades", 0),
            "age_sec": time.time() - r.get("first_seen", 0),
            "last_mcap_sol": r.get("last_mcap_sol", 0)}


def get_accelerating(min_age_sec: int = 60, max_age_sec: int = 720) -> list:
    """Ракеты до взлёта: ускорение 2-й минуты к 1-й по трейдам/покупателям/объёму.

    Общее у всех наших +100-1600% до роста (CCLAW, EQMODE, WINSTREAK, STMINT):
    цена ещё плоская (m5 мал), а поток покупателей уже разгоняется.
    Абсолютные пороги (buyers>=5) это ловят поздно — тут именно наклон.
    Возвращает [(mint, score, age_sec, buyers, trades), ...] по убыванию score.
    """
    now = time.time()
    cur_bkt = int(now // 60)
    out = []
    for mint, r in list(_registry.items()):
        try:
            age = now - r.get("first_seen", 0)
            if age < min_age_sec or age > max_age_sec:
                continue
            bkts = r.get("buckets") or {}
            b1 = bkts.get(cur_bkt - 1) or {}
            b2 = bkts.get(cur_bkt) or {}
            t1, t2 = b1.get("trades", 0), b2.get("trades", 0)
            u1, u2 = len(b1.get("buyers", ())), len(b2.get("buyers", ()))
            v1, v2 = b1.get("vol", 0.0), b2.get("vol", 0.0)
            # Нужен минимум потока в обеих минутах, иначе делим шум на ноль
            if t1 + t2 < 8 or u1 + u2 < 4:
                continue
            # Одна whale-свеча без покупателей = wash, не FOMO
            if u2 < 2 and t2 >= 10:
                continue
            tr_acc = t2 / max(1.0, float(t1))
            bu_acc = u2 / max(1.0, float(u1))
            vo_acc = v2 / max(0.001, float(v1)) if (v1 + v2) > 0 else 1.0
            # Разгон только вверх: затухание (acc<1) не ранжируем
            if tr_acc < 1.2 and bu_acc < 1.2:
                continue
            score = tr_acc * 1.0 + bu_acc * 1.5 + min(vo_acc, 5.0) * 0.5
            # Бонус за растущую капу bonding-кривой (mcap ползёт к градуации)
            m1cap = b1.get("mcap", 0) or 0
            m2cap = b2.get("mcap", 0) or 0
            if m2cap > m1cap > 0:
                score += 0.5
            out.append((mint, round(score, 2), age,
                        len(r.get("buyers", ())), r.get("trades", 0)))
        except Exception:
            continue
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:30]


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
                # отдельный таск: держим ОДНУ подписку на trades свежих минтов.
                # PumpPortal заменяет подписку каждым сообщением, поэтому шлём весь
                # список ключей разом (до 50 самых свежих) каждые 10с, а не по одному.
                async def resub_trades():
                    last_keys = []
                    while True:
                        try:
                            fresh = get_fresh_mints(180)
                            # самые свежие первые
                            fresh_sorted = sorted(fresh, key=lambda m: _registry.get(m, {}).get("first_seen", 0), reverse=True)[:50]
                            if fresh_sorted != last_keys and fresh_sorted:
                                try:
                                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": fresh_sorted}))
                                    last_keys = fresh_sorted
                                    print(f"⚡ [FAST] Подписан на trades {len(fresh_sorted)} свежих минтов")
                                except Exception:
                                    break
                            await asyncio.sleep(10)
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            await asyncio.sleep(10)
                async def heartbeat():
                    while True:
                        try:
                            await asyncio.sleep(300)
                            n_hot = len(get_hot_fresh())
                            print(f"💓 [FAST] heartbeat: токенов 0-сек={TOKENS_SEEN}, трейдов={TRADES_SEEN}, в реестре={len(_registry)}, горячих={n_hot}")
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            pass
                resub = asyncio.create_task(resub_trades())
                hb = asyncio.create_task(heartbeat())
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
                    hb.cancel()
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
                    if is_good is None:
                        # DexScreener ещё не проиндексировал (0-3 мин), но тяга по WSS есть.
                        # Early-bird лотерейный микро-вход вместо ожидания (иначе ракета улетает
                        # пока ждём API). Размер как LOTTERY, цена через Jupiter.
                        _early_max_age = getattr(config, "FAST_EARLY_MAX_AGE_SEC", 180)
                        if st["age_sec"] <= _early_max_age:
                            _early_size = getattr(config, "TRADE_AMOUNT_USD", 10.0) * getattr(config, "LOTTERY_SIZE_MULT", 0.25)
                            try:
                                from jupiter import JupiterAPI
                                _px = await JupiterAPI.get_prices([mint])
                                entry_price = (_px or {}).get(mint, 0.0) or 0.0
                            except Exception:
                                entry_price = 0.0
                            if entry_price > 0 and _early_size >= 1.0:
                                _deployed = sum(getattr(q, "amount_usd", 0) for q in tracker.get_open_positions().values())
                                _cap = tracker.get_total_capital() * getattr(config, "MAX_DEPLOYED_PCT", 0.60)
                                if _deployed + _early_size <= _cap:
                                    tracker.add_position("FAST-EARLY", mint, entry_price, _early_size, is_mature=True,
                                                         source=f"FAST-EARLY:b{st['buyers']}t{st['trades']}")
                                    print(f"⚡ [FAST-EARLY] Микро-вход {mint[:10]}... ${entry_price:.8f} x${_early_size:.1f} (DS пуст, тяга WSS)")
                                    break
                                else:
                                    print(f"⚡ [FAST-EARLY] {mint[:10]}... пропуск: exposure забит")
                            else:
                                print(f"⚡ [FAST-EARLY] {mint[:10]}... пропуск: цены ещё нет нигде (Jupiter=0)")
                        else:
                            print(f"⚡ [FAST] {mint[:10]}... DS пуст и возраст {st['age_sec']:.0f}с > лимита — жду индексацию")
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
