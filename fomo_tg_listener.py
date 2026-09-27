import re
from telethon import TelegramClient, events
import os

# --- НАСТРОЙКИ TELEGRAM API ---
# 1. Перейди на сайт https://my.telegram.org/
# 2. Авторизуйся и нажми "API development tools"
# 3. Создай приложение (название любое) и скопируй эти два значения:
API_ID = 1234567       # ЗАМЕНИ НА СВОЙ API ID (целое число)
API_HASH = 'твой_хэш'  # ЗАМЕНИ НА СВОЙ API HASH (строка)

# От кого мы слушаем сигналы? Укажи юзернейм (например '@fomo_bot') или название канала.
FOMO_CHAT_NAME = 'FOMO' 

client = TelegramClient('fomo_session', API_ID, API_HASH)

@client.on(events.NewMessage(chats=FOMO_CHAT_NAME))
async def handler(event):
    text = event.raw_text
    print(f"\n[TG] Получено новое сообщение от FOMO: {text}")
    
    # Регулярное выражение для поиска адреса контракта на Solana (Base58, длина 32-44 символа)
    match = re.search(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b', text)
    
    if match:
        mint = match.group(0)
        print(f"🔥 [TG] Найден смарт-контракт: {mint}")
        
        # Передаем токен в PhantBot через текстовый файл
        with open('fomo_signals.txt', 'a') as f:
            f.write(mint + '\n')
        print(f"✅ Сигнал передан основному боту!")
    else:
        print("🤷‍♂️ В сообщении нет адреса токена.")

if __name__ == '__main__':
    print("🚀 Запускаем слушатель Telegram (Служба FOMO)...")
    print("При первом запуске Telegram попросит ввести номер телефона и код подтверждения.")
    client.start()
    print(f"🎧 Слушаем сообщения из канала/бота: {FOMO_CHAT_NAME}")
    client.run_until_disconnected()
