import asyncio
import aiohttp
import pandas as pd
import random
import time
import os
import config

async def fetch_historical_tokens(limit_success=1000, limit_fail=1000):
    """
    Эмуляция сбора исторических данных с Helius/Pump.fun
    В реальной среде здесь будут запросы к Helius DAS API / индексаторам
    для получения 1000 успешных (Raydium) и 1000 провальных токенов.
    """
    dataset = []
    
    print("⏳ Начинаем сбор исторических данных Pump.fun (Сбор фичей на отметке 20% кривой)...")
    
    # Генерация синтетического датасета для старта (пока не прикручен дамп из Dune/BigQuery)
    for _ in range(limit_success):
        dataset.append({
            "mint": f"success_mint_{random.randint(1000,9999)}",
            "dev_holding_pct": round(random.uniform(0.0, 5.0), 2),
            "top_10_holding_pct": round(random.uniform(5.0, 25.0), 2),
            "tx_velocity_1m": round(random.uniform(20.0, 150.0), 1),
            "has_socials": 1,
            "funded_from_cex": random.choice([0, 1]),
            "is_success": 1
        })
        
    for _ in range(limit_fail):
        dataset.append({
            "mint": f"fail_mint_{random.randint(1000,9999)}",
            "dev_holding_pct": round(random.uniform(5.0, 30.0), 2),
            "top_10_holding_pct": round(random.uniform(25.0, 80.0), 2),
            "tx_velocity_1m": round(random.uniform(1.0, 25.0), 1),
            "has_socials": random.choice([0, 1]),
            "funded_from_cex": random.choice([0, 1]),
            "is_success": 0
        })
        
    df = pd.DataFrame(dataset)
    df = df.sample(frac=1).reset_index(drop=True)  # Перемешиваем
    
    filename = "pump_dataset.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Датасет успешно собран и сохранен в {filename} (Строк: {len(df)})")
    return df

if __name__ == "__main__":
    asyncio.run(fetch_historical_tokens(1000, 1000))
