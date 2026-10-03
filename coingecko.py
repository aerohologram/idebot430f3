"""CoinGecko trending → Solana-минты для GROWTH-вселенной.

Зачем: growth-режим ест только вотчлист + топ Raydium по резерву — трендовые
капы (ротации рынка) пролетают мимо. CoinGecko search/trending бесплатен,
без ключа; резолв id → platforms.solana даёт минт. Нагрузка: 1 запрос
трендов + до 7 резолвов раз в 30 мин — квота free (5-15/мин) цела.

Всё через fetch_json (семафор + троттлинг). Любая беда — fail-open [].
"""
import time as _t

BASE = "https://api.coingecko.com/api/v3"

_cache_mints = []
_cache_time = 0.0
CACHE_TTL = 1800  # 30 мин
MAX_RESOLVE = 7


def _cfg(name, default):
    try:
        import config as _c
        return getattr(_c, name, default)
    except Exception:
        return default


async def trending_solana_mints(limit: int = 7) -> list:
    """Минты Solana из CoinGecko trending. [] при любой беде."""
    global _cache_mints, _cache_time
    if not _cfg("COINGECKO_ENABLED", True):
        return []
    if _t.time() - _cache_time < CACHE_TTL and _cache_mints:
        return list(_cache_mints)
    out = []
    try:
        from http_client import fetch_json
        status, data = await fetch_json(f"{BASE}/search/trending",
                                        timeout=12, retries=1)
        if status != 200 or not isinstance(data, dict):
            return list(_cache_mints)
        ids = []
        for c in (data.get("coins") or [])[:15]:
            _id = ((c or {}).get("item") or {}).get("id", "")
            if _id and _id not in ids:
                ids.append(_id)
        for _id in ids[: max(1, min(limit, MAX_RESOLVE))]:
            try:
                st, cd = await fetch_json(
                    f"{BASE}/coins/{_id}?localization=false&tickers=false"
                    "&market_data=false&community_data=false"
                    "&developer_data=false&sparkline=false",
                    timeout=12, retries=1)
                if st == 429:
                    break  # квота — до следующего цикла, молча
                if st != 200 or not isinstance(cd, dict):
                    continue
                mint = ((cd.get("platforms") or {}).get("solana")) or ""
                if len(mint) > 30 and mint not in out:
                    out.append(mint)
            except Exception:
                continue
        if out:
            _cache_mints, _cache_time = out, _t.time()
    except Exception:
        pass
    return out if out else list(_cache_mints)
