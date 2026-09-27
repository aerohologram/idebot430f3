import asyncio
from ddgs import DDGS
import re

# Словари для анализа текста
POSITIVE_WORDS = ["buy", "gem", "moon", "bullish", "lfg", "pump", "based", "x10", "x100", "holding", "safu", "early"]
NEGATIVE_WORDS = ["scam", "rug", "rugpull", "dump", "fake", "avoid", "honeypot", "sell", "shit", "drainer", "dev sold"]

def _fetch_search_results(query: str, max_results: int = 15) -> list:
    try:
        with DDGS() as ddgs:
            # Ищем тексты по запросу
            results = list(ddgs.text(query, max_results=max_results))
            return [res.get("body", "") for res in results if "body" in res]
    except Exception as e:
        print(f"Search error: {e}")
        return []

async def analyze_sentiment(mint: str, symbol: str) -> dict:
    """
    Ищет упоминания контракта и тикера в интернете и оценивает настроение.
    """
    # Запрос: адрес смарт-контракта ИЛИ "$TICKER solana"
    query = f'"{mint}" OR "${symbol} solana"'
    
    # Запускаем синхронный парсинг в отдельном потоке, чтобы не тормозить бота
    texts = await asyncio.to_thread(_fetch_search_results, query, 15)
    
    if not texts:
        return {"score": 0, "positive": 0, "negative": 0, "decision": "neutral", "texts": 0}
        
    pos_count = 0
    neg_count = 0
    
    for text in texts:
        text_lower = text.lower()
        
        for word in POSITIVE_WORDS:
            if re.search(r'\b' + word + r'\b', text_lower):
                pos_count += 1
                
        for word in NEGATIVE_WORDS:
            if re.search(r'\b' + word + r'\b', text_lower):
                neg_count += 1
                
    # Простая формула: считаем перевес позитива над негативом
    score = pos_count - neg_count
    
    if score >= 1 and neg_count == 0:
        decision = "bullish"
    elif neg_count >= 1 and score <= 0:
        decision = "bearish"
    else:
        decision = "neutral"
        
    return {
        "score": score,
        "positive": pos_count,
        "negative": neg_count,
        "decision": decision,
        "texts": len(texts)
    }
