import logging
import json
import time

from .telegram import TelegramError, message_chunks

log = logging.getLogger("moneybot")


class Feedback:
    """Single writer for Telegram status/result messages; financial work never waits for it."""

    def __init__(self, settings, store, telegram):
        self.s, self.store, self.telegram = settings, store, telegram
        self.chat_ready = {}
        self.rate_limit_until = 0

    @staticmethod
    def desired(job, queue):
        if job["status"] in ("done", "failed"):
            return message_chunks(job["response"] or "")
        if job["status"] == "processing":
            return ["⏳ Обрабатываю запрос\n\nСейчас: " + (job["progress"] or "Начинаю обработку")]
        entry = queue.get(job["id"], {"position": 1, "total": 1, "ready_at": 0})
        detail = "Ожидаю начала обработки."
        if job["attempts"]:
            detail = "Временная ошибка. Запрос сохранён, повторю обработку автоматически."
        elif entry["ready_at"] > time.time():
            detail = "Ожидаю повторной обработки вашего предыдущего сообщения."
        return [f"🕓 Запрос принят\n\nПозиция в очереди: {entry['position']} из {entry['total']}\n\n{detail}"]

    async def tick(self):
        now = time.time()
        for callback in self.store.pending_callbacks():
            try:
                if callback["user_id"] in self.s.contacts:
                    await self.telegram.call("answerCallbackQuery", callback_query_id=callback["callback_id"])
            except TelegramError as exc:
                if str(exc) != "TELEGRAM_400":
                    self.rate_limit_until = time.time() + max(exc.retry_after, 5)
                    return False
            self.store.callback_answered(callback["id"])
        if now < self.rate_limit_until:
            return False
        queue = self.store.active_queue()
        for job in self.store.feedback_candidates(now):
            user = job["user_id"]
            if user not in self.s.contacts:
                self.store.disable_feedback(job["id"])
                continue
            if now < self.chat_ready.get(user, 0):
                continue
            desired = self.desired(job, queue)
            markup = json.loads(job.get("reply_markup") or '{}') if job["status"] == "done" else {}
            if job["status"] == "failed":
                action = "login" if job.get("error") == "CODEX_LOGIN_REQUIRED" and user == self.s.owner else "retry"
                markup = {"inline_keyboard": [[{"text": "Подключить Codex" if action == "login" else "Повторить",
                                                 "callback_data": action, "style": "primary"}]]}
            markups = ["{}"] * len(desired)
            if markups:
                markups[-1] = json.dumps(markup, ensure_ascii=False, sort_keys=True)
            sent = self.store.feedback_messages(job["id"])
            pending = next(((part, text) for part, text in enumerate(desired)
                            if part not in sent or sent[part]["sent_text"] != text
                            or sent[part].get("sent_markup", "{}") != markups[part]), None)
            if pending is None:
                if job["status"] in ("done", "failed"):
                    self.store.feedback_complete(job["id"], job["response"])
                else:
                    self.store.defer_feedback(job["id"], now + 2)
                continue
            part, text = pending
            message_id = sent.get(part, {}).get("message_id")
            try:
                if message_id:
                    try:
                        await self.telegram.edit_text(user, message_id, text, json.loads(markups[part]))
                    except TelegramError as exc:
                        if not exc.message_missing:
                            raise
                        message_id = None
                if not message_id:
                    message_id = await self.telegram.send_text(user, text,
                        job["source_message_id"] if part == 0 else sent[0]["message_id"], json.loads(markups[part]))
                self.store.save_feedback(job["id"], part, message_id, text, markups[part])
                self.store.defer_feedback(job["id"], time.time() + 2)
                self.chat_ready[user] = time.time() + 1.2
                # Each chunk has a persistent ID, so retrying a long response reuses delivered parts.
                if job["status"] in ("done", "failed") and part == len(desired) - 1:
                    self.store.feedback_complete(job["id"], job["response"])
            except TelegramError as exc:
                log.warning("feedback job=%s error=%s", job["id"], str(exc))
                if str(exc) == "TELEGRAM_403":
                    self.store.disable_feedback(job["id"])
                else:
                    delay = max(exc.retry_after, 5)
                    self.store.defer_feedback(job["id"], time.time() + delay)
                    if exc.retry_after:
                        self.rate_limit_until = time.time() + exc.retry_after
            return True
        return False
