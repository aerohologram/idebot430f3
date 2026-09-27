import asyncio
import websockets
import json
import csv
import time
import os
import aiohttp
import config

DATASET_FILE = "pump_dataset_real.csv"

class LiveDataCollector:
    def __init__(self):
        self.running = False
        self.tokens_tracking = {} # mint -> token_data
        
        # Создаем CSV если его нет
        if not os.path.exists(DATASET_FILE):
            with open(DATASET_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "mint", "dev_holding_pct", "top_10_holding_pct", 
                    "tx_velocity_1m", "has_socials", "funded_from_cex", "is_success"
                ])

    async def _fetch_socials_and_dev(self, mint, trader_pubkey):
        """Эмуляция сбора соцсетей и профиля (заглушка для быстроты в примере)"""
        return {
            "has_socials": 1, 
            "funded_from_cex": 0,
            "dev_holding_pct": 2.0,
            "top_10_holding_pct": 15.0
        }

    async def monitor_curve(self, mint, start_data):
        uri = config.PUMPPORTAL_WSS
        start_time = time.time()
        
        buys = 0
        sells = 0
        features_captured = False
        captured_data = {}
        
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
                
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        data = json.loads(msg)
                        
                        if data.get("txType") == "buy": buys += 1
                        if data.get("txType") == "sell": sells += 1
                        
                        sol_in_curve = data.get("vSolInBondingCurve", 30.0)
                        curve_progress = ((sol_in_curve - 30) / 55) * 100
                        
                        # 1. Захват фичей (Features) на отметке 20% кривой
                        if curve_progress >= 20.0 and not features_captured:
                            elapsed_min = (time.time() - start_time) / 60
                            tx_vel = (buys + sells) / elapsed_min if elapsed_min > 0 else (buys + sells)
                            
                            dev_info = await self._fetch_socials_and_dev(mint, start_data.get("traderPublicKey"))
                            
                            captured_data = {
                                "mint": mint,
                                "dev_holding_pct": dev_info["dev_holding_pct"],
                                "top_10_holding_pct": dev_info["top_10_holding_pct"],
                                "tx_velocity_1m": round(tx_vel, 1),
                                "has_socials": dev_info["has_socials"],
                                "funded_from_cex": dev_info["funded_from_cex"],
                            }
                            features_captured = True
                            print(f"📸 Захват фичей для {mint} (Прогресс: {curve_progress:.1f}%)")

                        # 2. Определение таргета (Target): Успех (Raydium) или Провал (Скам)
                        if curve_progress >= 99.0: # Почти 100% -> Миграция на Raydium
                            print(f"🚀 УСПЕХ! Токен {mint} мигрирует на Raydium.")
                            if features_captured:
                                self._save_to_csv(captured_data, 1)
                            break
                            
                        # Если прошло 2 часа, а кривая так и не заполнилась - токен мертв
                        if (time.time() - start_time) > 7200:
                            print(f"💀 ПРОВАЛ! Токен {mint} умер на кривой (прошло 2 часа).")
                            if features_captured:
                                self._save_to_csv(captured_data, 0)
                            break

                    except asyncio.TimeoutError:
                        # Нет сделок 30 секунд. Если прошло 2 часа - помечаем как мертвый
                        if (time.time() - start_time) > 7200:
                            print(f"💀 ПРОВАЛ (Таймаут)! Токен {mint} сдох.")
                            if features_captured:
                                self._save_to_csv(captured_data, 0)
                            break
                            
        except Exception as e:
            pass

    def _save_to_csv(self, data, is_success):
        with open(DATASET_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                data["mint"], data["dev_holding_pct"], data["top_10_holding_pct"],
                data["tx_velocity_1m"], data["has_socials"], data["funded_from_cex"], is_success
            ])
        print(f"💾 Записан результат для {data['mint']} -> is_success: {is_success}")

    async def connect_and_listen(self):
        self.running = True
        uri = config.PUMPPORTAL_WSS
        
        print("🎣 Запущен Сборщик Реальных Данных Pump.fun. Ожидаем токены...")
        
        while self.running:
            try:
                async with websockets.connect(uri) as websocket:
                    await websocket.send(json.dumps({"method": "subscribeNewToken"}))
                    
                    while self.running:
                        msg = await websocket.recv()
                        data = json.loads(msg)
                        
                        if data.get("txType") == "create":
                            mint = data.get("mint")
                            print(f"🟢 Обнаружен новый пул: {mint}. Начинаем слежку...")
                            
                            # Запускаем слежку за кривой в фоне
                            asyncio.create_task(self.monitor_curve(mint, data))
                            
            except Exception as e:
                await asyncio.sleep(2)

if __name__ == "__main__":
    collector = LiveDataCollector()
    asyncio.run(collector.connect_and_listen())
