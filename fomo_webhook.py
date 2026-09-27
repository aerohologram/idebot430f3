from flask import Flask, request
import re

app = Flask(__name__)

@app.route('/fomo', methods=['GET', 'POST'])
def fomo_webhook():
    # Читаем текст из входящего запроса
    text = ""
    if request.method == 'POST':
        if request.is_json:
            text = str(request.json)
        else:
            text = request.get_data(as_text=True)
    else:
        text = request.args.get('text', '')

    print(f"\n[WEBHOOK] Получено уведомление с телефона: {text}")

    # Ищем адрес контракта Solana
    match = re.search(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b', text)
    if match:
        mint = match.group(0)
        print(f"🔥 Найден смарт-контракт: {mint}")
        
        with open('fomo_signals.txt', 'a') as f:
            f.write(mint + '\n')
        print(f"✅ Сигнал успешно передан основному боту!")
        return "OK", 200
    else:
        print("🤷‍♂️ Адрес токена не найден.")
        return "No mint", 400

if __name__ == '__main__':
    print("🚀 Локальный сервер запущен! Ждем сигналы от телефона...")
    # Запускаем на всех интерфейсах (0.0.0.0), чтобы телефон мог достучаться по Wi-Fi
    app.run(host='0.0.0.0', port=5000)
