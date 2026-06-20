from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GenerationResult:
    file_path: str
    media_type: str  # "photo", "video", "audio"
    prompt: str
    task_id: Optional[str] = None
    urls: list = field(default_factory=list)
    # all downloaded local paths when several outputs were produced
    # (e.g. generating 2-4 photos at once). Falls back to [file_path].
    file_paths: list = field(default_factory=list)


class BaseGenerator(ABC):
    @abstractmethod
    async def generate_image(self, prompt: str, **kwargs) -> GenerationResult:
        ...

    @abstractmethod
    async def generate_video(self, prompt: str, **kwargs) -> GenerationResult:
        ...

    @abstractmethod
    async def generate_audio(self, prompt: str, **kwargs) -> GenerationResult:
        ...
