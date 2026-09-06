"""
Shared NetBox connection helper.

NetBox supports two Authorization schemes:

    Token <token>              classic 40-character API tokens
    Bearer <key>.<token>       the newer key/token credential pair (NetBox 4.7)

pynetbox only speaks the first. This module detects which credential you have
and wires up the right scheme, so .env can hold either form -- including the
"Bearer ..." string the NetBox UI offers for copy-paste.
"""

import os

import pynetbox
import requests
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _BearerAuth(requests.auth.AuthBase):
    """Sets Authorization: Bearer <credential> on every request.

    requests applies `auth` during prepare_request, after any headers passed
    to the call itself -- so this reliably overrides the "Token ..." header
    pynetbox would otherwise set.
    """

    def __init__(self, credential):
        self.credential = credential

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self.credential}"
        return request


class ConfigError(RuntimeError):
    """Raised when .env is missing or malformed."""


def connect():
    """Return (api, info) where info describes the connection for logging."""
    load_dotenv(os.path.join(REPO_ROOT, ".env"))

    url = os.environ.get("NETBOX_URL", "").strip()
    credential = os.environ.get("NETBOX_TOKEN", "").strip()

    if not url:
        raise ConfigError("NETBOX_URL not set. Check .env in the repo root.")
    if not credential:
        raise ConfigError("NETBOX_TOKEN not set. Check .env in the repo root.")
    if credential == "PASTE_YOUR_TOKEN_HERE":
        raise ConfigError("NETBOX_TOKEN is still the placeholder value.")

    # The UI offers a full header value; tolerate the prefix.
    if credential.lower().startswith("bearer "):
        credential = credential[len("bearer "):].strip()

    api = pynetbox.api(url, token=credential)

    # A dot means this is a <key>.<token> pair, which requires Bearer.
    scheme = "Token"
    if "." in credential:
        scheme = "Bearer"
        session = requests.Session()
        session.auth = _BearerAuth(credential)
        api.http_session = session

    info = {
        "url": url,
        "scheme": scheme,
        "masked": f"{credential[:4]}...{credential[-4:]} ({len(credential)} chars)",
    }
    return api, info
