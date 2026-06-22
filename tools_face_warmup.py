"""One-shot warmup/preload for the face-verify model (InsightFace buffalo_l).

Run once on the VPS after `pip install -r requirements.txt` to download the model
pack into FACE_MODEL_ROOT (gitignored) so the first real request doesn't pay the
download. Also doubles as a health check — prints whether the model is usable.

Usage (on the VPS, in /opt/tg-image-ai-bot):
    set -a && . ./.env && set +a            # load FACE_MODEL_ROOT etc.
    venv/bin/python tools_face_warmup.py

Optional self-test against an image (prints the largest-face embedding length):
    venv/bin/python tools_face_warmup.py /path/to/face.jpg

Exit code 0 = model ready, 1 = unavailable (the bot still runs fail-open).
"""
import sys

import face_verify


def main():
    print("FACE_MODEL_ROOT =", face_verify.FACE_MODEL_ROOT)
    print("model name      =", face_verify.FACE_MODEL_NAME)
    print("loading (downloads the pack on first run)…")
    ok = face_verify.available()
    print("available       =", ok)
    if not ok:
        print("Model unavailable — check insightface/onnxruntime install and FACE_MODEL_ROOT perms.")
        return 1

    if len(sys.argv) > 1:
        path = sys.argv[1]
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            print("could not read", path, "—", e)
            return 1
        emb = face_verify.embed(data)
        if emb is None:
            print("self-test: NO face detected in", path)
        else:
            print("self-test: face OK, embedding dim =", len(emb))
    print("ready ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
