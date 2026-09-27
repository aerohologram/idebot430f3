"""Единый источник рыночных данных.

Приоритет: GeckoTerminal (стабильно работает с Render, без Cloudflare-блока,
проверено: token, pools, trending, search, ohlcv).
DexScreener остаётся fallback'ом внутри get_token_data (fail-open).

Нормализация: get_token_data возвращает словарь в форме DexScreener-пары,
чтобы остальной код (analyzer, sniper) не менять:
  baseToken{address,symbol,name}, priceUsd, liquidity{usd},
  volume{m5,h24}, txns{m5:{buys,sells},h24:{...}}, priceChange{h24,...},
  fdv, marketCap, pairCreatedAt (ms), dexId, info{socials,websites},
  _source, _socials_unknown (True, если соцсети проверить негде).
"""
import asyncio
import time
from datetime import datetime, timezone

BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

_lock = asyncio.Lock()
_last_call = 0.0
MIN_INTERVAL = 2.0  # не чаще ~30/мин на весь процесс для GeckoTerminal

_jlock = asyncio.Lock()
_jlast = 0.0
J_MIN_INTERVAL = 1.2  # пауза между Token API запросами к Jupiter

# Кэш данных токенов: сканер и FOMO запрашивают одни и те же минты по кругу.
# Без кэша свежие токены съедают весь лимит 30/мин и всё падает с 429.
_TD_CACHE = {}  # mint -> (timestamp_monotonic, data)
TD_TTL = 90.0  # секунд свежие данные считаются годными


async def _gt_get(path: str, retries: int = 2):
    """GET к GeckoTerminal с общим rate-limiter'ом.
    Ретраи на 429/5xx/таймауты (сеть Render медленная). Возвращает dict или {}."""
    global _last_call
    from http_client import get_session
    session = await get_session()
    last_err = "?"
    for attempt in range(retries + 1):
        async with _lock:
            wait = MIN_INTERVAL - (time.monotonic() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with session.get(BASE + path, headers=HEADERS, timeout=12) as r:
                    _last_call = time.monotonic()
                    if r.status == 200:
                        return await r.json()
                    if r.status == 404:
                        return {}  # токен/пул ещё не проиндексирован — ретраи бессмысленны
                    last_err = f"HTTP {r.status}"
            except asyncio.TimeoutError:
                _last_call = time.monotonic()
                last_err = "timeout"
            except Exception as e:
                _last_call = time.monotonic()
                last_err = f"{type(e).__name__}"
        if attempt < retries:
            await asyncio.sleep(2 + 3 * attempt)
    print(f"🔎 GT fail {path.split('?')[0][:60]}: {last_err}")
    return {}


def _parse_ms(ts) -> int:
    try:
        if not ts:
            return 0
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _dex_id_from_pool(item: dict) -> str:
    try:
        dex = item.get("relationships", {}).get("dex", {}).get("data", {}).get("id", "")
        dex = str(dex).lower()
        if "pump" in dex:
            return "pump"
        if "raydium" in dex:
            return "raydium"
        return dex or "unknown"
    except Exception:
        return "unknown"


def _base_mint(item: dict) -> str:
    try:
        bid = item.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
        return bid.split("_", 1)[1] if "_" in bid else ""
    except Exception:
        return ""


def _normalize_pool(mint: str, name: str, symbol: str, item: dict, token_attrs: dict) -> dict:
    a = item.get("attributes", {})
    vol = a.get("volume_usd", {}) or {}
    tx = a.get("transactions", {}) or {}
    chg = a.get("price_change_percentage", {}) or {}
    m5 = tx.get("m5", {}) or {}
    h24 = tx.get("h24", {}) or {}
    try:
        liq = float(a.get("reserve_in_usd", 0) or 0)
    except Exception:
        liq = 0.0
    try:
        price = float(a.get("base_token_price_usd", 0) or 0)
    except Exception:
        price = 0.0
    try:
        fdv = float(token_attrs.get("fdv_usd", 0) or 0)
    except Exception:
        fdv = 0.0
    try:
        mcap = float(token_attrs.get("market_cap_usd", 0) or 0)
    except Exception:
        mcap = 0.0
    return {
        "chainId": "solana",
        "dexId": _dex_id_from_pool(item),
        "pairAddress": a.get("address", ""),
        "baseToken": {"address": mint, "name": name, "symbol": symbol},
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
        "volume": {"m5": float(vol.get("m5", 0) or 0), "h24": float(vol.get("h24", 0) or 0)},
        "txns": {"m5": {"buys": int(m5.get("buys", 0) or 0), "sells": int(m5.get("sells", 0) or 0)},
                 "h24": {"buys": int(h24.get("buys", 0) or 0), "sells": int(h24.get("sells", 0) or 0)}},
        "priceChange": {"m5": float(chg.get("m5", 0) or 0), "h1": float(chg.get("h1", 0) or 0),
                        "h6": float(chg.get("h6", 0) or 0), "h24": float(chg.get("h24", 0) or 0)},
        "fdv": fdv,
        "marketCap": mcap,
        "pairCreatedAt": _parse_ms(a.get("pool_created_at")),
        "info": {"socials": [], "websites": []},
        "_source": "geckoterminal",
        "_socials_unknown": True,  # GT не отдаёт соцсети — фильтр соцсетей пропускаем
    }


async def _jup_token_data(mint: str) -> dict:
    """Токен-профиль через Jupiter Token API.
    Старые хосты tokens.jup.ag sunset (Jupiter их гасит) — идём через
    api.jup.ag -> lite-api.jup.ag. Даёт соцсети, холдеры, аудит, возраст.
    Нормализация — в форму DexScreener-пары. {} если токен не проиндексирован."""
    from http_client import fetch_json
    data = None
    for base in ("https://api.jup.ag", "https://lite-api.jup.ag"):
        status, body = await fetch_json(f"{base}/tokens/v1/token/{mint}",
                                        timeout=10, retries=1)
        if status == 200 and body:
            data = [body] if isinstance(body, dict) else body
            break
    if not data:
        return {}
    try:
        items = data if isinstance(data, list) else []
        exact = [x for x in items if isinstance(x, dict) and x.get("id") == mint]
        if not exact:
            return {}
        t = exact[0]
        s1 = t.get("stats1h", {}) or {}
        s24 = t.get("stats24h", {}) or {}
        try:
            price = float(t.get("usdPrice", 0) or 0)
        except Exception:
            price = 0.0
        if price <= 0:
            return {}
        try:
            liq = float(t.get("liquidity", 0) or 0)
        except Exception:
            liq = 0.0
        try:
            fdv = float(t.get("fdv", 0) or 0)
        except Exception:
            fdv = 0.0
        try:
            mcap = float(t.get("mcap", 0) or fdv)
        except Exception:
            mcap = fdv
        graduated = bool(t.get("graduatedAt"))
        socials, websites = [], []
        if t.get("twitter"):
            socials.append({"type": "twitter", "url": t["twitter"]})
        if t.get("telegram"):
            socials.append({"type": "telegram", "url": t["telegram"]})
        if t.get("website"):
            websites.append({"url": t["website"]})
        try:
            bv1 = float(s1.get("buyVolume", 0) or 0) + float(s1.get("sellVolume", 0) or 0)
        except Exception:
            bv1 = 0.0
        try:
            bv24 = float(s24.get("buyVolume", 0) or 0) + float(s24.get("sellVolume", 0) or 0)
        except Exception:
            bv24 = 0.0
        return {
            "chainId": "solana",
            "dexId": "raydium" if graduated else "pump",
            "pairAddress": (t.get("graduatedPool") or t.get("firstPool") or {}).get("id", "")
                          if isinstance(t.get("graduatedPool") or t.get("firstPool"), dict)
                          else (t.get("graduatedPool") or t.get("firstPool") or ""),
            "baseToken": {"address": mint, "name": t.get("name", ""),
                          "symbol": t.get("symbol", "")},
            "priceUsd": str(price),
            "liquidity": {"usd": liq},
            "volume": {"m5": 0.0, "h24": bv24},
            "txns": {"m5": {"buys": 0, "sells": 0},
                     "h24": {"buys": int(s24.get("numBuys", 0) or 0),
                             "sells": int(s24.get("numSells", 0) or 0)}},
            "priceChange": {"m5": 0.0,
                            "h1": float(s1.get("priceChange", 0) or 0),
                            "h6": 0.0,
                            "h24": float(s24.get("priceChange", 0) or 0)},
            "fdv": fdv,
            "marketCap": mcap,
            "pairCreatedAt": _parse_ms(t.get("createdAt")),
            "info": {"socials": socials, "websites": websites},
            "holderCount": int(t.get("holderCount", 0) or 0),
            "organicScore": t.get("organicScore"),
            "audit": t.get("audit", {}) or {},
            "_source": "jupiter",
            "_socials_unknown": False,
            "_vol1h": bv1,
        }
    except Exception:
        return {}


async def get_token_data(mint: str) -> dict:
    """Jupiter Token API -> GeckoTerminal -> DexScreener fallback."""
    import time as _t
    now = _t.monotonic()
    hit = _TD_CACHE.get(mint)
    if hit and (now - hit[0]) < TD_TTL:
        return hit[1]
    # 1. DexScreener первым: 300 зап/мин, самый надёжный, даёт m5/h1/h24 + соцсети.
    # (Jupiter Token API гаснет - старые хосты sunset, поэтому он теперь вторым.)
    ds = await _ds_token_data(mint)
    if ds and float(ds.get("priceUsd", 0) or 0) > 0:
        _TD_CACHE[mint] = (now, ds)
        if len(_TD_CACHE) > 2000:
            _TD_CACHE.clear()
        return ds
    # 2. Jupiter: соцсети, холдеры, аудит, возраст, ликвидность
    jup = await _jup_token_data(mint)
    if jup:
        # 2. Добираем m5-окно (txns/volume) из GeckoTerminal — нужно для
        # VIP-фильтра и XGBoost-velocity. Не критично: при провале нули.
        try:
            g = await _gt_get(f"/networks/solana/tokens/{mint}?include=top_pools")
            pools = [x for x in g.get("included", []) if x.get("type") == "pool"]
            if pools:
                best = max(pools, key=lambda x: float(
                    x.get("attributes", {}).get("reserve_in_usd", 0) or 0))
                pa = best.get("attributes", {})
                vol = pa.get("volume_usd", {}) or {}
                tx = pa.get("transactions", {}) or {}
                chg = pa.get("price_change_percentage", {}) or {}
                m5 = tx.get("m5", {}) or {}
                jup["volume"]["m5"] = float(vol.get("m5", 0) or 0)
                jup["txns"]["m5"] = {"buys": int(m5.get("buys", 0) or 0),
                                     "sells": int(m5.get("sells", 0) or 0)}
                jup["priceChange"]["m5"] = float(chg.get("m5", 0) or 0)
        except Exception:
            pass
        _TD_CACHE[mint] = (now, jup)
        if len(_TD_CACHE) > 2000:
            _TD_CACHE.clear()
        return jup
    # 3. GeckoTerminal целиком (старая логика)
    d = await _gt_get(f"/networks/solana/tokens/{mint}?include=top_pools")
    try:
        data = d.get("data", {})
        ta = data.get("attributes", {})
        pools = [x for x in d.get("included", []) if x.get("type") == "pool"]
        if ta and pools:
            name = ta.get("name", "") or ""
            symbol = ta.get("symbol", "") or ""
            best = max(pools, key=lambda x: float(
                x.get("attributes", {}).get("reserve_in_usd", 0) or 0))
            norm = _normalize_pool(mint, name, symbol, best, ta)
            if float(norm["priceUsd"] or 0) > 0:
                _TD_CACHE[mint] = (now, norm)
                if len(_TD_CACHE) > 2000:  # чистка старья
                    _TD_CACHE.clear()
                return norm
    except Exception:
        pass
    # Fallback: DexScreener уже пробовали первым — возвращаем что есть
    return ds or {}


async def _ds_token_data(mint: str) -> dict:
    import config
    from http_client import fetch_json
    url = f"{config.DEXSCREENER_SEARCH}{mint}"
    status, data = await fetch_json(url, timeout=8, retries=2)
    if status == 200 and data:
        try:
            pairs = data.get("pairs", [])
            sol = [p for p in pairs if p.get("chainId") == "solana"]
            if sol:
                best = sorted(sol, key=lambda x: x.get("liquidity", {}).get("usd", 0),
                              reverse=True)[0]
                best["_source"] = "dexscreener"
                best["_socials_unknown"] = False
                return best
        except Exception:
            pass
    return {}


async def _cex_prices(mints: list) -> dict:
    """Цены majors через CEX public API: бесплатно, без ключей, глобально.
    Coinbase Spot + Kraken Ticker. Покрывает только SOL (для мемов есть Jupiter/Llama/GT)."""
    from http_client import fetch_json
    out = {}
    SOL = "So11111111111111111111111111111111111111112"
    if SOL not in mints:
        return out
    status, data = await fetch_json("https://api.coinbase.com/v2/prices/SOL-USD/spot",
                                    timeout=8, retries=1)
    try:
        px = float((data.get("data", {}) or {}).get("amount", 0))
        if px > 0:
            out[SOL] = px
            return out
    except Exception:
        pass
    status, data = await fetch_json("https://api.kraken.com/0/public/Ticker?pair=SOLUSD",
                                    timeout=8, retries=1)
    try:
        last = (((data.get("result", {}) or {}).get("SOLUSD", {}) or {}).get("c", []) or [0])[0]
        if float(last) > 0:
            out[SOL] = float(last)
    except Exception:
        pass
    return out


async def _llama_prices(mints: list) -> dict:
    """Цены через DeFiLlama Coins API: бесплатно, без ключа.
    https://coins.llama.fi/prices/current/solana:addr1,solana:addr2"""
    from http_client import fetch_json
    out = {}
    ms = [m for m in dict.fromkeys(mints) if m]
    for i in range(0, len(ms), 30):
        chunk = ms[i:i + 30]
        ids = ",".join(f"solana:{m}" for m in chunk)
        status, data = await fetch_json(f"https://coins.llama.fi/prices/current/{ids}",
                                        timeout=10, retries=1)
        if status != 200:
            continue
        try:
            coins = data.get("coins", {})
            for m in chunk:
                px = (coins.get(f"solana:{m}", {}) or {}).get("price", 0)
                if px and float(px) > 0:
                    out[m] = float(px)
        except Exception:
            pass
    return out


async def get_price(mint: str) -> float:
    prices = await get_bulk_prices([mint])
    return prices.get(mint, 0.0)


async def get_bulk_prices(mints: list) -> dict:
    """Балк-цены, цепочка fallback'ов (каждый бесплатный, со своей квотой):
    Jupiter Price V3 -> CEX (Coinbase/Kraken, для SOL) -> DeFiLlama (без ключа) -> GeckoTerminal simple."""
    from http_client import fetch_json
    out = {}
    if not mints:
        return out
    ms = [m for m in dict.fromkeys(mints) if m]
    try:
        out = await _jup_lite_prices(ms)
    except Exception as e:
        print(f"Jupiter bulk error: {type(e).__name__} {e}")
    missing = [m for m in ms if m not in out]
    if missing:
        try:
            out.update(await _cex_prices(missing))
        except Exception as e:
            print(f"CEX bulk error: {type(e).__name__} {e}")
    missing = [m for m in ms if m not in out]
    if missing:
        try:
            out.update(await _llama_prices(missing))
        except Exception as e:
            print(f"Llama bulk error: {type(e).__name__} {e}")
    missing = [m for m in ms if m not in out]
    for i in range(0, len(missing), 30):
        chunk = missing[i:i + 30]
        d = await _gt_get("/simple/networks/solana/token_price/" + ",".join(chunk))
        try:
            px = d.get("data", {}).get("attributes", {}).get("token_prices", {})
            for m, p in px.items():
                out[m] = float(p)
        except Exception:
            pass
    return out


async def _jup_lite_prices(mints: list) -> dict:
    """Цены через Jupiter Price API: api.jup.ag/price/v3 -> lite-api fallback.
    Старый price/v2 sunset (Jupiter гасит лимиты), парсинг защищённый под оба формата."""
    import json as _json
    import urllib.request as _url
    out = {}

    def one_call(chunk):
        last = None
        for base in ("https://api.jup.ag/price/v3?ids=",
                     "https://lite-api.jup.ag/price/v2?ids="):
            try:
                req = _url.Request(base + ",".join(chunk),
                                   headers={"User-Agent": "Mozilla/5.0",
                                            "Accept": "application/json"})
                with _url.urlopen(req, timeout=15) as r:
                    last = _json.load(r)
                if last and last.get("data"):
                    return last
            except Exception:
                continue
        return last or {}

    for i in range(0, len(mints), 50):
        chunk = mints[i:i + 50]
        try:
            data = await asyncio.to_thread(one_call, chunk)
            prices_data = data.get("data", {})
        except Exception:
            continue
        for m in chunk:
            try:
                entry = prices_data.get(m, {}) or {}
                px = entry.get("price", 0) or entry.get("usdPrice", 0)
                if px and float(px) > 0:
                    out[m] = float(px)
            except Exception:
                continue
    return out


_trending_cache = []
_trending_cache_time = 0


async def get_growth_universe(min_reserve_usd: float = 500000.0, limit: int = 20) -> list:
    """Вселенная GROWTH: ядро вотчлиста + топ пулов Raydium по резерву.
    Адреса только живые из API (не хардкод) + фильтр стейблов. Кэш 5 мин."""
    global _trending_cache, _trending_cache_time
    out, seen = [], set()

    def add(mint):
        if mint and mint not in seen and len(mint) > 30:
            seen.add(mint)
            out.append(mint)

    try:
        import config as _c
        for m in getattr(_c, "GROWTH_WATCHLIST", []):
            add(m)
    except Exception:
        pass
    stables = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
    try:
        for page in (1, 2):
            d = await _gt_get(f"/networks/solana/dexes/raydium/pools?page={page}")
            for item in (d.get("data") or []):
                try:
                    a = item.get("attributes", {})
                    if float(a.get("reserve_in_usd", 0) or 0) < min_reserve_usd:
                        continue
                    m = _base_mint(item)
                    if m in stables:
                        continue
                    add(m)
                    if len(out) >= limit:
                        return out
                except Exception:
                    continue
    except Exception as e:
        print(f"growth universe err: {type(e).__name__} {e}")
    return out


async def get_trending() -> list:
    """Замена boosts/profiles: тренды GT + свежие пулы pump.fun.
    Формат как у DexScreener boosts: [{'tokenAddress','chainId'}]."""
    global _trending_cache, _trending_cache_time
    
    # Кэшируем результаты на 60 секунд, чтобы можно было сканировать по 150 монет 
    # без бана по Rate Limit (HTTP 429), так как fomo_scanner опрашивает каждые 20 сек.
    if time.time() - _trending_cache_time < 60:
        return _trending_cache

    out, seen = [], set()

    def add(mint):
        if mint and mint not in seen and len(mint) > 30:
            seen.add(mint)
            out.append({"tokenAddress": mint, "chainId": "solana"})

    # Сканируем ТОП-150 трендов
    for page in range(1, 3):
        d = await _gt_get(f"/networks/solana/trending_pools?page={page}")
        for item in (d.get("data") or []):
            add(_base_mint(item))
            
    # Сканируем только ТОП-30 свежих Pump.fun (1 страница)
    for page in range(1, 2):
        d2 = await _gt_get(f"/networks/solana/dexes/pump-fun/pools?page={page}")
        for item in (d2.get("data") or []):
            add(_base_mint(item))
            
    # Сканируем только ТОП-30 Raydium (1 страница)
    for page in range(1, 2):
        d3 = await _gt_get(f"/networks/solana/dexes/raydium/pools?page={page}")
        for item in (d3.get("data") or []):
            add(_base_mint(item))
            
    return out


async def search_symbol(symbol: str) -> list:
    """Поиск пулов по тикеру для clone-check. Возвращает список
    {mint, symbol, reserve_usd, created_ms}."""
    if not symbol or len(symbol) <= 2:
        return []
    d = await _gt_get(f"/search/pools?query={symbol}&network=solana")
    res = []
    for item in (d.get("data") or []):
        try:
            a = item.get("attributes", {})
            name = str(a.get("name", ""))
            base_sym = name.split("/")[0].strip().upper()
            if base_sym != symbol.upper():
                continue
            res.append({
                "mint": _base_mint(item),
                "symbol": base_sym,
                "reserve_usd": float(a.get("reserve_in_usd", 0) or 0),
                "created_ms": _parse_ms(a.get("pool_created_at")),
            })
        except Exception:
            continue
    return res
