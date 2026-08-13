import requests
from config import settings

TELEGRAM_BOT_TOKEN = settings.BOT_TOKEN
TELEGRAM_CHAT_ID = settings.DEVELOPER_CHAT_ID
RATE_CON_CHAT_ID = settings.RATE_CON_CHAT_ID
RAHMATOLLOH_TG_ID = settings.RAHMATOLLOH_TG_ID
RTS_UPLOAD_ID = settings.RTS_UPLOAD_GROUP

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload, timeout=5)
        if response.status_code != 200:
            print(f"Failed to send Telegram message: {response.text}")
    except requests.RequestException as e:
        print(f"Failed to send Telegram message: {str(e)}")


def send_statement_message(message, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": RAHMATOLLOH_TG_ID,
        "text": message,
        "parse_mode": parse_mode
    }
    try:
        response = requests.post(url, data=payload)
        if response.status_code != 200:
            print(f"Failed to send Telegram message: {response.text}")
    except requests.RequestException as e:
        print(f"Failed to send Telegram message: {str(e)}")



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
        f"*Carrier:* {load.company.name}"
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


def send_rts_upload_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": RTS_UPLOAD_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload, timeout=5)
        if response.status_code != 200:
            print(f"Failed to send Telegram message: {response.text}")
    except requests.RequestException as e:
        print(f"Failed to send Telegram message: {str(e)}")

def send_statement_closed_message(statement):
    driver = statement.driver
    if not driver or not driver.telegram_group_id:
        return

    bot_username = "@cosmos_app_bot".replace("_", "\\_")
    message = (
        f"📄 *Statement Calculated*\n\n"
        f"• *Period:* {statement.start_date} - {statement.end_date}\n"
        f"• *Driver:* {driver.name}\n\n"
        f"📲 You can view full details in the bot:\n"
        f"👉 {bot_username}"
    )

    telegram_url = f"https://api.telegram.org/bot{settings.TELEGRAM_WEB_APP_TOKEN}/sendMessage"

    requests.post(
        telegram_url,
        data={
            "chat_id": driver.telegram_group_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=5
    )
