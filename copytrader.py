import asyncio
import websockets
import json
import time
import aiohttp

class CopyTrader:
    def __init__(self, tracker, analyzer=None):
        self.tracker = tracker
        self.analyzer = analyzer
        self.wallets = self.load_wallets()
        self.HELIUS_API_KEY = "9efda6f4-fddb-42d3-a2b1-098bbbecd299"
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.HELIUS_API_KEY}"
        self.wss_url = f"wss://mainnet.helius-rpc.com/?api-key={self.HELIUS_API_KEY}"
        self.processed_sigs = set()
        self.whale_buys = {}      # mint -> {wallet: ts}
        self.consensus_done = set()
        self.whale_first_price = {}  # mint -> цена в момент покупки ПЕРВОГО кита
        
    def load_wallets(self):
        import os
        wallets = {}
        if os.path.exists("smart_wallets.txt"):
            with open("smart_wallets.txt", "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        short_name = f"@Smart_{line[:4]}"
                        wallets[line] = short_name
        
        if not wallets:
            wallets = {
                "5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V": "@DumbCrayon",
                "2yXwy5Dsa1XtEXcsrkFVRJeyuWD3qKkMN3pP3p5VTW3V": "@Salem1299"
            }
        return wallets
        
    async def get_token_price(self, mint):
        try:
            import market_data
            p = await market_data.get_price(mint)
            if p > 0:
                return p
        except:
            pass
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}") as resp:
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        return float(pairs[0].get("priceUsd", 0))
        except:
            pass
        return 0.0001

    async def fetch_transaction(self, signature):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ]
        }
        
        rpc_endpoints = [
            self.rpc_url,
            "https://api.mainnet-beta.solana.com/",
            "https://solana-rpc.publicnode.com/"
        ]
        
        async with aiohttp.ClientSession() as session:
            for rpc in rpc_endpoints:
                try:
                    async with session.post(rpc, json=payload, timeout=5) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        elif resp.status == 429:
                            continue # Пробуем следующий RPC
                except:
                    continue
        return None

    async def process_transaction(self, signature):
        if signature in self.processed_sigs: return
        self.processed_sigs.add(signature)
        if len(self.processed_sigs) > 1000: self.processed_sigs.clear()
        
        # Даем RPC ноде 1.5 секунды на индексацию
        await asyncio.sleep(1.5)
        
        tx_data = await self.fetch_transaction(signature)
        if not tx_data or "result" not in tx_data or not tx_data["result"]:
            await asyncio.sleep(1.5)
            tx_data = await self.fetch_transaction(signature)
            if not tx_data or "result" not in tx_data or not tx_data["result"]: return

        meta = tx_data["result"].get("meta", {})
        if not meta or meta.get("err"): return # Ошибка транзакции (Failed)

        pre_bals = meta.get("preTokenBalances", [])
        post_bals = meta.get("postTokenBalances", [])

        # Проверяем транзакцию для каждого из 5 китов
        for wallet, trader_name in self.wallets.items():
            pre_dict = {b["mint"]: float(b["uiTokenAmount"]["uiAmountString"]) for b in pre_bals if b.get("owner") == wallet}
            post_dict = {b["mint"]: float(b["uiTokenAmount"]["uiAmountString"]) for b in post_bals if b.get("owner") == wallet}

            for mint, post_amt in post_dict.items():
                if not mint.endswith("pump"): continue
                
                pre_amt = pre_dict.get(mint, 0.0)
                if post_amt > pre_amt: # Баланс вырос = ПОКУПКА
                    import config
                    now = time.time()
                    self.whale_buys.setdefault(mint, {})
                    self.whale_buys[mint][wallet] = now
                    self.whale_buys[mint] = {w: t for w, t in self.whale_buys[mint].items() if now - t <= 600}
                    n_whales = len(self.whale_buys[mint])
                    if n_whales == 1:
                        self.whale_first_price[mint] = await self.get_token_price(mint)
                        print(f"👁️ WHALE WATCH: {trader_name} купил {mint[:12]}... слежу за ценой (вход на 2-м ките).")
                    print(f"🚨 COPYTRADE: {trader_name} купил {mint[:12]}... (китов за 10 мин: {n_whales}/{getattr(config, 'WHALE_CONSENSUS', 2)})")
                    if n_whales < getattr(config, 'WHALE_CONSENSUS', 2) or mint in self.consensus_done:
                        return  # ждём раннего консенсуса
                    self.consensus_done.add(mint)
                    # Guard от опоздания: цена не должна улететь от точки входа первого кита
                    cur_price = await self.get_token_price(mint)
                    first_price = self.whale_first_price.get(mint, 0)
                    max_runup = getattr(config, 'WHALE_MAX_RUNUP', 1.35)
                    if first_price > 0 and cur_price > first_price * max_runup:
                        print(f"🚫 [WHALE LATE] {mint[:12]}: цена +{(cur_price/first_price-1)*100:.0f}% с покупки 1-го кита — киты уже надули, входим в их выход.")
                        return
                    print(f"🐋 SMART MONEY CONSENSUS: {n_whales} кита, цена ещё не улетела (+{(cur_price/first_price-1)*100:.0f}%) — РАННИЙ вход!")
                    if len(self.tracker.get_open_positions()) >= config.MAX_CONCURRENT_POSITIONS:
                        print("🚫 Лимит позиций. Пропускаем копитрейд.")
                        return
                        
                    if self.analyzer:
                        print(f"🤖 Передаем сигнал кита ИИ на проверку...")
                        is_safe = await self.analyzer.analyze_token(mint)
                        if not is_safe:
                            print(f"🚫 ИИ забраковал токен кита {trader_name}. Спасли твои деньги от рагпула!")
                            return
                        else:
                            print(f"✅ ИИ одобрил токен кита! Покупаем!")
                    else:
                        print("⚠️ ИИ не подключен к Копитрейдеру. Покупаем вслепую!")
                        
                    price_usd = await self.get_token_price(mint)
                    symbol = f"COPY_{trader_name[1:5].upper()}"
                    
                    capital = self.tracker.get_total_capital()
                    position_size = max(4.0, min(100.0, capital * (config.REINVEST_PERCENT / 100.0)))
                    
                    self.tracker.add_position(symbol, mint, price_usd, amount_usd=position_size,
                                                source=f"COPY:{trader_name}")
                    print(f"✅ Успешно скопировали сделку {trader_name} на {position_size}$!")

    async def listen(self):
        # Список бесплатных публичных WSS
        wss_endpoints = [
            self.wss_url, # Helius
            "wss://api.mainnet-beta.solana.com/",
            "wss://solana-rpc.publicnode.com/"
        ]
        endpoint_idx = 0
        
        while True:
            current_wss = wss_endpoints[endpoint_idx % len(wss_endpoints)]
            try:
                async with websockets.connect(current_wss) as ws:
                    print(f"👥 Копитрейдер: Подключен к {current_wss.split('.')[0]}... Слушаем {len(self.wallets)} китов")
                    
                    req_id = 1
                    for wallet in self.wallets.keys():
                        payload = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [wallet]}, {"commitment": "processed"}]
                        }
                        await ws.send(json.dumps(payload))
                        req_id += 1
                        
                    async for message in ws:
                        data = json.loads(message)
                        if "method" in data and data["method"] == "logsNotification":
                            result = data["params"]["result"]
                            signature = result["value"]["signature"]
                            asyncio.create_task(self.process_transaction(signature))
                                
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg:
                    print(f"⚠️ Лимит запросов 429 на {current_wss}. Переключаемся на следующий сервер...")
                    endpoint_idx += 1
                    await asyncio.sleep(2)
                else:
                    print(f"Ошибка Копитрейдера ({current_wss}): {err_msg}. Переподключение...")
                    await asyncio.sleep(5)