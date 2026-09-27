import asyncio
import os
import time

async def manual_snipe_loop(analyzer, tracker):
    print("🎯 Manual Snipe Loop запущен: ожидание ручных контрактов...")
    while True:
        try:
            if os.path.exists("manual_snipe.txt"):
                with open("manual_snipe.txt", "r") as f:
                    lines = f.readlines()
                
                if lines:
                    # Очищаем файл сразу, чтобы не зациклиться
                    with open("manual_snipe.txt", "w") as f:
                        pass
                        
                    for mint in lines:
                        mint = mint.strip()
                        if mint:
                            print(f"🎯 РУЧНОЙ СНАЙП: Запрошен анализ {mint} от пользователя!")
                            
                            # Пропускаем некоторые жесткие фильтры сканера, так как токен дал пользователь
                            # Но РугЧек и ML все равно делаем!
                            
                            is_buy = await analyzer.analyze_token(mint)
                            if is_buy:
                                pair_data = await analyzer.fetch_token_data(mint)
                                if pair_data:
                                    actual_price = float(pair_data.get("priceUsd", 0))
                                    actual_symbol = pair_data.get("baseToken", {}).get("symbol", "MANUAL")
                                    
                                    if actual_price > 0:
                                        import config
                                        capital = tracker.get_total_capital()
                                        position_size = max(4.0, min(100.0, capital * (config.REINVEST_PERCENT / 100.0)))
                                        print(f"🚀 СНАЙП {actual_symbol} ({mint})! Входим на {position_size}$ по цене {actual_price}$")
                                        
                                        features = {}
                                        try:
                                            # Попробуем вытащить фичи, если ИИ их сохранил
                                            pass
                                        except:
                                            pass
                                            
                                        # Открываем позицию с флагом is_mature=True, так как это могут быть старые монеты
                                        tracker.open_position(
                                            mint=mint,
                                            symbol=actual_symbol,
                                            entry_price=actual_price,
                                            amount_usd=position_size,
                                            features=features,
                                            confidence=99.0
                                        )
                                        # Проставляем флаг зрелости вручную, если это Raydium монета
                                        pos = tracker.positions.get(mint)
                                        if pos and pair_data.get("dexId") == "raydium":
                                            pos.is_mature = True
                            else:
                                print(f"❌ РУЧНОЙ СНАЙП ОТМЕНЕН: Токен {mint} не прошел ML проверку безопасности!")
        except Exception as e:
            print(f"Ошибка в manual_snipe_loop: {e}")
            
        await asyncio.sleep(2)
