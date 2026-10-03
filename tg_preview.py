"""Бесключевой сборщик TG-коллов через публичное превью t.me/s/CHANNEL.

Без Telethon, без API-ключей: обычный HTTP-опрос открытых каналов.
Находит Solana-минты (base58 32-44) и EVM-адреса (0x+40hex), новые пишет
в fomo_signals.txt как 'sol:'/'evm:' (тот же формат, что tg_listener).
Дальше их забирает fomo_signal_loop -> analyze_token (фильтры!) -> покупка.

Проверено живьём: pumpfunmemecalls отдаёт контракты в превью.
solanamemeradar закрылся (HEO, без превью) - не использовать.
"""
import asyncio
import html
import re
import time

import config
from http_client import fetch_json  # noqa: F401 (сессия через get_session ниже)

SOLANA_MINT_REGEX = r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"
EVM_ADDR_REGEX = r"\b0x[a-fA-F0-9]{40}\b"

# href-паттерны кнопок-ссылок (EVM-каналы прячут адреса в URL, не в тексте):
# poocoin/rh-scan = токен сразу; dexscreener/solscan-token/pump.coin = см. ниже.
_HREF_TOKEN = [
    (re.compile(r"poocoin\.app/tokens/(0x[a-fA-F0-9]{40})", re.I), "evm_token"),
    (re.compile(r"rh-scan\.com/address/(0x[a-fA-F0-9]{40})", re.I), "evm_token"),
    (re.compile(r"solscan\.io/token/([1-9A-HJ-NP-Za-km-z]{32,44})"), "sol"),
    (re.compile(r"pump\.fun/coin/([1-9A-HJ-NP-Za-km-z]{32,44})"), "sol"),
]
# dexscreener/URL = PAIR-адрес (не токен!): dexscreener.com/<chain>/<pair>
_HREF_PAIR = re.compile(r"dexscreener\.com/([a-z0-9-]+)/((?:0x[a-fA-F0-9]{40})|(?:[1-9A-HJ-NP-Za-km-z]{32,44}))", re.I)
# t.me-боты со стартовым параметром: ?start=<junk>_<MINT>
_HREF_BOTMINT = re.compile(r"t(?:elegram)?\.me/[a-zA-Z0-9_]+bot\?start=[^\"'& ]*?_([1-9A-HJ-NP-Za-km-z]{32,44})")

_seen = set()  # (channel, post_id)


def _extract_signals(text: str) -> list:
    # Tier-1 плоские regex (как раньше) + tier-2 деобфускатор
    # (cniper-стайл: zerox[four], скобки, дефисы, арифметика).
    try:
        import addr_decode as _ad
        return _ad.extract_signals(text)
    except Exception:
        out = []
        for m in set(re.findall(SOLANA_MINT_REGEX, text or "")):
            out.append(f"sol:{m}")
        for a in set(re.findall(EVM_ADDR_REGEX, text or "")):
            out.append(f"evm:{a}")
        return out


def _tag(sig: str, channel: str) -> str:
    """Подпись источника: sol:..@канал. fomo_signal_loop её разберёт."""
    return f"{sig}@{channel}" if channel else sig


def extract_from_html(chunk_html: str, channel: str = "") -> list:
    """Сигналы из HTML-куска поста: текст (плоские+скрытые) + href-кнопки.
    Возвращает (tagged_sigs, pair_links[(chain, pair)]). Пары резолвятся позже."""
    html_chunk = chunk_html or ""
    # Мусор, который деобфускатор склеил бы в псевдо-адрес: base64 data-view,
    # URL (их разбираем отдельно по href), CSS-классы вне тегов после кривого сплита.
    clean = re.sub(r'data-view="[^"]*"', " ", html_chunk)
    text = html.unescape(re.sub(r"<[^>]+>", " ", clean))
    text = re.sub(r"https?://\S+", " ", text)
    out = [_tag(s, channel) for s in _extract_signals(text)]
    hrefs = re.findall(r'href="([^"]+)"', html_chunk)
    pairs = []
    for h in hrefs:
        for rx, kind in _HREF_TOKEN:
            m = rx.search(h)
            if m:
                out.append(_tag(("sol:" if kind == "sol" else "evm:") + m.group(1), channel))
        m = _HREF_PAIR.search(h)
        if m:
            pairs.append((m.group(1).lower(), m.group(2)))
        m = _HREF_BOTMINT.search(h)
        if m:
            out.append(_tag("sol:" + m.group(1), channel))
    return sorted(set(out)), pairs


async def _fetch_preview(channel: str):
    """Возвращает [(post_id, text, chunk_html), ...] свежие сверху.
    chunk_html нужен для href-кнопок (адреса в URL, не в тексте).
    Никогда не падает."""
    from http_client import get_session
    url = f"https://t.me/s/{channel}"
    try:
        session = await get_session()
        async with session.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html"}, timeout=15) as r:
            if r.status != 200:
                return None
            page = await r.text()
    except Exception as e:
        print(f"📡 TG preview {channel}: {type(e).__name__}")
        return None
    posts = []
    # посты: data-post="channel/123" + блок текста (два варианта разметки).
    # Сплит ЕСТ через закрывающий > открывающего тега: иначе чанк начинается
    # внутри атрибутов (data-view base64, class-имена) и мусор парсится как текст.
    blocks = re.split(r'data-post="[^"]+/(\d+)"[^>]*>', page)
    # blocks[0] мусор, дальше чередуются id, html-кусок
    for i in range(1, len(blocks) - 1, 2):
        pid, chunk = blocks[i], blocks[i + 1]
        m = re.search(r'js-message_text[^>]*>(.*?)</div\s*>', chunk, re.S)
        if not m:
            m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div\s*>', chunk, re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
        posts.append((pid, text, chunk))
    return posts


async def tg_preview_loop(*_args, **_kwargs):
    channels = list(getattr(config, "TG_PREVIEW_CHANNELS", ["pumpfunmemecalls"]) or [])
    if not getattr(config, "TG_PREVIEW_ENABLED", True):
        return
    if not channels:
        print("⚠️ TG-превью: пуст TG_PREVIEW_CHANNELS")
        return
    interval = int(getattr(config, "TG_PREVIEW_INTERVAL", 90))
    print(f"📡 TG-превью запущен (без ключей): {channels}, опрос каждые {interval}с")
    _stat = {}  # ch -> [опросов, новых постов, сигналов]
    first = True
    while True:
        try:
            _cycle = []
            for ch in channels:
                posts = await _fetch_preview(ch)
                if posts is None:
                    _cycle.append(f"{ch} ERR")
                    continue
                _st = _stat.setdefault(ch, [0, 0, 0])
                _st[0] += 1
                if first:
                    for pid, _t, _c in posts:
                        _seen.add((ch, pid))
                    print(f"📡 TG {ch}: запомнил {len(posts)} старых постов, жду новые коллы")
                    _cycle.append(f"{ch} init({len(posts)})")
                    continue
                if first:
                    for pid, _t, _c in posts:
                        _seen.add((ch, pid))
                    print(f"📡 TG {ch}: запомнил {len(posts)} старых постов, жду новые коллы")
                    continue
                fresh = [p for p in posts if (ch, p[0]) not in _seen]
                for pid, _t, _c in fresh:
                    _seen.add((ch, pid))
                _stat[ch][1] += len(fresh)
                # Текст (плоские+скрытые) + href-кнопки, всё с тегом канала.
                # DS pair-ссылки резолвим в токены (иначе это адрес ПАРЫ).
                sigs, pair_links = [], []
                for _pid, t, chunk in fresh:
                    try:
                        _hs, _pl = extract_from_html(chunk, ch)
                    except Exception:
                        _hs, _pl = [], []
                    if not _hs:
                        # fallback без HTML (юнит-тесты, обрезанные чанки)
                        _hs = [_tag(s, ch) for s in _extract_signals(t)]
                    sigs.extend(_hs)
                    pair_links.extend(_pl)
                if pair_links:
                    try:
                        import evm_data as _ev
                        for _chain, _pair in dict.fromkeys(pair_links):
                            tok = await _ev.resolve_ds_pair(_pair, _chain)
                            if tok:
                                sigs.append(_tag(f"evm:{_chain}:{tok}", ch))
                    except Exception:
                        pass
                sigs = sorted(set(sigs))
                if sigs:
                    print(f"🎯 TG-колл @{ch}: {sigs}")
                    with open("fomo_signals.txt", "a") as f:
                        for s in sigs:
                            f.write(s + "\n")
                _stat[ch][2] += len(sigs)
                _cycle.append(f"{ch} +{len(fresh)}/{len(sigs)}⚡")
                if len(_seen) > 5000:
                    _seen.clear()
            # Пульс цикла: видно, что опрос идёт, даже когда тихо.
            # Формат: канал +новых/сигналов. Пусто везде несколько циклов
            # подряд = каналы молчат (а не парсер мёртв).
            try:
                _tot = " ".join(_cycle) if _cycle else "—"
                print(f"📡 TG-цикл: {_tot}")
                with open("tg_status.json", "w") as _f:
                    import json as _json
                    _json.dump({"ts": time.time(),
                                "channels": {c: {"polls": v[0], "new_posts": v[1], "signals": v[2]}
                                             for c, v in _stat.items()}}, _f)
            except Exception:
                pass
        except Exception as e:
            print(f"Ошибка TG-превью: {type(e).__name__} {e}")
        first = False
        await asyncio.sleep(interval)


if __name__ == "__main__":
    async def _t():
        posts = await _fetch_preview("pumpfunmemecalls")
        print("постов:", len(posts or []))
        for pid, t, _c in (posts or [])[:5]:
            print(pid, _extract_signals(t))
    asyncio.run(_t())
