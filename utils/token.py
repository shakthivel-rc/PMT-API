import base64
import os
import secrets
from urllib.parse import urlencode

from cryptography.fernet import Fernet

from core.config import get_settings


def generate_random_code(length: int) -> str:
    """Generate a random hexadecimal string of the specified byte length."""
    return secrets.token_hex(length)

# The verify/reset-link cipher, built once at import.
#
# Read through `get_settings()` rather than `os.getenv("SECRET_KEY")`: the bare getenv
# returns None on a deployment that never set it, and `Fernet(None)` fails with a
# TypeError about bytes-like objects several frames from the actual cause. `_required`
# already names the missing variable, and this way there is one place that decides what
# SECRET_KEY is. Still import-time — `tests/conftest.py` depends on that ordering.
cipher_suite = Fernet(get_settings().secret_key)

def encrypt_string(plain_text: str) -> bytes:
    """Encrypts a string using Fernet encryption."""
    return cipher_suite.encrypt(plain_text.encode())

def decrypt_string(encrypted_text: bytes) -> str:
    """Decrypts a string using Fernet encryption."""
    return cipher_suite.decrypt(encrypted_text).decode()

def encode_to_base64(raw: bytes) -> str:
    """Encodes bytes into a Base64 string.

    The parameter was annotated `str`, which every caller ignored: both of them pass
    `encrypt_string(...)`, and `b64encode` raises TypeError on a str. The annotation was
    the wrong half of the pair, so it is the half that changed.
    """
    encoded_bytes = base64.b64encode(raw)
    return encoded_bytes.decode("utf-8")

def decode_base64(encoded_text: str) -> str:
    """Decodes a Base64-encoded string."""
    decoded_bytes = base64.b64decode(encoded_text)  # Decode Base64 to bytes
    return decoded_bytes.decode("utf-8")  # Convert bytes back to string


#: The SPA screens the emailed links open, and so a contract with the SPA that the OpenAPI
#: digest cannot see: these are browser routes in `pmt-app/src/App.tsx`, not API
#: routes. Change one only together with the route there.
#: `tests/integration/test_emailed_links.py` pins this half by following each link back
#: into the API. In the SPA, `src/App.test.tsx` pins the routes and the `?token=` read, and
#: `src/features/auth/components/{ResetPassword,AcceptInvite}.test.tsx` pin that the token
#: goes back unchanged in the POST body.
RESET_LINK_PATH = "/reset-password"
INVITE_LINK_PATH = "/verify-account"


def app_link(path: str, token: str) -> str:
    """The absolute SPA URL an email sends someone to, carrying `token` as `?token=`.

    A query parameter rather than a path segment, because the SPA reads the token with
    `useSearchParams` and posts it back as `?token=` — one shape end to end. The links used
    to be `/reset-password/<token>` and `/verify-account/<token>`, which no SPA route
    matched, so both fell through to the catch-all and neither flow could be completed.
    `urlencode` percent-encodes the Base64 padding, so the value arrives byte-identical.

    `APP_BASE_URL` is read per call, not at import, so a deployment's value is the one in
    effect when the mail is sent. A trailing slash is dropped so `https://app.example.com/`
    does not produce `//reset-password`. Unset, the link is relative, which no mail client
    can open — `.env.example` ships a value for exactly that reason.
    """
    base = (os.getenv("APP_BASE_URL") or "").rstrip("/")
    return f"{base}{path}?{urlencode({'token': token})}"
