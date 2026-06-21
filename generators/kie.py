import asyncio
import json
import os
import uuid
import logging
from typing import Optional

import aiohttp

from generators.base import BaseGenerator, GenerationResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.kie.ai"
MEDIA_DIR = os.getenv("MEDIA_DIR", "/tmp")

IMAGE_MODELS = {
    "NanoBanana PRO": {"model": "nano-banana-pro", "ref_field": "image_input"},
    "NanoBanana 2": {"model": "nano-banana-2", "ref_field": "image_input"},
    # GPT Image 2: KIE exposes only the image-to-image variant (needs a reference photo)
    "GPT Image 2": {"model": "gpt-image-2-image-to-image", "ref_field": "input_urls"},
    # Seedream 4.5: text-to-image by default, edit variant when a reference is uploaded
    "Seedream 4.5": {"model": "seedream/4.5-text-to-image", "ref_model": "seedream/4.5-edit", "ref_field": "image_urls"},
}

VIDEO_MODELS = {
    "Kling Motion 3.0": "kling-3.0/motion-control",
    "Grok Imagine 1.5": "grok-imagine/image-to-video",
    "Kling 3.0": "kling-3.0/video",
    # Seedance 2.0: standard slug; Fast mode = "bytedance/seedance-2-fast" (route by settings.mode)
    "Seedance 2.0": "bytedance/seedance-2",
    # Veo 3.1 Fast uses the dedicated /api/v1/veo endpoint (see _generate_veo), not jobs/createTask
}

AUDIO_MODELS = {
    "Suno V5": "V5",
    "Suno V4.5": "V4_5",
}

DEFAULT_IMAGE_MODEL = "nano-banana-pro"
DEFAULT_VIDEO_MODEL = "kling-3.0/motion-control"
DEFAULT_AUDIO_MODEL = "V5"


class KieGenerator(BaseGenerator):

    def __init__(self, api_key: str, callback_url: Optional[str] = None):
        self.api_key = api_key
        self.callback_url = callback_url

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _create_task(self, model: str, input_data: dict,
                           callback_url: Optional[str] = None,
                           on_task=None) -> str:
        url = f"{API_BASE}/api/v1/jobs/createTask"
        body = {
            "model": model,
            "input": input_data,
        }
        if callback_url or self.callback_url:
            body["callBackUrl"] = callback_url or self.callback_url

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body,
                                    headers=self._headers()) as resp:
                data = await resp.json()
                logger.info("createTask response: %s", data)
                if data.get("code") != 200:
                    raise RuntimeError(
                        f"KIE createTask failed: {data.get('msg', data)}")
                task_id = (data.get("data") or {}).get("taskId")
                if not task_id:
                    raise RuntimeError(f"KIE createTask: no taskId in response: {data}")
        # Report the task id (persist it) BEFORE the long poll, so a restart can
        # recover this generation. Outside the session ctx so a slow callback
        # doesn't hold the HTTP connection.
        if on_task:
            try:
                await on_task(task_id)
            except Exception:
                logger.exception("on_task callback failed for %s", task_id)
        return task_id

    async def _create_audio_task(self, prompt: str,
                                 model: str = DEFAULT_AUDIO_MODEL,
                                 on_task=None,
                                 **kwargs) -> str:
        url = f"{API_BASE}/api/v1/generate"
        body = {
            "prompt": prompt,
            "model": model,
            "customMode": kwargs.get("custom_mode", False),
            "instrumental": kwargs.get("instrumental", False),
        }
        if kwargs.get("custom_mode"):
            if kwargs.get("style"):
                body["style"] = kwargs["style"]
            if kwargs.get("title"):
                body["title"] = kwargs["title"]
            if kwargs.get("lyrics"):
                body["prompt"] = kwargs["lyrics"]
                body["style"] = prompt
            if kwargs.get("negative_tags"):
                body["negativeTags"] = kwargs["negative_tags"]
        if kwargs.get("vocal_gender"):
            body["vocalGender"] = kwargs["vocal_gender"]
        if kwargs.get("style_weight") is not None:
            body["styleWeight"] = kwargs["style_weight"]
        if kwargs.get("weirdness") is not None:
            body["weirdnessConstraint"] = kwargs["weirdness"]
        if kwargs.get("audio_weight") is not None:
            body["audioWeight"] = kwargs["audio_weight"]
        if kwargs.get("callback_url") or self.callback_url:
            body["callBackUrl"] = kwargs.get("callback_url") or self.callback_url

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body,
                                    headers=self._headers()) as resp:
                data = await resp.json()
                logger.info("generate audio response: %s", data)
                if data.get("code") != 200:
                    raise RuntimeError(
                        f"KIE audio failed: {data.get('msg', data)}")
                task_id = (data.get("data") or {}).get("taskId")
                if not task_id:
                    raise RuntimeError(f"KIE audio: no taskId in response: {data}")
        if on_task:
            try:
                await on_task(task_id)
            except Exception:
                logger.exception("on_task callback failed for %s", task_id)
        return task_id

    async def _poll_task(self, task_id: str, timeout: int = 300,
                         interval: int = 5) -> dict:
        url = f"{API_BASE}/api/v1/jobs/recordInfo"
        deadline = asyncio.get_event_loop().time() + timeout

        async with aiohttp.ClientSession() as session:
            while asyncio.get_event_loop().time() < deadline:
                async with session.get(
                    url, params={"taskId": task_id},
                    headers=self._headers()
                ) as resp:
                    data = await resp.json()

                if data.get("code") != 200:
                    raise RuntimeError(
                        f"KIE recordInfo failed: {data.get('msg', data)}")

                task = data.get("data") or {}
                state = task.get("state", "")
                logger.info("Task %s state: %s", task_id, state)

                if state == "success":
                    return task
                if state in ("failed", "error"):
                    raise RuntimeError(
                        f"Task failed: {task.get('failMsg', 'unknown error')}")

                await asyncio.sleep(interval)

        raise TimeoutError(f"Task {task_id} did not complete in {timeout}s")

    def _parse_result_urls(self, task: dict) -> list:
        result_json = task.get("resultJson", "{}")
        if isinstance(result_json, str):
            try:
                result_json = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                return []
        if not isinstance(result_json, dict):
            return []
        return result_json.get("resultUrls", [])

    async def _download_file(self, file_url: str, ext: str) -> str:
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(MEDIA_DIR, filename)
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                # Don't write a 404/expired-URL error body into a .png/.mp4 — fail
                # so the caller's except/refund path fires instead of charging for junk.
                resp.raise_for_status()
                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
        return filepath

    def _file_to_url(self, filepath: str) -> str:
        if self.callback_url:
            base = self.callback_url.rsplit("/api/callback", 1)[0]
        else:
            base = os.getenv("WEBAPP_URL", "")
        fname = os.path.basename(filepath)
        return f"{base}/media/{fname}"

    @staticmethod
    def _collect_files(files: dict, key: str) -> list:
        val = files.get(key)
        if not val:
            return []
        return val if isinstance(val, list) else [val]

    async def generate_image(self, prompt: str, **kwargs) -> GenerationResult:
        model_name = kwargs.get("model")
        model_cfg = IMAGE_MODELS.get(model_name, {"model": DEFAULT_IMAGE_MODEL, "ref_field": "image_input"})
        files = kwargs.get("files", {})

        ref_paths = self._collect_files(files, "photo-refs")
        ref_urls = [self._file_to_url(p) for p in ref_paths]

        model = model_cfg["model"]
        if ref_urls and "ref_model" in model_cfg:
            model = model_cfg["ref_model"]

        input_data = {"prompt": prompt, "output_format": "png"}
        if kwargs.get("aspect_ratio"):
            input_data["aspect_ratio"] = kwargs["aspect_ratio"]
        if kwargs.get("resolution"):
            input_data["resolution"] = kwargs["resolution"]

        ref_field = model_cfg.get("ref_field", "image_input")
        input_data[ref_field] = ref_urls

        count = kwargs.get("count", 1)
        try:
            count = max(1, min(4, int(count)))
        except (TypeError, ValueError):
            count = 1

        on_task = kwargs.get("on_task")

        async def _one(report):
            # Only the first task reports its id back for recovery (one row, one id).
            task_id = await self._create_task(model, input_data,
                                              on_task=on_task if report else None)
            task = await self._poll_task(task_id)
            urls = self._parse_result_urls(task)
            if not urls:
                raise RuntimeError("No result URLs in completed task")
            filepath = await self._download_file(urls[0], "png")
            return task_id, urls[0], filepath

        # run N independent generations concurrently (one task per image)
        results = await asyncio.gather(*[_one(i == 0) for i in range(count)])

        task_ids = [r[0] for r in results]
        all_urls = [r[1] for r in results]
        file_paths = [r[2] for r in results]

        return GenerationResult(
            file_path=file_paths[0], media_type="photo", prompt=prompt,
            task_id=task_ids[0], urls=all_urls, file_paths=file_paths,
        )

    async def _generate_veo(self, prompt: str, **kwargs) -> GenerationResult:
        files = kwargs.get("files", {})
        img_urls = []
        for key in ("v-start-frame", "v-end-frame"):
            for p in self._collect_files(files, key):
                img_urls.append(self._file_to_url(p))

        body = {
            "prompt": prompt or "",
            "model": "veo3_fast",
            "aspect_ratio": kwargs.get("aspect_ratio") or "16:9",
            "resolution": str(kwargs.get("resolution") or "720p").lower(),  # "4K" -> "4k"
            "duration": int(kwargs.get("duration") or 8),
        }
        if img_urls:
            body["imageUrls"] = img_urls[:3]
            body["generationType"] = (
                "FIRST_AND_LAST_FRAMES_2_VIDEO" if len(img_urls) >= 2 else "REFERENCE_2_VIDEO"
            )
        else:
            body["generationType"] = "TEXT_2_VIDEO"
        if self.callback_url:
            body["callBackUrl"] = self.callback_url

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{API_BASE}/api/v1/veo/generate",
                                    json=body, headers=self._headers()) as resp:
                data = await resp.json()
                logger.info("veo generate response: %s", data)
                if data.get("code") != 200:
                    raise RuntimeError(f"KIE Veo failed: {data.get('msg', data)}")
                task_id = (data.get("data") or {}).get("taskId")
                if not task_id:
                    raise RuntimeError(f"KIE Veo: no taskId in response: {data}")
        on_task = kwargs.get("on_task")
        if on_task:
            try:
                await on_task(task_id)
            except Exception:
                logger.exception("on_task callback failed for %s", task_id)

        poll_url = f"{API_BASE}/api/v1/veo/record-info"
        deadline = asyncio.get_event_loop().time() + 600
        urls = []
        async with aiohttp.ClientSession() as session:
            while asyncio.get_event_loop().time() < deadline:
                async with session.get(poll_url, params={"taskId": task_id},
                                       headers=self._headers()) as resp:
                    data = await resp.json()
                if data.get("code") != 200:
                    raise RuntimeError(f"KIE Veo record-info failed: {data.get('msg', data)}")
                d = data.get("data", {})
                flag = d.get("successFlag")
                logger.info("Veo task %s flag: %s", task_id, flag)
                if flag == 1:
                    response = d.get("response") or {}
                    urls = response.get("resultUrls") or response.get("fullResultUrls") or []
                    break
                if flag in (2, 3):
                    raise RuntimeError(f"Veo task failed: {d.get('errorMessage', 'unknown error')}")
                await asyncio.sleep(10)

        if not urls:
            raise TimeoutError("Veo task did not complete in time")

        filepath = await self._download_file(urls[0], "mp4")
        return GenerationResult(
            file_path=filepath, media_type="video", prompt=prompt,
            task_id=task_id, urls=urls,
        )

    async def generate_video(self, prompt: str, **kwargs) -> GenerationResult:
        model_name = kwargs.get("model")
        # Veo 3.1 runs on KIE's dedicated Veo API, not the generic jobs endpoint
        if model_name == "Veo 3.1 Fast":
            return await self._generate_veo(prompt, **kwargs)

        model = VIDEO_MODELS.get(model_name, DEFAULT_VIDEO_MODEL)
        files = kwargs.get("files", {})

        mode = str(kwargs.get("mode") or "").lower()
        # Seedance 2.0 Fast mode runs on a separate KIE slug
        if model_name == "Seedance 2.0" and mode == "fast":
            model = "bytedance/seedance-2-fast"

        input_data = {"prompt": prompt}
        if kwargs.get("aspect_ratio"):
            input_data["aspect_ratio"] = kwargs["aspect_ratio"]

        # Pull uploaded frames/references from whichever form field the UI used
        # (templates send "v-first-frame"; the Create flow uses model-specific ids).
        def _first(*keys):
            for k in keys:
                paths = self._collect_files(files, k)
                if paths:
                    return paths
            return []
        first_frames = _first("v-first-frame", "v-start-frame", "v-grok15-photo", "v-grok-photo")
        last_frames = _first("v-last-frame", "v-end-frame")
        ref_images = self._collect_files(files, "ref-images")
        first_url = self._file_to_url(first_frames[0]) if first_frames else None
        last_url = self._file_to_url(last_frames[0]) if last_frames else None

        duration = kwargs.get("duration")
        sound = kwargs.get("sound")
        resolution = kwargs.get("resolution")

        # Each KIE model has its OWN input schema — sending the wrong key silently drops
        # the reference image (model falls back to text-to-video and invents a face).
        if model_name == "Seedance 2.0":
            # KIE rejects first/last frames together WITH reference images (422: mutually
            # exclusive — "only one scene can be selected"). Pick ONE: explicit start/end
            # frames, OR reference images (the latter resolves the @ImageN identity
            # mentions in the prompt — what the trend templates rely on). duration int (4-15).
            if first_url or last_url:
                if first_url:
                    input_data["first_frame_url"] = first_url
                if last_url:
                    input_data["last_frame_url"] = last_url
            elif ref_images:
                input_data["reference_image_urls"] = [self._file_to_url(p) for p in ref_images][:9]
            if duration:
                try:
                    input_data["duration"] = int(duration)
                except (TypeError, ValueError):
                    pass
            if resolution:
                input_data["resolution"] = resolution
            if sound is not None:
                input_data["generate_audio"] = bool(sound)

        elif model_name == "Grok Imagine 1.5":
            # Image-to-video input is image_urls (array, up to 7). duration is a string (6-30).
            imgs = [self._file_to_url(p) for p in first_frames] if first_frames else []
            if ref_images:
                imgs += [self._file_to_url(p) for p in ref_images]
            if imgs:
                input_data["image_urls"] = imgs[:7]
            if duration:
                input_data["duration"] = str(duration)
            if resolution:
                input_data["resolution"] = resolution

        elif model_name == "Kling 3.0":
            # First frame is image_urls (array); optional end frame is tail_image_url (str).
            if first_url:
                input_data["image_urls"] = [first_url]
            if last_url:
                input_data["tail_image_url"] = last_url
            if duration:
                input_data["duration"] = str(duration)
            if sound is not None:
                input_data["sound"] = sound

        elif model_name == "Kling Motion 3.0":
            # Character-driven motion control: subject photo + driving video.
            char = _first("v-char-photo")
            motion = _first("v-motion-video")
            if char:
                input_data["subject_reference"] = self._file_to_url(char[0])
            if motion:
                input_data["video"] = self._file_to_url(motion[0])
            orient = str(kwargs.get("orientation") or "")
            if orient:
                input_data["character_orientation"] = "image" if orient == "byPhoto" else "video"
            if duration:
                input_data["duration"] = str(duration)

        else:
            # Unknown model: best-effort image-to-video with the common array key.
            if first_url:
                input_data["image_urls"] = [first_url]
            if duration:
                input_data["duration"] = str(duration)

        # Advanced create-flow reference media (model-agnostic).
        for ref_key, api_key in [("ref-videos", "reference_videos"),
                                  ("ref-audio", "reference_audio")]:
            paths = self._collect_files(files, ref_key)
            if paths:
                input_data[api_key] = [self._file_to_url(p) for p in paths]

        task_id = await self._create_task(model, input_data, on_task=kwargs.get("on_task"))
        task = await self._poll_task(task_id, timeout=600, interval=10)
        urls = self._parse_result_urls(task)

        if not urls:
            raise RuntimeError("No result URLs in completed task")

        filepath = await self._download_file(urls[0], "mp4")
        return GenerationResult(
            file_path=filepath, media_type="video", prompt=prompt,
            task_id=task_id, urls=urls,
        )

    async def generate_audio(self, prompt: str, **kwargs) -> GenerationResult:
        model_name = kwargs.get("model")
        model = AUDIO_MODELS.get(model_name, DEFAULT_AUDIO_MODEL)

        task_id = await self._create_audio_task(prompt, model=model, **kwargs)
        task = await self._poll_task(task_id, timeout=300, interval=8)
        urls = self._parse_result_urls(task)

        if not urls:
            raise RuntimeError("No result URLs in completed task")

        filepath = await self._download_file(urls[0], "mp3")
        return GenerationResult(
            file_path=filepath, media_type="audio", prompt=prompt,
            task_id=task_id, urls=urls,
        )

    async def recover_task(self, task_id: str, gen_type: str,
                           model: Optional[str] = None) -> Optional[str]:
        """Single-shot re-check of an existing task (reconciliation sweep). Returns
        the downloaded local path if done, None if still processing, raises if failed."""
        ext = {"photo": "png", "image": "png", "audio": "mp3"}.get(gen_type, "mp4")

        if model == "Veo 3.1 Fast":
            url = f"{API_BASE}/api/v1/veo/record-info"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params={"taskId": task_id},
                                       headers=self._headers()) as resp:
                    data = await resp.json()
            if data.get("code") != 200:
                raise RuntimeError(f"KIE Veo record-info failed: {data.get('msg', data)}")
            d = data.get("data", {})
            flag = d.get("successFlag")
            if flag in (2, 3):
                raise RuntimeError(f"Veo task failed: {d.get('errorMessage', 'unknown error')}")
            if flag != 1:
                return None
            response = d.get("response") or {}
            urls = response.get("resultUrls") or response.get("fullResultUrls") or []
        else:
            url = f"{API_BASE}/api/v1/jobs/recordInfo"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params={"taskId": task_id},
                                       headers=self._headers()) as resp:
                    data = await resp.json()
            if data.get("code") != 200:
                raise RuntimeError(f"KIE recordInfo failed: {data.get('msg', data)}")
            task = data.get("data") or {}
            state = task.get("state", "")
            if state in ("failed", "error"):
                raise RuntimeError(f"Task failed: {task.get('failMsg', 'unknown error')}")
            if state != "success":
                return None
            urls = self._parse_result_urls(task)

        if not urls:
            raise RuntimeError("Task done but no result URLs")
        return await self._download_file(urls[0], ext)
