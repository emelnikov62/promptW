import os
import uuid

from PIL import Image, ImageDraw

from generators.base import BaseGenerator, GenerationResult

MEDIA_DIR = os.getenv("MEDIA_DIR", "/tmp")


class StubGenerator(BaseGenerator):

    async def generate_image(self, prompt: str, **kwargs) -> GenerationResult:
        try:
            count = max(1, min(4, int(kwargs.get("count", 1))))
        except (TypeError, ValueError):
            count = 1
        paths = []
        for i in range(count):
            img = Image.new("RGB", (512, 512), color=(30, 30, 30))
            draw = ImageDraw.Draw(img)
            draw.text((20, 240), prompt[:80], fill="white")
            if count > 1:
                draw.text((20, 280), f"#{i + 1}", fill="white")
            path = os.path.join(MEDIA_DIR, f"{uuid.uuid4().hex}.png")
            img.save(path)
            paths.append(path)
        return GenerationResult(
            file_path=paths[0], media_type="photo", prompt=prompt,
            file_paths=paths,
        )

    async def generate_video(self, prompt: str, **kwargs) -> GenerationResult:
        raise NotImplementedError("Video generation not yet implemented")

    async def generate_audio(self, prompt: str, **kwargs) -> GenerationResult:
        raise NotImplementedError("Audio generation not yet implemented")
