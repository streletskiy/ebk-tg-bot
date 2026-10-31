from pathlib import PurePosixPath
from dataclasses import dataclass

import httpx


@dataclass
class Reply:
    text: str
    markup: dict


def button(text, data=None, style=None):
    result = {"text": text[:64]}
    if data is None:
        result["disabled"] = {}
    else:
        if not 1 <= len(data.encode()) <= 64:
            raise ValueError("Invalid callback size")
        result["callback_data"] = data
    if style:
        result["style"] = style
    return result


class TelegramError(Exception):
    def __init__(self, code, *, retry_after=0, not_modified=False, message_missing=False, bad_markup=False):
        super().__init__(code)
        self.retry_after = retry_after
        self.not_modified = not_modified
        self.message_missing = message_missing
        self.bad_markup = bad_markup


def message_chunks(text, limit=3500):
    chunks = []
    while text:
        # Telegram counts UTF-16 units; astral symbols must not overrun its limit.
        units = 0
        end = 0
        for char in text:
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                break
            end += 1
        chunk = text[:end]
        if end < len(text) and "\n" in chunk and chunk.rsplit("\n", 1)[0]:
            chunk = chunk.rsplit("\n", 1)[0]
        chunks.append(chunk)
        text = text[len(chunk):].lstrip("\n")
    return chunks


class Telegram:
    def __init__(self, settings):
        self.s = settings
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15), follow_redirects=False)

    async def call(self, method, **payload):
        try:
            response = await self.http.post(f"https://api.telegram.org/bot{self.s.token}/{method}", json=payload)
            data = response.json()
            if not data.get("ok"):
                code = data.get("error_code", response.status_code)
                description = str(data.get("description", "")).lower()
                delay = data.get("parameters", {}).get("retry_after", 0)
                raise TelegramError(f"TELEGRAM_{code}",
                    retry_after=max(0, min(int(delay), 86400)),
                    not_modified=code == 400 and "message is not modified" in description,
                    bad_markup=code == 400 and any(word in description for word in ("button", "reply markup", "reply_markup")),
                    message_missing=code == 400 and any(part in description for part in (
                        "message to edit not found", "message can't be edited", "message_id_invalid")))
            return data["result"]
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError("TELEGRAM_NETWORK_ERROR") from exc

    async def reply(self, user, text):
        # Plain text: model/merchant content cannot inject Telegram HTML or Markdown links.
        for chunk in message_chunks(text):
            await self.send_text(user, chunk)

    async def send_text(self, user, text, source_message_id=None, markup=None):
        payload = {"chat_id": user, "text": text, "link_preview_options": {"is_disabled": True}}
        if source_message_id:
            payload["reply_parameters"] = {"message_id": source_message_id, "allow_sending_without_reply": True}
        if markup:
            payload["reply_markup"] = markup
        result = await self.markup_call("sendMessage", payload)
        return result["message_id"]

    async def edit_text(self, user, message_id, text, markup=None):
        try:
            await self.markup_call("editMessageText", {"chat_id": user, "message_id": message_id, "text": text,
                            "link_preview_options": {"is_disabled": True}, "reply_markup": markup or {"inline_keyboard": []}})
        except TelegramError as exc:
            if not exc.not_modified:
                raise

    async def markup_call(self, method, payload):
        try:
            return await self.call(method, **payload)
        except TelegramError as exc:
            if not exc.bad_markup or "reply_markup" not in payload:
                raise
            # Only retry definite markup rejections, never ambiguous delivery failures.
            rows = [[{k: v for k, v in b.items() if k != "style"} for b in row if "disabled" not in b]
                    for row in payload["reply_markup"].get("inline_keyboard", [])]
            return await self.call(method, **{**payload, "reply_markup": {"inline_keyboard": [r for r in rows if r]}})

    async def download(self, file_id, target):
        info = await self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > self.s.max_media:
            raise ValueError("Файл слишком большой.")
        path = info.get("file_path", "")
        if not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts or any(c in path for c in "?#\\"):
            raise TelegramError("TELEGRAM_BAD_FILE_PATH")
        try:
            async with self.http.stream("GET", f"https://api.telegram.org/file/bot{self.s.token}/{path}") as response:
                response.raise_for_status()
                total = 0
                with target.open("wb") as output:
                    async for data in response.aiter_bytes():
                        total += len(data)
                        if total > self.s.max_media:
                            raise ValueError("Файл слишком большой.")
                        output.write(data)
        except httpx.HTTPError as exc:
            raise TelegramError("TELEGRAM_DOWNLOAD_ERROR") from exc
