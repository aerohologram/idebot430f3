"""Деобфускатор адресов из TG-коллов (идея cniper-сканера).

Коллеры прячутся от парсеров: `zerox31fb6[four]3235-835(five,zero)558(123+234)f6d`,
скобки/дефисы/пробелы внутри Solana-минтов, слова вместо цифр. Обычный regex
такое не берёт — коллы невидимы.

Что умеет (порядок важен):
1. `zerox` → `0x` (EVM-префикс словом).
2. Слова-цифры zero..nine (любой регистр, в скобках/без) → цифры.
   Одиночные 'o'/'oh' НЕ трогаем: 'o' — валидный base58.
3. Простая арифметика в скобках `(123+234)`, `(40*2)` → результат
   (только целые, результат <100000, иначе оставить как есть).
4. Джойнеры `space -_.~|/\\:*'"()[]{}<>,;` выкидываются.
5. Валидация: Solana 32-44 base58 (без 0,O,I,l — их в минтах не бывает),
   EVM 0x+40 hex. Границы в нормализованном тексте (хвосты 88-символьных
   подписей транз отрезаются правилом границ).

Tier-1 (точные regex по сырому тексту) остаётся первым; этот модуль —
tier-2 и срабатывает только если tier-1 ничего не нашёл И в тексте есть
маркеры обфускации. Ложняки гасятся дальше гейтами анализатора
(фильтры → микро-сайзы), fail-open: сомнения = пропуск.
"""
import re

NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

_SOL_ALPH = r"1-9A-HJ-NP-Za-km-z"
_SOL_RE = re.compile(rf"(?<![0-9A-Za-z])[{_SOL_ALPH}]{{32,44}}(?![0-9A-Za-z])")
_EVM_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
_ZEROX_RE = re.compile(r"(?i)\bzerox(?=[0-9a-fA-F])")
_WORD_RE = re.compile(
    r"(?i)\b(zero|one|two|three|four|five|six|seven|eight|nine)\b")
_ARITH_RE = re.compile(r"\(\s*(\d{1,6})\s*([+*\-])\s*(\d{1,6})\s*\)")
_JOINERS_RE = re.compile(r"[ \t\n\r\-_·.~|/\\:*'\"()\[\]{}<>,;`]+")
_NS_JOINERS_RE = re.compile(r"[\-_·.~|/\\:*'\"()\[\]{}<>,;`]+")
_MARKER_RE = re.compile(
    r"(?i)\bzerox(?=[0-9a-fA-F])"
    r"|\b(zero|one|two|three|four|five|six|seven|eight|nine)\b"
    r"|[()\[\]{}]|\d\s*\(\s*\d")
# Space-aware: пробелы — тоже джойнер обфускации ("f6d 8f809"), но их нельзя
# удалять глобально (склеят "CA:"/"ape" с адресом). Паттерн допускает
# одиночные пробелы внутри, границы и длина проверяются после их снятия.
_SOL_SP_RE = re.compile(rf"(?<![0-9A-Za-z])(?:[{_SOL_ALPH}] ?){{32,44}}(?![0-9A-Za-z])")
_EVM_SP_RE = re.compile(r"(?<![0-9A-Za-z])0x(?:[0-9a-fA-F] ?){40}(?![0-9A-Za-z])")


def _words_to_digits(s: str) -> str:
    s = _ZEROX_RE.sub("0x", s)  # zerox31fb... (без границы после x)
    return _WORD_RE.sub(lambda m: NUM_WORDS[m.group(0).lower()], s)


def _eval_arith(s: str) -> str:
    def _rep(m):
        try:
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            r = a + b if op == "+" else (a * b if op == "*" else a - b)
            if 0 <= r < 100000:
                return str(r)
        except Exception:
            pass
        return m.group(0)
    return _ARITH_RE.sub(_rep, s)


def normalize(text: str) -> str:
    """Сырой текст колла → строка для поиска адресов."""
    s = _eval_arith(_words_to_digits(text or ""))
    return _JOINERS_RE.sub("", s)


def has_markers(text: str) -> bool:
    return bool(_MARKER_RE.search(text or ""))


def extract_obfuscated(text: str) -> list:
    """[(kind, addr)] kind: 'sol'|'evm'. Только скрытые (плоские уже найдены tier-1)."""
    text = text or ""
    if not has_markers(text):
        return []
    # words→digits и арифметика, затем снос НЕпробельных джойнеров;
    # пробелы остаются — их ест space-aware паттерн (лимит 4 внутри).
    s = _NS_JOINERS_RE.sub("", _eval_arith(_words_to_digits(text)))
    out, seen = [], set()

    def _take(kind, raw_match):
        a = raw_match.replace(" ", "")
        if (a in seen or a in text or raw_match.count(" ") > 4
                or not (32 <= len(a) <= 44 if kind == "sol" else True)):
            return
        # Склейки английских слов (классы, URL-хвосты) — почти всегда
        # lowercase-only; живые минты содержат upper/цифры (vanity-риск ничтожен
        # против потока ложняков, а гейты всё равно перепроверят).
        if kind == "sol" and not re.search(r"[A-Z0-9]", a):
            return
        seen.add(a)
        out.append((kind, a))

    for m in _SOL_SP_RE.finditer(s):
        _take("sol", m.group(0))
    for m in _EVM_SP_RE.finditer(s):
        a = m.group(0).replace(" ", "")
        if len(a) != 42 or a in seen or a in text or m.group(0).count(" ") > 4:
            continue
        seen.add(a)
        out.append(("evm", a))
    return out


def extract_signals(text: str) -> list:
    """Полная цепочка для колла: ['sol:...', 'evm:...'] (плоские + скрытые).
    Хвост-подстрока (tier-1 выхватил 40 символов из 44 за скобкой) давится
    правилом длиннейшего: подстрока чужого хита того же вида удаляется."""
    text = text or ""
    sols, evms = set(), set()
    for m in set(re.findall(rf"\b[{_SOL_ALPH}]{{32,44}}\b", text)):
        sols.add(m)
    for a in set(re.findall(r"\b0x[a-fA-F0-9]{40}\b", text)):
        evms.add(a)
    for kind, addr in extract_obfuscated(text):
        (sols if kind == "sol" else evms).add(addr)

    def _longest(pool: set) -> set:
        items = sorted(pool, key=len)
        return {a for a in items
                if not any(a != b and a in b for b in items)}

    out = [f"sol:{m}" for m in _longest(sols)]
    out += [f"evm:{a}" for a in _longest(evms)]
    return sorted(set(out))
