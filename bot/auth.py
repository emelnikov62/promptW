"""Validation of Telegram WebApp initData.

Telegram signs the WebApp init data with a key derived from the bot token, so
the server can trust the user id inside it instead of whatever the client sends
in the request body. Algorithm: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hmac
import json
import time
import hashlib
import logging
from urllib.parse import parse_qsl

logger = logging.getLogger(__name__)


def validate_init_data(init_data: str, bot_token: str, max_age_sec: int = 30 * 86400):
    """Return the parsed init-data dict (with `user` decoded to a dict) if the
    signature is valid and fresh, otherwise None.

    The HMAC signature is what authenticates the user; the auth_date window is only
    replay protection. Kept generous (30d) so genuine returning users — whose webview
    was launched a while ago and kept open/cached — are NOT rejected and bounced to the
    "session expired" screen. Pass max_age_sec=0 to disable the freshness check."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
    except (ValueError, TypeError):
        return None

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    # Data-check-string: remaining fields sorted by key, joined as key=value\n
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None

    # Reject stale init data (replay protection). A missing/zero/unparseable
    # auth_date is treated as invalid rather than skipped.
    if max_age_sec:
        try:
            auth_date = int(data.get("auth_date", "0"))
        except (ValueError, TypeError):
            return None
        if auth_date <= 0 or (time.time() - auth_date) > max_age_sec:
            return None

    if data.get("user"):
        try:
            data["user"] = json.loads(data["user"])
        except (json.JSONDecodeError, TypeError):
            data["user"] = None
    return data


# ── Fallback auth token (for clients that don't expose initData, e.g. some
# Telegram Desktop builds). HMAC-signed with the bot token, same trust model as
# initData. Format: "<tg_id>.<exp>.<sig>" — tg_id/exp are readable by the client
# (for building API URLs); only the server can forge a valid signature. ──

def _auth_secret(bot_token: str) -> bytes:
    return hmac.new(b"PromptWDesktopAuth", bot_token.encode(), hashlib.sha256).digest()


# This token is embedded in the WebApp URL and persisted to localStorage, so a leak
# grants user-scope API access until it expires. The server now ROLLS it forward on every
# authenticated request (X-Auth-Refresh header), so active users never expire regardless
# of TTL; the TTL therefore only bounds an *inactivity* gap (and the leak window). 14 days
# covers normal inactivity while keeping the leak window modest. Primary auth is initData.
def make_auth_token(tg_id: int, bot_token: str, ttl_sec: int = 14 * 86400) -> str:
    exp = int(time.time()) + ttl_sec
    msg = f"{tg_id}.{exp}"
    sig = hmac.new(_auth_secret(bot_token), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def verify_auth_token(token: str, bot_token: str):
    """Return the tg_id if the token is well-formed, unexpired and correctly
    signed, otherwise None."""
    if not token or not bot_token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    tg_id_s, exp_s, sig = parts
    try:
        tg_id = int(tg_id_s)
        exp = int(exp_s)
    except (ValueError, TypeError):
        return None
    if exp < time.time():
        return None
    calc = hmac.new(_auth_secret(bot_token), f"{tg_id_s}.{exp_s}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, sig):
        return None
    return tg_id


# ── Admin session token ──
# Scoped SEPARATELY from the user/desktop token above (distinct HMAC namespace),
# so a leaked user token (which is long-lived and embedded in the WebApp URL)
# can NEVER be replayed against the admin panel. Same format, different secret;
# the admin routes accept ONLY this token, never a user token.

def _admin_secret(bot_token: str) -> bytes:
    return hmac.new(b"PromptWAdminSession", bot_token.encode(), hashlib.sha256).digest()


def make_admin_token(tg_id: int, bot_token: str, ttl_sec: int = 12 * 3600) -> str:
    exp = int(time.time()) + ttl_sec
    msg = f"{tg_id}.{exp}"
    sig = hmac.new(_admin_secret(bot_token), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def verify_admin_token(token: str, bot_token: str):
    """Return the admin tg_id for a valid, unexpired admin-session token, else None."""
    if not token or not bot_token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    tg_id_s, exp_s, sig = parts
    try:
        tg_id = int(tg_id_s)
        exp = int(exp_s)
    except (ValueError, TypeError):
        return None
    if exp < time.time():
        return None
    calc = hmac.new(_admin_secret(bot_token), f"{tg_id_s}.{exp_s}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, sig):
        return None
    return tg_id
