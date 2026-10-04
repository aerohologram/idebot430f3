"""honeypot.is — второй honeypot-гейт для EVM (идея awesome-web3-rug-check).

Зачем: GoPlus у нас единственный источник honeypot-вердикта — одна точка
отказа. honeypot.is симулирует покупку+продажу и ловит то, что GoPlus
пропускает (нестандартные transfer-ловушки). Бесплатно, без ключа.

Покрытие: ethereum (1), bsc (56), base (8453). Остальных (включая
Robinhood Chain) не знает — скип (fail-open).

Экономность: вызывается ТОЛЬКО для финалистов, которых GoPlus пропустил,
плюс кэш вердиктов 1ч. Единицы запросов в час.
При недоступности — fail-open (вход разрешён, в лог).
"""
import time as _t

CHAIN_IDS = {"ethereum": "1", "bsc": "56", "base": "8453"}

_CACHE: dict = {}  # (chain, addr_lower) -> (ts, (ok, reason))
CALLS = {"hit": 0, "miss": 0, "block": 0}


def _cfg(name, default):
    try:
        import config as _c
        return getattr(_c, name, default)
    except Exception:
        return default


def _tax_frac(raw) -> float:
    try:
        v = float(raw or 0)
    except Exception:
        return 0.0
    if v > 1.0:
        v /= 100.0
    return max(0.0, v)


def _verdict(data: dict):
    """Разбор ответа v2. Возвращает (ok, reason). Fail-open при сомнениях."""
    if not isinstance(data, dict) or not data:
        return True, "no-data"
    hr = data.get("honeypotResult") or {}
    # isHoneypot может быть bool или строкой
    _hp = hr.get("isHoneypot", False)
    if _hp is True or str(_hp).lower() == "true":
        return False, "honeypot"
    sim = data.get("simulationResult") or {}
    worst, worst_k = 0.0, ""
    for k in ("buyTax", "sellTax", "transferTax"):
        t = _tax_frac(sim.get(k, 0))
        if t > worst:
            worst, worst_k = t, k
    if worst > float(_cfg("HONEYPOTIS_MAX_TAX", 0.10)):
        return False, f"{worst_k} {worst * 100:.0f}%"
    return True, ""


async def check_token(address: str, chain: str):
    """EVM-гейт: (True, '') — можно брать; (False, причина) — блок."""
    chain = (chain or "").lower()
    if not _cfg("HONEYPOTIS_ENABLED", True):
        return True, "off"
    if not address:
        return True, "no-addr"
    cid = CHAIN_IDS.get(chain)
    if not cid:
        return True, "unsupported"
    key = (chain, address.lower())
    ttl = float(_cfg("HONEYPOTIS_CACHE_SEC", 3600) or 0)
    hit = _CACHE.get(key)
    if hit and ttl > 0 and (_t.time() - hit[0]) < ttl:
        CALLS["hit"] += 1
        return hit[1]
    CALLS["miss"] += 1
    verdict = (True, "no-data")
    try:
        from http_client import fetch_json
        url = (f"https://api.honeypot.is/v2/IsHoneypot"
               f"?address={address}&chainID={cid}")
        status, data = await fetch_json(url, timeout=10, retries=1)
        if status == 200 and data:
            verdict = _verdict(data)
    except Exception:
        verdict = (True, "error")
    if ttl > 0:
        _CACHE[key] = (_t.time(), verdict)
        if len(_CACHE) > 2000:
            _CACHE.clear()
    if not verdict[0]:
        CALLS["block"] += 1
    return verdict
