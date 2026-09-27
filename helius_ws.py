import asyncio
import websockets
import json
import time
import config

class HeliusWSSniper:
    def __init__(self):
        self.token_queue = asyncio.Queue(maxsize=100)
        self.running = False

    async def connect_and_listen(self):
        self.running = True
        uri = config.PUMPPORTAL_WSS
        
        while self.running:
            try:
                print("🟢 Подключение к PumpPortal WSS...")
                async with websockets.connect(uri) as websocket:
                    # Подписываемся на создание ВСЕХ новых токенов
                    payload = {
                        "method": "subscribeNewToken"
                    }
                    await websocket.send(json.dumps(payload))
                    print("🚀 Успешная подписка на поток создания токенов (NewToken)!")
                    
                    while self.running:
                        message = await websocket.recv()
                        data = json.loads(message)
                        
                        # Пример:
                        # {"signature":"...","mint":"...","traderPublicKey":"...","txType":"create",
                        #  "initialBuy": 1000000.0, "vSolInBondingCurve": 30.0, ... "name": "...", "symbol": "..."}
                        
                        if "mint" in data and data.get("txType") == "create":
                            # Передаем токен в анализатор
                            if not self.token_queue.full():
                                await self.token_queue.put(data)
                                
            except websockets.exceptions.ConnectionClosed:
                print("⚠️ WSS соединение закрыто. Переподключение через 2 секунды...")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"❌ WSS Ошибка: {e}")
                await asyncio.sleep(2)
                
    def stop(self):
        self.running = False

# Глобальный синглтон для доступа к очереди из любого места
wss_sniper = HeliusWSSniper()
