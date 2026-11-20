"""Telegram helpers for lead posts. Pure functions, no Django models.

The bot token is part of every Telegram URL, so it is never logged: only the
method name and Telegram's description are.
"""

import io
import json
import logging
import re
import time
from html import escape

import requests

logger = logging.getLogger("hiring.telegram")

API_BASE = "https://api.telegram.org"
TIMEOUT_SHORT = (5, 15)
TIMEOUT_UPLOAD = (5, 30)
MAX_ATTEMPTS = 3
RETRY_AFTER_CAP = 30
BACKOFF = (2, 5)
MAX_SIDE = 2560
MAX_BYTES = 9 * 1024 * 1024
CAPTION_LIMIT = 1024


class TelegramError(Exception):
    """A Telegram call failed. str(error) is a plain message for the user."""


def _describe(resp) -> str:
    try:
        body = resp.json()
        desc = body.get("description")
        if desc:
            return str(desc)
    except Exception:
        pass
    return "HTTP %s" % resp.status_code


def _retry_after(resp) -> float:
    try:
        value = resp.json().get("parameters", {}).get("retry_after")
        return float(value) if value is not None else float(BACKOFF[0])
    except Exception:
        return float(BACKOFF[0])


def _call(token, method, data=None, files=None, timeout=TIMEOUT_SHORT, attempts=1):
    return _call_n(token, method, data, files, timeout, attempts)[0]


def _call_n(token, method, data=None, files=None, timeout=TIMEOUT_SHORT, attempts=1):
    """POST one Bot API method. Returns (result, attempts_used).

    Retries 429 (honouring retry_after, capped), 5xx and network errors up to
    `attempts` times. Other 4xx fail at once.
    """
    url = "%s/bot%s/%s" % (API_BASE, token, method)
    last = "Telegram request failed"
    used = 0
    for n in range(1, attempts + 1):
        used = n
        wait = None
        try:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as exc:
            last = "Could not reach Telegram (%s)" % type(exc).__name__
            wait = BACKOFF[min(n - 1, len(BACKOFF) - 1)]
        else:
            try:
                body = resp.json()
            except Exception:
                body = None
            if resp.status_code == 200 and isinstance(body, dict) and body.get("ok"):
                return body.get("result"), used
            last = _describe(resp)
            if resp.status_code == 429:
                wait = min(_retry_after(resp), RETRY_AFTER_CAP)
            elif resp.status_code >= 500:
                wait = BACKOFF[min(n - 1, len(BACKOFF) - 1)]
            else:
                logger.error("Telegram %s failed: %s", method, last)
                raise TelegramError(last)
        if n < attempts:
            time.sleep(wait)
    logger.error("Telegram %s failed after %s attempts: %s", method, used, last)
    raise TelegramError(last)


def check_token(token: str) -> str:
    """Return the bot username for a token via getMe."""
    if not token or not str(token).strip():
        raise TelegramError("Bot token is empty")
    result = _call(str(token).strip(), "getMe")
    username = (result or {}).get("username") if isinstance(result, dict) else None
    if not username:
        raise TelegramError("Telegram did not return a bot username")
    return str(username)


def get_chat(token: str, chat_id: str) -> dict:
    """Return {"title", "type", "members"} for a chat."""
    chat = _call(token, "getChat", data={"chat_id": str(chat_id)})
    if not isinstance(chat, dict):
        raise TelegramError("Telegram did not return the chat")
    title = chat.get("title") or " ".join(
        p for p in (chat.get("first_name"), chat.get("last_name")) if p
    ) or chat.get("username") or ""
    members = None
    try:
        count = _call(token, "getChatMemberCount", data={"chat_id": str(chat_id)})
        if isinstance(count, int) and not isinstance(count, bool):
            members = count
    except TelegramError:
        members = None
    return {"title": str(title), "type": str(chat.get("type") or ""), "members": members}


# ---------------------------------------------------------------- photos


def _is_pdf(data, filename, content_type):
    if data[:5] == b"%PDF-":
        return True
    if content_type and "pdf" in content_type.lower():
        return True
    return bool(filename and filename.lower().endswith(".pdf"))


def _render_pdf(data):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        if len(pdf) < 1:
            return None
        page = pdf[0]
        width, height = page.get_size()
        longest = max(width, height) or 1
        scale = min(3.0, MAX_SIDE / longest)
        return page.render(scale=scale).to_pil()
    finally:
        pdf.close()


def _open_image(data):
    from PIL import Image

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def to_photo(data: bytes, filename: str, content_type: str | None) -> bytes | None:
    """JPEG bytes ready for Telegram, or None when data is not a picture."""
    if not data:
        return None
    try:
        from PIL import Image, ImageOps

        if _is_pdf(data, filename, content_type):
            img = _render_pdf(data)
            if img is None:
                return None
        else:
            img = _open_image(data)
            img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            base = Image.new("RGB", rgba.size, (255, 255, 255))
            base.paste(rgba, mask=rgba.getchannel("A"))
            img = base
        else:
            img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        quality = 85
        while True:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_BYTES:
                return buf.getvalue()
            if quality > 50:
                quality -= 15
            else:
                img = img.resize((max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)))
    except Exception as exc:
        logger.warning("to_photo failed: %s", type(exc).__name__)
        return None


# ---------------------------------------------------------------- caption


def _format_phone(phone):
    raw = str(phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return "+1 (%s) %s-%s" % (digits[:3], digits[3:6], digits[6:])
    return raw


def build_caption(*, full_name, phone, dispatcher_name, dispatcher_username, link, notes) -> str:
    """HTML caption (parse_mode HTML). `notes` is a list of strings, a string or None."""
    # Clipped so the caption stays well under Telegram's CAPTION_LIMIT whatever was typed.
    username = str(dispatcher_username or "").strip().lstrip("@")[:32]
    name = str(dispatcher_name or "").strip()[:80]
    full_name = str(full_name or "").strip()[:120]
    if username and name:
        dispatcher = "@%s (%s)" % (username, escape(name))
    elif username:
        dispatcher = "@%s" % username
    elif name:
        dispatcher = escape(name)
    else:
        dispatcher = "not assigned, the team is empty"
    if isinstance(notes, str):
        notes = [notes]
    notes = [str(n).strip() for n in (notes or []) if str(n).strip()]
    lines = [
        "<b>New lead</b>",
        "Name: %s" % escape(str(full_name or "").strip()),
        "Phone: %s" % escape(_format_phone(phone)),
        "Dispatcher: %s" % dispatcher,
        '<a href="%s">Open in Box Truck Manage</a>' % escape(str(link or ""), quote=True),
    ]
    if notes:
        lines.append(escape(" · ".join(notes)))
    return "\n".join(lines)


# ---------------------------------------------------------------- sending


def send_lead(token: str, chat_id: str, photos: list, caption: str) -> dict:
    """Post a lead: text, one photo or an album. Returns {"message_id", "attempts"}."""
    chat_id = str(chat_id)
    photos = list(photos or [])[:3]
    if not photos:
        result, attempts = _call_n(
            token,
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            attempts=MAX_ATTEMPTS,
        )
    elif len(photos) == 1:
        result, attempts = _call_n(
            token,
            "sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("photo.jpg", photos[0], "image/jpeg")},
            timeout=TIMEOUT_UPLOAD,
            attempts=MAX_ATTEMPTS,
        )
    else:
        media = []
        files = {}
        for i, blob in enumerate(photos):
            item = {"type": "photo", "media": "attach://photo%d" % i}
            if i == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
            files["photo%d" % i] = ("photo%d.jpg" % i, blob, "image/jpeg")
        result, attempts = _call_n(
            token,
            "sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files=files,
            timeout=TIMEOUT_UPLOAD,
            attempts=MAX_ATTEMPTS,
        )
    first = result[0] if isinstance(result, list) and result else result
    message_id = first.get("message_id") if isinstance(first, dict) else None
    return {"message_id": str(message_id) if message_id is not None else "", "attempts": attempts}


def send_text(token: str, chat_id: str, html: str, attempts: int = MAX_ATTEMPTS) -> str:
    """Send an HTML text message; returns its message id. The Team window's test passes
    attempts=1 so a person clicking Send test never waits through the retry delays."""
    result = _call(
        token,
        "sendMessage",
        data={
            "chat_id": str(chat_id),
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        attempts=max(1, int(attempts)),
    )
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return str(message_id) if message_id is not None else ""
