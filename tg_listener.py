from telethon import TelegramClient, events
import re
import os
import asyncio

TARGET_CHANNEL = "lxetrades"
SOLANA_MINT_REGEX = r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"

async def tg_listener_loop():
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    
    if not api_id or not api_hash:
        print("⚠️ Telegram-парсер отключен (Не указаны TG_API_ID и TG_API_HASH)")
        return
        
    client = TelegramClient('sniper_session', int(api_id), api_hash)
    
    @client.on(events.NewMessage(chats=TARGET_CHANNEL))
    async def handler(event):
        message_text = event.message.message
        if not message_text:
            return
            
        print(f"\n[TG] Сигнал из {TARGET_CHANNEL}:\n{message_text[:100]}...\n")
        
        mints = re.findall(SOLANA_MINT_REGEX, message_text)
        if mints:
            unique_mints = list(set(mints))
            print(f"🎯 ИЗВЛЕЧЕНЫ КОНТРАКТЫ: {unique_mints}")
            with open('fomo_signals.txt', 'a') as f:
                for mint in unique_mints:
                    f.write(f"{mint}\n")
                    
    try:
        print(f"🚀 Подключение Telegram-парсера к каналу: {TARGET_CHANNEL}...")
        await client.start()
        print("✅ Успешно подключено к Telegram (слушаем сигналы)!")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"❌ Ошибка Telegram: {e}. Возможно, нет файла sniper_session.session")

