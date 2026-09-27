"""Единый HTTP-клиент: общая сессия + per-host rate limiter + fetch с ретраями.

Зачем: каждый модуль раньше долбил API без лимитов и падал на первом 429/timeout.
Теперь все идут через этот модуль: соблюдение квот, backoff на 429/5xx, ротация.
"""
import aiohttp
import asyncio
import time

_session = None

# Лимиты бесплатных API (запросов в минуту на весь процесс).
# DexScreener пары/поиск ~300/мин, профили/бусты ~60/мин,
# GeckoTerminal ~30/мин, DeFiLlama без ключа - щадим, Jupiter keyless ~30/мин (0.5 RPS).
HOST_LIMITS = {
    "api.dexscreener.com": 240.0,
    "api.geckoterminal.com": 25.0,
    "coins.llama.fi": 60.0,
    "api.llama.fi": 60.0,
    "api.coinbase.com": 60.0,
    "api.kraken.com": 60.0,
    "api.jup.ag": 25.0,
    "lite-api.jup.ag": 25.0,
    "tokens.jup.ag": 20.0,
    "quote-api.jup.ag": 20.0,
    "frontend-api.pump.fun": 20.0,
    "public-api.birdeye.so": 30.0,
    "lunarcrush.com": 10.0,
    "api.rugcheck.xyz": 30.0,
}

_last_call: dict = {}
_locks: dict = {}


def _host_lock(host: str) -> asyncio.Lock:
    lock = _locks.get(host)
    if lock is None:
        lock = asyncio.Lock()
        _locks[host] = lock
    return lock


async def get_session():
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=20, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=15, connect=8)
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _session


async def close_session():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()


def _throttle_delay(url: str) -> float:
    """Сколько секунд ждать перед запросом на этот хост."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        return 0.0
    limit = 0.0
    for h, rpm in HOST_LIMITS.items():
        if host == h or host.endswith("." + h):
            limit = rpm
            break
    if not limit:
        return 0.0
    min_interval = 60.0 / limit
    now = time.monotonic()
    last = _last_call.get(host, 0.0)
    wait = min_interval - (now - last)
    return max(0.0, wait)


def _mark_call(url: str):
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        return
    _last_call[host] = time.monotonic()


async def fetch_json(url: str, params=None, headers=None, timeout: int = 12,
                     retries: int = 3, method: str = "GET", json_payload=None):
    """GET/POST JSON с троттлингом, ретраями и backoff на 429/5xx.

    Возвращает (status, data). data={} при неудаче. Никогда не кидает исключение.
    """
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or "?"
    except Exception:
        host = "?"
    session = await get_session()
    hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    last_status, last_err = 0, "?"
    for attempt in range(retries + 1):
        async with _host_lock(host):
            wait = _throttle_delay(url)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                if method == "POST":
                    ctx = session.post(url, params=params, headers=hdrs,
                                       json=json_payload, timeout=timeout)
                else:
                    ctx = session.get(url, params=params, headers=hdrs, timeout=timeout)
                async with ctx as r:
                    _mark_call(url)
                    last_status = r.status
                    if r.status == 200:
                        try:
                            return r.status, await r.json(content_type=None)
                        except Exception:
                            return r.status, {}
                    if r.status == 404:
                        return r.status, {}
                    last_err = f"HTTP {r.status}"
            except asyncio.TimeoutError:
                _mark_call(url)
                last_err = "timeout"
            except Exception as e:
                _mark_call(url)
                last_err = f"{type(e).__name__}"
        if attempt < retries:
            # 429/5xx/сеть: 2с -> 5с -> 10с
            await asyncio.sleep(2 + 3 * attempt)
    print(f"🔌 API fail {host}{urlparse(url).path[:50]}: {last_err} (status {last_status})")
    return last_status, {}
