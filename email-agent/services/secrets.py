"""
App passwords at rest.

A mailbox connected with an app password has to keep it: every send and
every IMAP pass signs in with it. It is stored encrypted with
MAILBOX_SECRET_KEY, a Fernet key kept only in the service's .env, so a copy
of the database alone gives up no mailbox. A missing key refuses to store
anything rather than falling back to plain text.
"""
from cryptography.fernet import Fernet, InvalidToken

from config.settings import config


class SecretError(Exception):
    pass


def _fernet() -> Fernet:
    key = config.MAILBOX_SECRET_KEY
    if not key:
        raise SecretError(
            "MAILBOX_SECRET_KEY is not set, so app passwords cannot be stored. "
            "Generate one with Fernet.generate_key() and put it in .env."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise SecretError(f"MAILBOX_SECRET_KEY is not a valid Fernet key: {e}")


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise SecretError("the stored app password cannot be read with the current MAILBOX_SECRET_KEY")
