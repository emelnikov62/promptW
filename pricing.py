"""Server-side cost calculation in platform tokens (W).

This MUST stay in sync with the frontend pricing in webapp/static/js/app.js
(VIDEO_MODELS / PHOTO_BASE_COST / AUDIO_CREDITS). The server is authoritative:
it recomputes the price from the same (model, settings) it generates with, so a
client cannot forge a cheaper price. Wave 4 will unify both via /api/models.
"""

PHOTO_BASE_COST = 30            # photo: 30 W per image, any quality
CREDIT_TO_TOKEN = 2.2          # KIE credit -> token fallback multiplier
AUDIO_CREDITS = {"Suno V5": 16, "Suno V4.5": 12}
CHAT_COST = 1                  # tokens charged per chat reply

# Trend templates are fixed-price products: the user sees one advertised price and
# must be charged exactly that, regardless of the underlying model's per-second math.
# Keyed by the tplId the client sends in settings. MUST mirror TRENDS[*].cost in app.js.
TEMPLATE_COST = {
    "birthday-photo": 30,
    "yacht-photo": 30,
    "birthday-video": 420,
    "yacht-video": 50,
    "girl-roses-photo": 30,
    "girl-sunset-photo": 30,
    "girl-porsche-photo": 30,
    "girl-vogue-photo": 30,
    "girl-neon-photo": 30,
    "man-jet-photo": 30,
    "man-supercar-photo": 30,
    "man-boss-photo": 30,
    "man-gangster-photo": 30,
    "man-alpine-photo": 30,
}

# Per-model video pricing (KIE credits, 1 credit = $0.005). Mirrors VIDEO_MODELS.
VIDEO_PRICING = {
    "Kling 3.0": {
        "cost": 90, "duration_default": 5,
        "tokenPerSec": {"720p": {"silent": 18, "sound": 24},
                        "1080p": {"silent": 24, "sound": 32}},
    },
    "Veo 3.1 Fast": {
        "cost": 69, "duration_default": 8, "tokenPerCredit": 0.867,
        "creditPerSec": {"720p": {"silent": 10}, "1080p": {"silent": 15}, "4K": {"silent": 26}},
    },
    "Seedance 2.0": {
        "cost": 48, "duration_default": 4, "tokenPerCredit": 0.99,
        "creditPerSecByMode": {"Standard": {"480p": 12, "720p": 28},
                               "Fast": {"480p": 10, "720p": 22}},
    },
    "Kling Motion 3.0": {
        "cost": 139, "fixedSeconds": 10, "tokenPerCredit": 1.156,
        "creditPerSec": {"720p": {"silent": 12}, "1080p": {"silent": 20}},
    },
    "Grok Imagine 1.5": {
        "cost": 132, "duration_default": 6,
        "creditPerSec": {"480p": {"silent": 10}, "720p": {"silent": 14}},
    },
}


def _int(val, default=None):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def photo_cost(settings: dict) -> int:
    count = _int((settings or {}).get("count"), 1) or 1
    count = max(1, min(4, count))
    return PHOTO_BASE_COST * count


def audio_cost(model: str, settings: dict) -> int:
    cr = AUDIO_CREDITS.get(model)
    if cr is None:
        return 0
    return round(cr * CREDIT_TO_TOKEN)


def _video_credits(cfg: dict, quality, duration, sound, mode):
    if "creditPerSecByMode" in cfg:
        modes = cfg["creditPerSecByMode"]
        # frontend pills send "Standard"/"Fast"; trends may send lowercase
        key = mode if mode in modes else (str(mode).capitalize() if mode else None)
        rates = modes.get(key) or modes[next(iter(modes))]
        per = rates.get(quality) if rates else None
        if per is None:
            return None
        return per * (duration or cfg.get("duration_default", 1))
    if "creditPerSec" in cfg:
        cps = cfg["creditPerSec"]
        rate = cps.get(quality) or cps[next(iter(cps))]
        if not rate:
            return None
        per = rate["sound"] if (sound and rate.get("sound") is not None) else rate["silent"]
        sec = duration or cfg.get("fixedSeconds") or cfg.get("duration_default", 1)
        return per * sec
    return None


def video_cost(model: str, settings: dict) -> int:
    cfg = VIDEO_PRICING.get(model)
    if not cfg:
        return 0
    settings = settings or {}
    quality = settings.get("quality")
    duration = _int(settings.get("duration"))
    sound = bool(settings.get("sound"))
    mode = settings.get("mode")

    # explicit W-per-second grid (overrides credit math) — e.g. Kling 3.0
    grid = cfg.get("tokenPerSec")
    if grid:
        rate = grid.get(quality) or grid[next(iter(grid))]
        if rate:
            per = rate["sound"] if (sound and rate.get("sound") is not None) else rate["silent"]
            return round(per * (duration or cfg.get("duration_default", 1)))

    cr = _video_credits(cfg, quality, duration, sound, mode)
    if cr is not None:
        return round(cr * cfg.get("tokenPerCredit", CREDIT_TO_TOKEN))
    return cfg.get("cost", 0)


def compute_cost(gen_type: str, model: str, settings: dict) -> int:
    # Fixed-price templates win over per-model math (server stays authoritative: the
    # client only selects a known tplId, it cannot forge an arbitrary price).
    tpl_id = (settings or {}).get("tplId")
    if tpl_id in TEMPLATE_COST:
        return TEMPLATE_COST[tpl_id]
    if gen_type in ("photo", "image"):
        return photo_cost(settings)
    if gen_type == "audio":
        return audio_cost(model, settings)
    if gen_type == "video":
        return video_cost(model, settings)
    return 0
