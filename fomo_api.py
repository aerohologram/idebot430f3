"""Мост fomoapi.io (независимое API данных fomo.family) -> fomo_signals.txt.

Что даёт (бесплатный ключ 250k кредитов/мес, без ключа — демо с задержкой 60с):
- WS /ws/alerts: покупки/продажи топ-трейдеров в реальном времени (сообщения бесплатны).
  buy-алерт с tokenAddress+chain -> строка в fomo_signals.txt -> существующая
  fomo_signal_loop прогоняет через гейты и покупает с меткой FOMO:.
- REST token boards (только с ключом, экономно): trending/graduated доски.
- Подписка на конкретных трейдеров из FOMO_FOLLOW_TRADERS.

Кредит-экономность: WS-сообщения бесплатны; REST-доски редко + стоп при остатке < 20k.
"""
import asyncio
import json
import time

API = "https://api.fomoapi.io"
_seen = {}  # (chain, addr) -> ts, дедуп 10 мин


def _cfg():
    try:
        import config
        return config
    except Exception:
        return None


def _key():
    c = _cfg()
    return (getattr(c, "FOMO_API_KEY", "") or "").strip() if c else ""


def push_signal(chain: str, addr: str, why: str = ""):
    """Положить сигнал в очередь FOMO-петли. Формат sol:<mint> | evm:<chain>:<addr>."""
    if not addr:
        return
    try:
        import config as _c
        if not getattr(_c, "USE_FOMO_SIGNALS", False):
            return
    except Exception:
        pass
    chain = (chain or "").lower()
    now = time.time()
    k = (chain, addr.lower())
    if now - _seen.get(k, 0) < 600:
        return
    _seen[k] = now
    if len(_seen) > 2000:
        _seen.clear()
    line = f"sol:{addr}" if chain == "solana" else f"evm:{chain}:{addr}"
    try:
        with open("fomo_signals.txt", "a") as f:
            f.write(line + "\n")
        print(f"📡 FOMO-API {why or 'buy'} [{chain}] {addr[:12]} -> очередь")
    except Exception as e:
        print(f"FOMO-API write err: {e}")


async def _boards_once():
    """Доски trending/graduated -> очередь (только с ключом, редко)."""
    import aiohttp
    key = _key()
    if not key:
        return
    try:
        async with aiohttp.ClientSession() as s:
            for board in ("trending", "graduated"):
                try:
                    async with s.get(
                        f"{API}/v2/leaderboard/tokens/{board}?limit=25",
                        headers={"authorization": f"Bearer {key}"},
                        timeout=15,
                    ) as r:
                        rem = r.headers.get("x-credits-remaining")
                        if rem is not None:
                            try:
                                if float(rem) < 20000:
                                    print(f"📡 FOMO-API: кредитов мало ({rem}), REST-пауза.")
                                    return
                            except Exception:
                                pass
                        if r.status != 200:
                            continue
                        d = await r.json()
                        for t in (d.get("tokens") or [])[:25]:
                            tok = t.get("token") or {}
                            a = tok.get("address", "")
                            net = str(t.get("network", "")).lower()
                            ch = {"solana": "solana", "1399811149": "solana"}.get(net, net) or "solana"
                            if a:
                                push_signal(ch, a, f"board:{board}")
                except Exception:
                    continue
    except Exception as e:
        print(f"FOMO-API boards err: {type(e).__name__}")


async def _ws_loop():
    """WS /ws/alerts: покупки топов -> очередь. Без ключа демо с задержкой 60с."""
    import websockets
    c = _cfg()
    follows = list(getattr(c, "FOMO_FOLLOW_TRADERS", []) or []) if c else []
    min_usd = float(getattr(c, "FOMO_MIN_USD", 100) or 0) if c else 100
    while True:
        try:
            key = _key()
            url = "wss://api.fomoapi.io/ws/alerts" + (f"?key={key}" if key else "")
            async with websockets.connect(url, max_size=10 ** 6, ping_interval=20) as ws:
                print(f"📡 FOMO-API WS: подключено{' (ключ)' if key else ' (демо 60с)'}")
                for tr in follows[:20]:
                    try:
                        await ws.send(json.dumps({"action": "subscribe", "trader": tr}))
                    except Exception:
                        pass
                async for message in ws:
                    try:
                        m = json.loads(message)
                    except Exception:
                        continue
                    if m.get("type") != "alert":
                        continue
                    if m.get("alertType") != "buy":
                        continue
                    try:
                        if float(m.get("usdValue") or 0) < min_usd:
                            continue
                    except Exception:
                        pass
                    addr = m.get("tokenAddress", "")
                    chain = str(m.get("chain") or "").lower()
                    if addr and chain:
                        push_signal(chain, addr,
                                    f"{m.get('trader', '?')} ${m.get('usdValue', '?')}")
        except Exception as e:
            print(f"⚠️ FOMO-API WS: {type(e).__name__}. Реконнект 10с...")
            await asyncio.sleep(10)


async def fomo_api_loop(*_a, **_k):
    """Петля моста: WS всегда + REST-доски редко (с ключом)."""
    c = _cfg()
    if c and not getattr(c, "FOMO_WS_ENABLED", True):
        return
    interval = int(getattr(c, "FOMO_BOARDS_INTERVAL", 7200) or 7200) if c else 7200
    async def boards_task():
        await asyncio.sleep(30)
        while True:
            try:
                await _boards_once()
            except Exception:
                pass
            await asyncio.sleep(interval)
    await asyncio.gather(_ws_loop(), boards_task())
