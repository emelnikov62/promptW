"""File storage abstraction: S3-compatible object storage with local-disk fallback.

Backend is chosen by STORAGE_BACKEND ("s3" | "local"). If unset it defaults to
"s3" when all S3_* vars are present, otherwise "local" (dev / StubGenerator).

Model: S3 objects are uploaded public-read and addressed by their permanent
path-style URL `{S3_ENDPOINT}/{S3_BUCKET}/{key}`. That full URL is what we hand
to the client, store in the DB, and pass to KIE.AI (so KIE can fetch references)
and to Telegram. This mirrors the previous "store a media URL" scheme — only the
host changes — so legacy `/media/<file>` rows keep working unchanged (served from
disk by the existing /media route). Privacy posture is identical to the old setup:
media was already public via unguessable /media/<uuid> URLs.

boto3 is synchronous; the public helpers here are sync (easy to test), and async
wrappers (a*) run them in the default executor so the aiohttp event loop is never
blocked. Beget (like most non-AWS S3) rejects botocore's default flexible
checksums (aws-chunked) with XAmzContentSHA256Mismatch, so we force
request_checksum_calculation="when_required".
"""
import asyncio
import functools
import logging
import mimetypes
import os

logger = logging.getLogger(__name__)

MEDIA_DIR = os.getenv("MEDIA_DIR", "media")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "").rstrip("/")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
S3_REGION = os.getenv("S3_REGION", "ru1")
S3_PREFIX = os.getenv("S3_PREFIX", "media").strip("/")

_client = None


def backend() -> str:
    b = (os.getenv("STORAGE_BACKEND", "") or "").lower()
    if b in ("s3", "local"):
        return b
    return "s3" if (S3_ENDPOINT and S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY) else "local"


def is_s3() -> bool:
    return backend() == "s3"


def _s3():
    global _client
    if _client is None:
        import boto3
        from botocore.client import Config
        _client = boto3.client(
            "s3", endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            region_name=S3_REGION,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _client


# ── url / key helpers ────────────────────────────────────────────────────────

def is_remote(url: str) -> bool:
    return bool(url) and (url.startswith("http://") or url.startswith("https://"))


def _key(filename: str) -> str:
    filename = os.path.basename(filename)
    return "%s/%s" % (S3_PREFIX, filename) if S3_PREFIX else filename


def public_url(key: str) -> str:
    return "%s/%s/%s" % (S3_ENDPOINT, S3_BUCKET, key)


def key_from_url(url: str) -> str:
    """Object key from a full path-style URL we minted, else '' (foreign/legacy)."""
    prefix = "%s/%s/" % (S3_ENDPOINT, S3_BUCKET)
    return url[len(prefix):] if url.startswith(prefix) else ""


def media_name_from_url(url: str) -> str:
    """Basename if `url` is one of OUR legacy '{WEBAPP_URL}/media/<name>' URLs (served
    from disk, pre-S3 generations), else '' (foreign URL — refuse). Tolerates a
    query/fragment suffix. Keeps the 'only our own media' guard: foreign hosts return ''."""
    marker = "/media/"
    base = (WEBAPP_URL + marker) if WEBAPP_URL else ""
    if base and url.startswith(base):
        tail = url[len(base):]
    elif url.startswith(marker):           # relative legacy reference
        tail = url[len(marker):]
    else:
        return ""
    return os.path.basename(tail.split("?", 1)[0].split("#", 1)[0])


def _local_url(filename: str) -> str:
    return "%s/media/%s" % (WEBAPP_URL, os.path.basename(filename))


def _content_type(filename: str, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(filename)[0] or fallback


# ── sync core ────────────────────────────────────────────────────────────────

def put_bytes(data: bytes, filename: str, content_type: str = None) -> str:
    """Store raw bytes under `filename` (basename); return its public URL."""
    filename = os.path.basename(filename)
    ct = content_type or _content_type(filename)
    if is_s3():
        key = _key(filename)
        _s3().put_object(Bucket=S3_BUCKET, Key=key, Body=data,
                         ContentType=ct, ACL="public-read")
        return public_url(key)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    path = os.path.join(MEDIA_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)
    return _local_url(filename)


def put_file(local_path: str, content_type: str = None,
             remove_local: bool = True) -> str:
    """Upload an existing local file; return its public URL.

    s3: uploads public-read then removes the local temp (remove_local=True).
    local: returns {WEBAPP_URL}/media/<basename> (file already lives in MEDIA_DIR).
    """
    filename = os.path.basename(local_path)
    if not is_s3():
        return _local_url(filename)
    ct = content_type or _content_type(filename)
    key = _key(filename)
    with open(local_path, "rb") as f:
        _s3().put_object(Bucket=S3_BUCKET, Key=key, Body=f,
                         ContentType=ct, ACL="public-read")
    if remove_local:
        try:
            os.remove(local_path)
        except OSError:
            pass
    return public_url(key)


def delete_url(url: str) -> bool:
    """Delete the object behind a URL we minted. Returns True if an S3 delete ran.

    Legacy '/media/<name>' or local-disk values return False (caller does os.remove).
    """
    if not is_s3():
        return False
    key = key_from_url(url)
    if not key:
        return False
    try:
        _s3().delete_object(Bucket=S3_BUCKET, Key=key)
    except Exception:
        logger.exception("s3 delete failed for %s", key)
    return True


def get_bytes(url: str) -> bytes:
    """Fetch object bytes for a URL we minted (used to re-upload into Telegram)."""
    key = key_from_url(url)
    obj = _s3().get_object(Bucket=S3_BUCKET, Key=key)
    return obj["Body"].read()


# ── async wrappers (never block the aiohttp loop) ────────────────────────────

async def _run(fn, *args, **kw):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kw))


async def aput_bytes(data, filename, content_type=None):
    return await _run(put_bytes, data, filename, content_type)


async def aput_file(local_path, content_type=None, remove_local=True):
    return await _run(put_file, local_path, content_type, remove_local)


async def adelete_url(url):
    return await _run(delete_url, url)


async def aget_bytes(url):
    return await _run(get_bytes, url)
