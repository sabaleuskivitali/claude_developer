import re

_FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "msn.com",
    "mail.ru", "yandex.ru", "yandex.com",
    "protonmail.com", "proton.me",
}


def derive_server_name(email: str) -> str:
    """
    Custom domain (acme.com) → first label → "acme".
    Free provider (gmail.com) → local part → "vitali".
    """
    if "@" not in email:
        return _sanitize(email)
    local, domain = email.lower().split("@", 1)
    if domain in _FREE_PROVIDERS:
        return _sanitize(local)
    return _sanitize(domain.split(".")[0])


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name).strip("-") or "server"
