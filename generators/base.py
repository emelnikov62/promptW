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

    async def recover_task(self, task_id: str, gen_type: str,
                           model: Optional[str] = None) -> Optional[str]:
        """Re-check an already-created provider task (used by the reconciliation
        sweep after a restart). Return the downloaded local file path if the task
        is done, None if it's still processing, or raise if it failed. Default:
        no provider-side state to recover (e.g. the local stub)."""
        return None
