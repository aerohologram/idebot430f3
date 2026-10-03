"""Jupiter Tokens API v2 — дискавери Solana: свежие листинги + топ трендов 5m.

Эндпоинты (нужен бесплатный x-api-key с portal.jup.ag):
- GET /tokens/v2/recent — токены с первым пулом
- GET /tokens/v2/toptrending/5m?limit=100 — макс. движение за 5 мин
- GET /tokens/v2/toptraded/1h?limit=50 — макс. объём за час

Без JUP_API_KEY модуль спит. Кэш 120 сек.
Адаптировано под текущий анализатор (без log_scan/pack_features).
"""
import asyncio
import time

import config
from http_client import fetch_json

BASE = "https://api.jup.ag/tokens/v2"

_cache = []
_cache_time = 0.0
CACHE_TTL = 120


def _headers():
    key = getattr(config, "JUP_API_KEY", "") or ""
    return {"x-api-key": key} if key else {}


async def fetch_jup_tokens() -> list:
    """Возвращает [mint, ...] Solana: recent + toptrending/5m (дедуп, топ по organicScore)."""
    global _cache, _cache_time
    if time.time() - _cache_time < CACHE_TTL and _cache:
        return _cache
    if not (getattr(config, "JUP_API_KEY", "") or ""):
        return []
    out, seen, scored = [], set(), []
    for path in ("/recent",
                 "/toptrending/5m?limit=100",
                 "/toptraded/1h?limit=50"):
        status, data = await fetch_json(BASE + path, headers=_headers(),
                                        timeout=12, retries=1)
        if status != 200 or not isinstance(data, list):
            continue
        for t in data:
            m = t.get("id", "")
            if not m or m in seen:
                continue
            seen.add(m)
            scored.append((float(t.get("organicScore", 0) or 0), m))
    scored.sort(reverse=True)
    out = [m for _, m in scored][:120]
    _cache, _cache_time = out, time.time()
    return out


async def jup_loop(analyzer, tracker):
    """Петля Jupiter-дискавери (Solana). Молчит без JUP_API_KEY."""
    if not (getattr(config, "JUP_API_KEY", "") or ""):
        return
    print("🪐 Jupiter Discovery запущен: recent + toptrending/5m (Solana)!")
    processed = {}
    interval = int(getattr(config, "JUP_INTERVAL", 120))
    while True:
        try:
            if len(tracker.get_open_positions()) < config.MAX_CONCURRENT_POSITIONS:
                import time as _t
                mints = await fetch_jup_tokens()
                for mint in mints:
                    if not mint or mint in tracker.positions:
                        continue
                    if _t.time() - processed.get(mint, 0.0) < 600:
                        continue
                    try:
                        ok = await analyzer.analyze_token(mint)
                    except Exception as e:
                        print(f"🪐 JUP analyze err {mint[:8]}: {type(e).__name__}")
                        continue
                    if ok is None:
                        continue
                    processed[mint] = _t.time()
                    if ok is True and len(tracker.get_open_positions()) < config.MAX_CONCURRENT_POSITIONS:
                        from jupiter import JupiterAPI
                        price = await JupiterAPI.get_price(mint)
                        if price and price > 0:
                            cap = tracker.get_total_capital()
                            size = max(4.0, min(100.0, cap * (config.REINVEST_PERCENT / 100.0))) if cap > 0 else 4.0
                            td = await analyzer.fetch_token_data(mint)
                            try:
                                size *= analyzer.conviction_size_mult(td or {})
                            except Exception:
                                pass
                            tracker.add_position(
                                mint[:4], mint, price, min(size, 100.0),
                                is_mature=True,
                                source=f"JUP:{analyzer.get_signal(mint)}")
                            print(f"🪐 JUP BUY {mint[:8]} @ ${price}")
                            break
                    await asyncio.sleep(1.0)
                if len(processed) > 1000:
                    processed.clear()
        except Exception as e:
            print(f"Ошибка в jup_loop: {e}")
        await asyncio.sleep(interval)
