import requests
from config import settings
from users.models import Company

TELEGRAM_BOT_TOKEN = settings.BOT_TOKEN
RATE_CON_CHAT_ID = settings.RATE_CON_CHAT_ID


def send_rate_con_message(file_data, load):
    file_obj = file_data['file']
    if hasattr(file_obj, 'seek'):
        file_obj.seek(0)
    file_content = file_obj.read()
    if not file_content:
        print("File is empty, not sending to Telegram")
        return
    caption = (
        f"*File Name: {file_data['name']}*\n"
        f"*Shipment:* {load.shipment}\n"
        f"*Load #:* {load.load_number}\n"
        f"*Carrier:* {Company.current().name}"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    files = {'document': (file_obj.name, file_content, 'application/octet-stream')}
    data = {
        'chat_id': RATE_CON_CHAT_ID,
        'caption': caption,
        'parse_mode': 'Markdown',
    }
    response = requests.post(url, files=files, data=data)
    return response.status_code, response.text


