import requests

TELEGRAM_TOKEN = "8751754421:AAHN2ta1wD-1LAGKf7Juyao2ggmKVqkwqlI"
url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"

response = requests.get(url)
print("Durum Kodu:", response.status_code)
print("Telegram Yanıtı:", response.text)