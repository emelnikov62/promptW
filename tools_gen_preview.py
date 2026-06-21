"""One-shot preview generator: calls NanoBanana PRO via the project's KieGenerator.

Usage:
    set -a; . /opt/tg-image-ai-bot/.env; set +a
    venv/bin/python tools_gen_preview.py "<prompt>" "<ref_url>" "<aspect>" "<out_path>"

KIE_API_KEY is read from the environment (never printed). Result image is
downloaded to <out_path>. Prints the remote result URL + local path.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generators.kie import KieGenerator


async def main():
    prompt_file, ref_url, aspect, out_path = sys.argv[1:5]
    with open(prompt_file, encoding="utf-8") as fh:
        prompt = fh.read().strip()
    g = KieGenerator(os.environ["KIE_API_KEY"])
    input_data = {
        "prompt": prompt,
        "output_format": "png",
        "aspect_ratio": aspect,
        "image_input": [ref_url],
    }
    task_id = await g._create_task("nano-banana-pro", input_data)
    task = await g._poll_task(task_id, timeout=300)
    urls = g._parse_result_urls(task)
    if not urls:
        print("NO_URLS", task)
        sys.exit(2)
    filepath = await g._download_file(urls[0], "png")
    os.replace(filepath, out_path)
    print("RESULT_URL", urls[0])
    print("SAVED", out_path)


if __name__ == "__main__":
    asyncio.run(main())
