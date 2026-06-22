"""Face similarity verification for template photo generations (Level C).

Computes an ArcFace embedding (InsightFace `buffalo_l`) for the uploaded reference
face and for a generated result, then their cosine similarity. The generate route
uses this to silently re-generate when the produced face drifts from the reference
(see docs/specs/2026-06-22-face-verify-retry-design.md).

Design rules:
- **Fail-open**: if insightface/onnxruntime/the model are missing or anything throws,
  `available()` returns False and the caller falls back to the normal single-shot
  generation. This module must NEVER break the main generation path.
- **Lazy singleton**: the model is loaded once, on first use, off the event loop.
- The heavy work (decode + detect + embed) is synchronous (CPU); call the `a*`
  async wrappers from aiohttp handlers so the loop is never blocked.
- The model is read from FACE_MODEL_ROOT, which lives OUTSIDE the git checkout
  (gitignored) so `git pull --ff-only` on deploy is never disturbed.
"""
import asyncio
import functools
import logging
import os

# Keep ONNX/BLAS single-threaded — on the small shared VPS the default (all cores)
# spikes CPU and starves the bot/Postgres. Inference stays ~0.3-0.5s, which is fine.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

logger = logging.getLogger(__name__)

FACE_MODEL_ROOT = os.getenv("FACE_MODEL_ROOT", "/opt/tg-image-ai-bot/models")
FACE_MODEL_NAME = os.getenv("FACE_MODEL_NAME", "buffalo_l")
# Detector input size; smaller = faster, larger = better on small faces.
_DET_SIZE = int(os.getenv("FACE_DET_SIZE", "640"))
# Load ONLY what verify needs: detect the face + embed it. The full buffalo_l pack
# also ships landmark_3d/landmark_2d/genderage models that we never use — loading all
# five pushed the bot's RSS to ~970MB and OOM-crashed the 2GB VPS. detection+recognition
# alone roughly halves that. Override via FACE_MODULES (comma list) if ever needed.
_MODULES = [m.strip() for m in os.getenv("FACE_MODULES", "detection,recognition").split(",") if m.strip()]

_app = None            # insightface FaceAnalysis singleton
_state = None          # None = not tried yet, True = ready, False = unavailable


def _load():
    """Try to build the FaceAnalysis app once. Sets _state to True/False. Never raises."""
    global _app, _state
    if _state is not None:
        return _state
    try:
        import numpy  # noqa: F401  (ensure the stack is importable before we commit)
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name=FACE_MODEL_NAME, root=FACE_MODEL_ROOT,
                           providers=["CPUExecutionProvider"],
                           allowed_modules=_MODULES)
        app.prepare(ctx_id=-1, det_size=(_DET_SIZE, _DET_SIZE))
        _app = app
        _state = True
        logger.info("face_verify ready (model=%s modules=%s root=%s)",
                    FACE_MODEL_NAME, _MODULES, FACE_MODEL_ROOT)
    except Exception:
        _state = False
        logger.exception("face_verify unavailable — feature disabled (fail-open)")
    return _state


def available() -> bool:
    """True if the face model loaded successfully (attempts a lazy load on first call)."""
    return _load()


def _largest_face(faces):
    """Pick the face with the biggest bbox area (the subject, not a bystander)."""
    best, best_area = None, -1.0
    for f in faces:
        x1, y1, x2, y2 = f.bbox
        area = (x2 - x1) * (y2 - y1)
        if area > best_area:
            best, best_area = f, area
    return best


def embed(image_bytes: bytes):
    """Return the normalized ArcFace embedding (numpy array) of the largest face in
    the image, or None if no face is detected / the image can't be decoded / the
    feature is unavailable. Never raises."""
    if not image_bytes or not _load():
        return None
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)   # BGR, as insightface expects
        if img is None:
            return None
        faces = _app.get(img)
        if not faces:
            return None
        face = _largest_face(faces)
        return getattr(face, "normed_embedding", None)
    except Exception:
        logger.exception("face_verify.embed failed")
        return None


def similarity(a, b) -> float:
    """Cosine similarity of two (normalized) embeddings. Returns -1.0 if either is
    missing — callers treat -1.0 as 'no comparable face' (a miss)."""
    if a is None or b is None:
        return -1.0
    try:
        import numpy as np
        return float(np.dot(a, b))   # both are L2-normalized -> dot == cosine
    except Exception:
        logger.exception("face_verify.similarity failed")
        return -1.0


# ── async wrappers (never block the aiohttp loop) ────────────────────────────

async def _run(fn, *args, **kw):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kw))


async def aembed(image_bytes: bytes):
    return await _run(embed, image_bytes)


async def aavailable() -> bool:
    return await _run(available)
