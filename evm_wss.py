"""EVM factory-listener: новые пулы в реальном времени (до индексации DS/GT).

Подписка eth_subscribe logs на фабрики Uniswap V2/V3 (Base, BSC) через
публичные PublicNode WSS (без ключей). Событие PairCreated/PoolCreated ->
токен (не-quote сторона) -> очередь свежих -> скан-петля разбирает ПЕРВЫМ.
Задержка секунды вместо минут опроса. Robinhood: публичного WSS нет - только опрос.

Топики считаются keccak рантайм (pysha3), fallback - проверенные константы.
"""
import asyncio
import json
import time

# Проверенные keccak-значения (пересчитаны, совпали с каноном Uniswap)
TOPIC_V3_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
TOPIC_V2_PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

V3_FACTORY = "0x1F98431c8aD98523631D114a775f6886A8e1957b6"
V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"

# chain -> (wss_url, quote-токены, для которых второй адрес = новый мем)
CHAINS_WSS = {
    "base": ("wss://base-rpc.publicnode.com", {
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA4C",  # USDC
    }),
    "bsc": ("wss://bsc-rpc.publicnode.com", {
        "0xbb4CdB9CBd36B01bD046d3df728d6333785BCE9",  # WBNB
        "0x55d398326f99059f775485246999027B3197955",  # USDT
    }),
}

# chain -> [(token, pool, ts), ...] свежие пулы (моложе 10 мин)
FRESH_POOLS: dict = {}

# Копи-покупки китов: chain -> {token_lower: ts}. Скан разбирает их первыми,
# в сделке метим source COPY (видно в дашборде, откуда вход).
COPY_BUYS: dict = {}

# ERC20 Transfer(address,address,uint256) — канонический топик, одинаков везде
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3e6"


def _pad_addr(a: str) -> str:
    a = a.lower().replace("0x", "")
    return "0x" + "0" * (64 - len(a)) + a


def is_copybuy(chain: str, addr: str) -> bool:
    try:
        ts = (COPY_BUYS.get(chain) or {}).get((addr or "").lower(), 0)
        return (time.time() - ts) <= 600
    except Exception:
        return False


def _copy_wallets(chain: str) -> list:
    try:
        import config
        return list((getattr(config, "EVM_COPY_WALLETS", {}) or {}).get(chain, []) or [])
    except Exception:
        return []


async def _listen_wallets(chain: str, url: str):
    """Копитрейдинг EVM: входящие Transfer на кошельки китов = их покупки.
    Токен (log.address) уходит в очередь свежих с меткой COPY — скан разберёт первым."""
    import websockets
    wallets = [w.lower() for w in _copy_wallets(chain) if w]
    if not wallets:
        return
    padded = [_pad_addr(w) for w in wallets]
    while True:
        try:
            print(f"🐋 EVM-COPY {chain}: подключаюсь ({len(wallets)} китов)...")
            async with websockets.connect(url, max_size=10 ** 6, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 21, "method": "eth_subscribe",
                    "params": ["logs", {"topics": [TOPIC_TRANSFER, None, padded]}]}))
                await ws.recv()
                print(f"✅ EVM-COPY {chain}: слушаю покупки китов")
                async for message in ws:
                    try:
                        d = json.loads(message)
                        if d.get("method") != "eth_subscription":
                            continue
                        res = d.get("params", {}).get("result", {}) or {}
                        token = str(res.get("address", "") or "")
                        if len(token) != 42 or not token.startswith("0x"):
                            continue
                        FRESH_POOLS.setdefault(chain, []).append((token, "", time.time()))
                        COPY_BUYS.setdefault(chain, {})[token.lower()] = time.time()
                        print(f"🐋 EVM-COPY {chain}: кит купил {token[:10]}")
                    except Exception:
                        continue
        except Exception as e:
            print(f"⚠️ EVM-COPY {chain}: {type(e).__name__} {e}. Реконнект 5с...")
            await asyncio.sleep(5)


def _keccak_topics():
    try:
        import sha3

        def t(s):
            k = sha3.keccak_256()
            k.update(s.encode())
            return "0x" + k.hexdigest()
        return (t("PoolCreated(address,address,uint24,int24,address)"),
                t("PairCreated(address,address,address,uint256)"))
    except Exception:
        return TOPIC_V3_POOL_CREATED, TOPIC_V2_PAIR_CREATED


def _addr(topic: str) -> str:
    topic = (topic or "").lower()
    if topic.startswith("0x") and len(topic) == 66:
        return "0x" + topic[-40:]
    return ""


def _handle_log(chain: str, quotes: set, log: dict):
    """Из лога фабрики достаёт (новый_токен, пул) или None."""
    try:
        topics = [str(t or "").lower() for t in log.get("topics", [])]
        if not topics:
            return None
        data = str(log.get("data", "") or "0x")
        if topics[0] == TOPIC_V3_POOL_CREATED.lower() and len(topics) >= 4:
            t0, t1 = _addr(topics[1]), _addr(topics[2])
            pool = "0x" + data[-40:] if len(data) >= 42 else ""
        elif topics[0] == TOPIC_V2_PAIR_CREATED.lower() and len(topics) >= 3:
            t0, t1 = _addr(topics[1]), _addr(topics[2])
            # PairCreated: pair НЕ indexed - первое слово data, адрес = последние 40 hex
            pool = "0x" + data[26:66] if len(data) >= 66 else ""
        else:
            return None
        q = {x.lower() for x in quotes}
        new = ""
        if t0 and t0 not in q:
            new = t0
        elif t1 and t1 not in q:
            new = t1
        if not new or not pool:
            return None
        return new, pool
    except Exception:
        return None


def drain_fresh(chain: str, max_age_sec: int = 600) -> list:
    """Забрать свежие пулы сети (и почистить старые). Возвращает [token, ...]."""
    now = time.time()
    box = FRESH_POOLS.setdefault(chain, [])
    fresh = [tok for tok, _pool, ts in box if now - ts <= max_age_sec]
    FRESH_POOLS[chain] = [(tok, pool, ts) for tok, pool, ts in box if now - ts <= max_age_sec]
    seen = set()
    out = []
    for tok in fresh:
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


async def _listen_chain(chain: str, url: str, quotes: set):
    import websockets
    v3, v2 = _keccak_topics()
    global TOPIC_V3_POOL_CREATED, TOPIC_V2_PAIR_CREATED
    TOPIC_V3_POOL_CREATED, TOPIC_V2_PAIR_CREATED = v3, v2
    while True:
        try:
            print(f"🔌 EVM-WSS {chain}: подключаюсь...")
            async with websockets.connect(url, max_size=10 ** 6, ping_interval=20) as ws:
                for i, (factory, topic) in enumerate(((V3_FACTORY, v3), (V2_FACTORY, v2))):
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 10 + i, "method": "eth_subscribe",
                        "params": ["logs", {"address": factory, "topics": [topic]}]}))
                    await ws.recv()
                print(f"✅ EVM-WSS {chain}: слушаю фабрики V2+V3")
                async for message in ws:
                    try:
                        d = json.loads(message)
                        if d.get("method") != "eth_subscription":
                            continue
                        hit = _handle_log(chain, quotes, d.get("params", {}).get("result", {}))
                        if hit:
                            token, pool = hit
                            FRESH_POOLS.setdefault(chain, []).append((token, pool, time.time()))
                            print(f"🆕 EVM-WSS {chain}: новый пул {token[:10]} (пул {pool[:10]})")
                    except Exception:
                        continue
        except Exception as e:
            print(f"⚠️ EVM-WSS {chain}: {type(e).__name__} {e}. Реконнект 5с...")
            await asyncio.sleep(5)


async def evm_wss_loop(*_args, **_kwargs):
    import config
    if not getattr(config, "EVM_WSS_ENABLED", True):
        return
    tasks = [_listen_chain(c, u, q) for c, (u, q) in CHAINS_WSS.items()]
    # Копи-покупки китов (только где заданы кошельки в EVM_COPY_WALLETS)
    for c, (u, _q) in CHAINS_WSS.items():
        try:
            import config as _c
            if ((getattr(_c, "EVM_COPY_WALLETS", {}) or {}).get(c)):
                tasks.append(_listen_wallets(c, u))
        except Exception:
            pass
    await asyncio.gather(*tasks)
