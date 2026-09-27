import aiohttp
import base64
import config

class JitoExecutor:
    """
    Интеграция с Jito Block Engine.
    Отправляет транзакции напрямую лидерам слотов Jito (не в публичный мемпул).
    Это защищает от сэндвич-атак (MEV) и гарантирует приоритетное включение в блок.
    """

    # Официальные Jito Tip Accounts (один из них выбирается случайно для равномерного распределения)
    JITO_TIP_ACCOUNTS = [
        "96gYZGLnJYVFmbjzopPSU6QiCRK4rPdTuQ8hB1aP442b",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt13ij7Ar",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    ]

    @staticmethod
    async def send_bundle(encoded_tx: str, is_emergency: bool = False) -> bool:
        """
        Отправляет транзакцию через Jito Bundle Engine (асинхронно, без блокировки).
        encoded_tx: base64-строка подписанной транзакции от Jupiter.
        is_emergency: если True, увеличивает чаевые для гарантированного включения.
        """
        if not getattr(config, "USE_JITO_EXECUTION", False):
            print("⚠️ [JITO] USE_JITO_EXECUTION = False. Paper Mode, транзакция не отправляется.")
            return False

        import random
        tip_account = random.choice(JitoExecutor.JITO_TIP_ACCOUNTS)
        tip_lamports = 5_000_000 if is_emergency else 200_000  # 0.005 SOL экстренно / 0.0002 SOL обычно

        jito_url = getattr(config, "JITO_ENGINE_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [[encoded_tx]]
        }

        label = "🚨 ЭКСТРЕННЫЙ" if is_emergency else "⚡"
        print(f"{label} [JITO] Отправка бандла → {jito_url} | Tip: {tip_lamports/1e9:.5f} SOL → {tip_account[:8]}...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    jito_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    if "result" in data:
                        print(f"✅ [JITO] Бандл принят! Bundle ID: {data['result']}")
                        return True
                    else:
                        err = data.get("error", data)
                        print(f"❌ [JITO] Ошибка отправки бандла: {err}")
                        return False
        except Exception as e:
            print(f"❌ [JITO] Ошибка соединения с Block Engine: {type(e).__name__}: {e}")
            return False

    @staticmethod
    def build_and_send_bundle(encoded_tx: str) -> bool:
        """Синхронная обёртка для обратной совместимости."""
        import asyncio
        try:
            return asyncio.get_event_loop().run_until_complete(
                JitoExecutor.send_bundle(encoded_tx)
            )
        except Exception as e:
            print(f"❌ [JITO] Sync wrapper error: {e}")
            return False
