import requests
from config import settings
from django.core.mail import send_mail


def send_sms(phone_number: str, message: str):
    response = requests.post(
        "https://api.telnyx.com/v2/messages",
        headers={
            "Authorization": f"Bearer {settings.TELNYX_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.TELNYX_PHONE_NUMBER,
            "to": phone_number,
            "text": message,
        }
    )
    if not response.ok:
        raise Exception(f"Telnyx SMS failed: {response.status_code} {response.text}")
    return response.json()


def send_email(email: str, subject: str, message: str):
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )
