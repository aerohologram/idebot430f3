"""Авто-журнал сделок (аналог Trading Literacy / RizeTrade) на данных Supabase.
Запуск: python3 trade_journal.py
Показывает: win rate по причинам выхода, среднее удержание, час входа,
средний убыток/выигрыш и диагноз, какой фильтр не работает."""
import json
from collections import Counter, defaultdict
from datetime import timezone, datetime

from supabase import create_client

import config

sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)


def fetch():
    rows, off = [], 0
    while True:
        r = sb.table("trades_pump").select("*").range(off, off + 999).execute()
        if not r.data:
            break
        rows += r.data
        if len(r.data) < 1000:
            break
        off += 1000
    return [d for d in rows if d.get("mint") != "PORTFOLIO_STATE_V3"]


def main():
    rows = fetch()
    closed = [d for d in rows if d.get("status") == "CLOSED" and d.get("pnl") is not None]
    opened = [d for d in rows if d.get("status") == "OPEN"]
    print(f"=== ЖУРНАЛ СДЕЛОК === всего {len(rows)} | закрыто {len(closed)} | открыто {len(opened)}")
    if not closed:
        print("Закрытых сделок нет.")
        return

    pnls = [d["pnl"] for d in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    print(f"Win rate: {len(wins)}/{len(pnls)} = {len(wins)/len(pnls)*100:.0f}%")
    print(f"Суммарный PnL: {sum(pnls):+.2f} | средний: {sum(pnls)/len(pnls):+.2f}")
    if wins:
        print(f"Средний выигрыш: +{sum(wins)/len(wins):.2f}")
    if losses:
        print(f"Средний убыток: {sum(losses)/len(losses):.2f}")

    print("\n--- По причинам выхода ---")
    by_reason = defaultdict(list)
    for d in closed:
        by_reason[d.get("exit_reason") or "?"].append(d["pnl"])
    for reason, ps in sorted(by_reason.items(), key=lambda kv: sum(kv[1])):
        wr = sum(1 for p in ps if p > 0) / len(ps) * 100
        print(f"  {reason:45s} n={len(ps):3d} wr={wr:4.0f}% sum={sum(ps):+8.2f} avg={sum(ps)/len(ps):+6.2f}")

    print("\n--- Удержание (мин) ---")
    durs = []
    for d in closed:
        try:
            durs.append(((d["exit_time"] - d["entry_time"]) / 60, d["pnl"], d.get("exit_reason")))
        except Exception:
            pass
    if durs:
        buckets = defaultdict(list)
        for dur, p, _ in durs:
            b = "<10" if dur < 10 else "10-30" if dur < 30 else "30-60" if dur < 60 else "60+"
            buckets[b].append(p)
        for b in ("<10", "10-30", "30-60", "60+"):
            if b in buckets:
                ps = buckets[b]
                print(f"  {b:6s} мин: n={len(ps):3d} wr={sum(1 for p in ps if p>0)/len(ps)*100:4.0f}% sum={sum(ps):+8.2f}")

    print("\n--- Диагноз ---")
    dead = [p for r, ps in by_reason.items() if "Dead Coin" in r or "Time" in r for p in ps]
    if dead and len(dead) / len(pnls) > 0.5 and sum(1 for p in dead if p > 0) / len(dead) < 0.2:
        print("  >50% сделок закрываются Time Exit в минус и win rate <20%:")
        print("  бот заходит в пулы БЕЗ момента. Проверь, что на Render загружен analyzer.py")
        print("  с MOMENTUM GATE (m5 >= +2%, buys > sells, vol24h >= $20k).")
    if len(opened) > 6:
        print(f"  Открытых позиций {len(opened)} > 6: капитал распылён. MAX_CONCURRENT_POSITIONS=6.")
    stagnant = [d for d in opened]
    now = datetime.now(timezone.utc).timestamp()
    flat = []
    for d in stagnant:
        try:
            held = (now - d["entry_time"]) / 60
            feats = json.loads(d.get("features") or "{}")
            cur = feats.get("current_price_usd", 0)
            ent = feats.get("entry_price_usd", 0)
            if ent and held >= 20 and abs((cur - ent) / ent) < 0.05:
                flat.append(d["mint"][:8])
        except Exception:
            pass
    if flat:
        print(f"  Зависшие около нуля >20 мин: {flat} — stagnant exit не работает на Render (обнови main.py).")
    print("\nГотово.")


if __name__ == "__main__":
    main()
