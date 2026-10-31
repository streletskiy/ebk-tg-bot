import asyncio
import os
import time

from PIL import Image, ImageOps

from .ezbookkeeping import UserInputError

Image.MAX_IMAGE_PIXELS = 30_000_000


class Media:
    def __init__(self, settings, telegram):
        self.s, self.telegram = settings, telegram
        self.directory = settings.data / "media"
        self.directory.mkdir(exist_ok=True)
        self.whisper = None

    async def extract(self, message, job_id, progress=None):
        text = message.get("text") or message.get("caption") or ""
        if len(text) > 12000:
            raise UserInputError("Сообщение слишком длинное: сократите до 12 000 символов.")
        images = []
        media = None
        kind = None
        if message.get("photo"):
            media, kind = message["photo"][-1], "image"
        elif message.get("voice") or message.get("audio"):
            media, kind = message.get("voice") or message["audio"], "audio"
        elif message.get("document"):
            media = message["document"]
            kind = "image" if media.get("mime_type", "").startswith("image/") else None
        if media and kind is None:
            raise UserInputError("Пришлите текст, фотографию/изображение или голосовое сообщение.")
        if media:
            if media.get("file_size", 0) > self.s.max_media:
                raise UserInputError(f"Размер файла должен быть не больше {self.s.max_media//1024//1024} МБ.")
            if kind == "audio" and media.get("duration", 0) > self.s.max_voice:
                raise UserInputError(f"Голосовое должно быть не длиннее {self.s.max_voice} секунд.")
            raw = self.directory / f"{job_id}.input"
            try:
                if progress:
                    progress("Скачиваю изображение" if kind == "image" else "Скачиваю голосовое сообщение")
                await self.telegram.download(media["file_id"], raw)
                if kind == "image":
                    if progress:
                        progress("Подготавливаю изображение к распознаванию")
                    image_path = self.directory / f"{job_id}.jpg"
                    await asyncio.to_thread(self.normalize_image, raw, image_path)
                    images.append(str(image_path))
                else:
                    if progress:
                        progress("Распознаю голосовое сообщение")
                    transcript = await asyncio.to_thread(self.transcribe, raw)
                    text = (text + "\nРаспознанное голосовое сообщение: " + transcript).strip()
            finally:
                raw.unlink(missing_ok=True)
        if not text and not images:
            raise UserInputError("Пришлите описание покупки, чек или голосовое сообщение.")
        return text, images

    @staticmethod
    def normalize_image(source, destination):
        try:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((2400, 2400))
                image.save(destination, "JPEG", quality=90)
        except Exception as exc:
            raise UserInputError("Не удалось открыть изображение. Пришлите JPEG или PNG.") from exc

    def transcribe(self, path):
        import av
        import numpy as np
        from faster_whisper import WhisperModel

        # Decode once, enforce the real duration, and pass PCM to Whisper. This also avoids
        # faster-whisper's legacy PyAV metadata_errors argument removed in newer PyAV releases.
        seconds = 0
        chunks = []
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        try:
            with av.open(str(path)) as container:
                for frame in container.decode(audio=0):
                    seconds += frame.samples / frame.sample_rate
                    if seconds > self.s.max_voice + 1:
                        raise UserInputError(f"Голосовое должно быть не длиннее {self.s.max_voice} секунд.")
                    for output in resampler.resample(frame):
                        chunks.append(output.to_ndarray().reshape(-1))
                for output in resampler.resample(None):
                    chunks.append(output.to_ndarray().reshape(-1))
            if not chunks:
                raise UserInputError("Аудио не содержит речи.")
            audio = np.concatenate(chunks).astype(np.float32) / 32768.0
        except UserInputError:
            raise
        except Exception as exc:
            raise UserInputError("Не удалось прочитать аудио. Пришлите голосовое сообщение ещё раз.") from exc
        if self.whisper is None:
            self.whisper = WhisperModel(os.getenv("WHISPER_MODEL", "small"), device="cpu", compute_type="int8",
                cpu_threads=int(os.getenv("WHISPER_THREADS", "4")), download_root=str(self.s.data / "models"))
        segments, _ = self.whisper.transcribe(audio, language=os.getenv("WHISPER_LANGUAGE", "ru"),
                                               beam_size=5, vad_filter=True, condition_on_previous_text=False)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            raise UserInputError("Не удалось разобрать речь. Напишите сумму и покупку текстом.")
        return text

    def cleanup(self):
        cutoff = time.time() - 86400
        for file in self.directory.iterdir():
            if file.is_file() and file.stat().st_mtime < cutoff:
                file.unlink()
