import aiohttp
import asyncio
import time

# Глобальный кеш цены SOL
_sol_price = 150.0
_last_update = 0.0
_UPDATE_INTERVAL = 300  # Обновляем каждые 5 минут

async def get_sol_price() -> float:
    """Получает актуальную цену SOL (Jupiter Lite -> GeckoTerminal) с кешем на 5 минут"""
    global _sol_price, _last_update

    now = time.time()
    if now - _last_update < _UPDATE_INTERVAL:
        return _sol_price

    price = await asyncio.to_thread(_fetch_sol_price_sync)
    if price > 0:
        _sol_price = price
        _last_update = now
        print(f"💰 Цена SOL обновлена: ${_sol_price:.2f}")
    else:
        print(f"⚠️ Ошибка получения цены SOL. Используем кеш: ${_sol_price:.2f}")

    return _sol_price


def _fetch_sol_price_sync() -> float:
    prices = fetch_bulk_prices_sync(["So11111111111111111111111111111111111111112"])
    try:
        return float(prices.get("So11111111111111111111111111111111111111112", 0))
    except Exception:
        return 0.0

def get_sol_price_sync() -> float:
    """Синхронная версия — возвращает последнюю кешированную цену"""
    return _sol_price

import requests

def fetch_bulk_prices_sync(mints: list) -> dict:
    """Синхронные лайв-цены, цепочка: Jupiter Price V3 -> DeFiLlama (без ключа) -> GeckoTerminal.
    Старый price/v2 sunset - мигрировано на v3."""
    if not mints: return {}
    out = {}
    # 1. Jupiter Price V3 (до 50 за запрос), fallback lite v2
    try:
        import json as _json
        ms = [m for m in dict.fromkeys(mints) if m]
        for i in range(0, len(ms), 50):
            chunk = ms[i:i + 50]
            data = {}
            for base in ("https://api.jup.ag/price/v3?ids=",
                         "https://lite-api.jup.ag/price/v2?ids="):
                try:
                    resp = requests.get(base + ",".join(chunk), headers={"Accept": "application/json",
                                                      "User-Agent": "Mozilla/5.0"}, timeout=10)
                    if resp.status_code == 200 and resp.json().get("data"):
                        data = resp.json().get("data", {})
                        break
                except Exception:
                    continue
            for m in chunk:
                try:
                    entry = data.get(m, {}) or {}
                    px = entry.get("price", 0) or entry.get("usdPrice", 0)
                    if px and float(px) > 0:
                        out[m] = float(px)
                except Exception:
                    continue
    except Exception as e:
        print(f"⚠️ Ошибка Live-цен Jupiter: {e}")
    # 1.5 CEX public (без ключей): Coinbase + Kraken для SOL
    missing_sol = "So11111111111111111111111111111111111111112" not in out
    if missing_sol:
        try:
            resp = requests.get("https://api.coinbase.com/v2/prices/SOL-USD/spot",
                                headers={"Accept": "application/json",
                                         "User-Agent": "Mozilla/5.0"}, timeout=8)
            if resp.status_code == 200:
                px = float((resp.json().get("data", {}) or {}).get("amount", 0))
                if px > 0:
                    out["So11111111111111111111111111111111111111112"] = px
        except Exception as e:
            print(f"⚠️ Ошибка цены SOL Coinbase: {e}")
    if "So11111111111111111111111111111111111111112" not in out:
        try:
            resp = requests.get("https://api.kraken.com/0/public/Ticker?pair=SOLUSD",
                                headers={"Accept": "application/json",
                                         "User-Agent": "Mozilla/5.0"}, timeout=8)
            if resp.status_code == 200:
                last = (((resp.json().get("result", {}) or {}).get("SOLUSD", {}) or {}).get("c", []) or [0])[0]
                if float(last) > 0:
                    out["So11111111111111111111111111111111111111112"] = float(last)
        except Exception as e:
            print(f"⚠️ Ошибка цены SOL Kraken: {e}")
    missing = [m for m in dict.fromkeys(mints) if m and m not in out]
    if missing:
        try:
            ids = ",".join(f"solana:{m}" for m in missing[:30])
            resp = requests.get(f"https://coins.llama.fi/prices/current/{ids}",
                                headers={"Accept": "application/json",
                                         "User-Agent": "Mozilla/5.0"}, timeout=10)
            if resp.status_code == 200:
                for m in missing[:30]:
                    try:
                        px = (resp.json().get("coins", {}).get(f"solana:{m}", {}) or {}).get("price", 0)
                        if px and float(px) > 0:
                            out[m] = float(px)
                    except Exception:
                        continue
        except Exception as e:
            print(f"⚠️ Ошибка Live-цен DeFiLlama: {e}")
    # 2. Недостающее — через GeckoTerminal
    missing = [m for m in dict.fromkeys(mints) if m and m not in out]
    if missing:
        try:
            url = ("https://api.geckoterminal.com/api/v2/simple/networks/solana/token_price/"
                   + ",".join(missing[:30]))
            resp = requests.get(url, headers={"Accept": "application/json",
                                              "User-Agent": "Mozilla/5.0"}, timeout=5)
            if resp.status_code == 200:
                px = resp.json().get("data", {}).get("attributes", {}).get("token_prices", {})
                for m, p in px.items():
                    out[m] = float(p)
        except Exception as e:
            print(f"⚠️ Ошибка получения Live-цен из GeckoTerminal: {e}")
    return out
