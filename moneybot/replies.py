"""Safe Telegram formatting and bounded, user-scoped clarification choices."""

import re
import secrets

from .telegram import Reply, button


def units(text):
    return len(text.encode("utf-16-le")) // 2


def styled_reply(text):
    """Format application output without interpreting catalog labels as Markdown."""
    entities = []
    for match in re.finditer(r"(?:\A|\n\n)([^\n]+)(?=\n|\Z)", text):
        heading = match.group(1)
        if len(heading) <= 140 and (match.end() < len(text) or heading.endswith(":")):
            entities.append({"type": "bold", "offset": units(text[:match.start(1)]), "length": units(heading)})
    for match in re.finditer(r"(?<!\w)/[a-z_]+(?:@[A-Za-z0-9_]+)?\b", text):
        offset, length = units(text[:match.start()]), units(match.group())
        if not any(e["offset"] < offset + length and offset < e["offset"] + e["length"] for e in entities):
            entities.append({"type": "code", "offset": offset, "length": length})
    return Reply(text, {}, sorted(entities, key=lambda e: e["offset"]))


def markdown_reply(text):
    """Render a small Markdown subset as entities; links and HTML stay literal."""
    pattern = re.compile(
        r"```(?:[\w+-]+\n)?([\s\S]*?)```|`([^`\n]+)`|\*\*([^*\n]+)\*\*|"
        r"__([^_\n]+)__|~~([^~\n]+)~~|(?<!\w)\*([^*\n]+)\*(?!\w)|(?<!\w)_([^_\n]+)_(?!\w)|"
        r"(?m:^#{1,6} +([^\n]+)$)|(?m:^[-*] +)"
    )
    parts, entities, end, offset = [], [], 0, 0
    kinds = ("pre", "code", "bold", "bold", "strikethrough", "italic", "italic", "bold")
    for match in pattern.finditer(text):
        prefix = text[end:match.start()]
        parts.append(prefix)
        offset += units(prefix)
        index = next((i for i, value in enumerate(match.groups()) if value is not None), None)
        if index is None:
            parts.append("• ")
            offset += 2
            end = match.end()
            continue
        content = match.group(index + 1)
        parts.append(content)
        length = units(content)
        if length:
            entities.append({"type": kinds[index], "offset": offset, "length": length})
        offset += length
        end = match.end()
    parts.append(text[end:])
    return Reply("".join(parts), {}, entities)


def clarification_reply(question, options, token=None):
    reply = markdown_reply(question)
    options = [option.strip() for option in options]
    if not options and re.search(r"\bда\s*(?:/|или|,)\s*нет\b", reply.text, re.IGNORECASE):
        options = ["Да", "Нет"]
    if not 2 <= len(options) <= 3 or any(not o or len(o) > 48 or "\n" in o for o in options):
        options = []
    if len({o.casefold() for o in options}) != len(options):
        options = []
    token = token or secrets.token_hex(8)
    if options:
        reply.markup = {"inline_keyboard": [[button(o, f"q:{token}:{i}") for i, o in enumerate(options)]]}
        reply.text += "\n\nМожно нажать кнопку или ответить своими словами."
    return reply, {"token": token, "options": options}
