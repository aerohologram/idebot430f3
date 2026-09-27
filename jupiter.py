import aiohttp

class JupiterAPI:
    @staticmethod
    async def get_prices(mints: list) -> dict:
        """
        Балк-запрос цен: Jupiter Lite первым (своя квота), GeckoTerminal как fallback.
        Значительно ускоряет цикл трекинга позиций, избавляя от последовательных HTTP-запросов.
        """
        if not mints:
            return {}
        import market_data
        try:
            return await market_data.get_bulk_prices(mints)
        except Exception:
            pass
        addresses = ",".join(mints)
        url = f"https://api.geckoterminal.com/api/v2/simple/networks/solana/token_price/{addresses}"
        headers = {"Accept": "application/json"}
        
        from http_client import get_session
        session = await get_session()
        try:
            async with session.get(url, headers=headers, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    prices = data.get("data", {}).get("attributes", {}).get("token_prices", {})
                    return {mint: float(price) for mint, price in prices.items()}
        except Exception:
            pass
        return {}

    @staticmethod
    async def get_price(mint: str) -> float:
        prices = await JupiterAPI.get_prices([mint])
        return prices.get(mint, 0.0)

    @staticmethod
    async def check_taxes_and_simulate_swap(mint: str, input_amount_sol: float = 0.1) -> dict:
        """
        Симулирует маршрут через Jupiter. 
        Если Price Impact аномален для маленькой суммы, значит пул пустой или это Honeypot с Tax-fee.
        """
        # 1 SOL = 1e9 lamports
        lamports_in = int(input_amount_sol * 1e9)
        # Jupiter sunset quote-api.jup.ag/v6 -> Swap V1 на api.jup.ag
        url = f"https://api.jup.ag/swap/v1/quote?inputMint=So11111111111111111111111111111111111111112&outputMint={mint}&amount={lamports_in}&slippageBps=300"
        
        from http_client import get_session
        session = await get_session()
        try:
            async with session.get(url, timeout=5) as response:
                if response.status != 200:
                    return {"is_safe": False, "reason": "Jupiter routing failed (No liquidity or rug)"}

                data = await response.json()
                price_impact = float(data.get("priceImpactPct", 100))

                # Если impact > 5% на микрообъеме 0.1 SOL — в пуле нет денег, либо стоит заградительный налог
                if price_impact > 5.0:
                    return {"is_safe": False, "reason": f"High price impact: {price_impact}%"}

                return {"is_safe": True, "data": data}
        except Exception as e:
             return {"is_safe": False, "reason": f"Error: {e}"}

    @staticmethod
    async def get_swap_transaction(mint: str, is_sell: bool = False, amount_lamports: int = 0, is_emergency: bool = False) -> dict:
        """
        Генерирует реальную транзакцию на Swap через Jupiter Swap V1 (api.jup.ag).
        quote-api.jup.ag/v6 sunset - мигрировано на Swap V1.
        Блокировка проскальзывания: жесткий лимит slippageBps = 1500 (15%).
        Динамический Priority Fee: если это SELL (Stop-Loss/Crash Guard), ставим Ultra/Very High priority!
        """
        sol_mint = "So11111111111111111111111111111111111111112"
        input_mint = mint if is_sell else sol_mint
        output_mint = sol_mint if is_sell else mint
        
        # 1. Жесткий контроль проскальзывания (Max Slippage) по просьбе пользователя.
        # Устанавливаем лимит в 10% (1000 bps) как на вход, так и на выход. 
        # Бот больше не будет спасать копейки с проскальзыванием 50-100%. Если цена ушла ниже 10% от заявленной - транзакция отменяется.
        slippage = 1000
        quote_url = f"https://api.jup.ag/swap/v1/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount_lamports}&slippageBps={slippage}"
        
        from http_client import get_session
        session = await get_session()
        try:
            async with session.get(quote_url, timeout=5) as response:
                if response.status != 200:
                    return {"success": False, "reason": "No route or slippage too high"}
                quote_response = await response.json()

                # 2. Формируем транзакцию с динамическими fee
                swap_url = "https://api.jup.ag/swap/v1/swap"

            # ИНТЕГРАЦИЯ JITO & PRIORITY FEES
            # Для экстренных продаж (Crash Guard / Stop Loss) агрессивно завышаем комиссию (Jito Tip), 
            # чтобы транзакция гарантированно прошла первой в блоке, обогнав остальных продавцов.
            if is_emergency:
                jito_tip = 5000000  # 0.005 SOL для экстренного спасения капитала
                priority_level = "veryHigh"
            else:
                jito_tip = 150000 if is_sell else 100000
                priority_level = "high"

            payload = {
                "quoteResponse": quote_response,
                "userPublicKey": "YOUR_WALLET_PUBLIC_KEY", # Placeholder для интеграции
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {
                    "jitoTipLamports": jito_tip,
                    "priorityLevelWithMaxLamports": {
                        "maxLamports": 2000000,
                        "priorityLevel": priority_level
                    }
                }
            }

            async with session.post(swap_url, json=payload, timeout=5) as response:
                if response.status != 200:
                    return {"success": False, "reason": "Failed to generate swap tx"}
                swap_data = await response.json()
                encoded_tx = swap_data.get("swapTransaction")

                if not encoded_tx:
                    return {"success": False, "reason": "Empty swapTransaction from Jupiter"}

                # === JITO BUNDLE EXECUTION ===
                # Вместо отправки через обычный RPC (открытый мемпул),
                # направляем транзакцию напрямую в Jito Block Engine.
                # MEV-боты не видят нашу транзакцию — нет сэндвич-атак!
                from jito_executor import JitoExecutor
                jito_ok = await JitoExecutor.send_bundle(encoded_tx, is_emergency=is_emergency)

                if jito_ok:
                    return {"success": True, "tx": encoded_tx, "via": "jito"}
                else:
                    # Fallback: если Jito недоступен, возвращаем транзакцию (вызывающий сам решит что делать)
                    print("⚠️ [JITO] Fallback: бандл не принят, транзакция возвращена для ручной отправки")
                    return {"success": True, "tx": encoded_tx, "via": "fallback_rpc"}

        except Exception as e:
            print(f"Jupiter Swap Error: {type(e).__name__} {e}")
            return {"success": False, "reason": f"Jupiter API error: {e}"}
