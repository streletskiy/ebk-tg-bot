"""Trusted input policy and exact payment identities for the shared ledger."""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from .models import cents, local_datetime


CYRILLIC_TRANSLITERATION = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def validate_image_coverage(entries, visible, image_count):
    def identity(row):
        if not 1 <= row.source_image <= image_count:
            raise ValueError("Invalid payment image index")
        return row.source_image, row.kind, cents(row.amount), row.currency.upper()

    expected = Counter(identity(row) for row in visible)
    actual = Counter(identity(row) for row in entries if row.source_image)
    if expected != actual:
        raise ValueError("Image payment inventory does not match the plan")


def merchant_words(value):
    # Ledger comments and bank screenshots may spell the same name in different scripts.
    value = unicodedata.normalize("NFKC", value).casefold().translate(CYRILLIC_TRANSLITERATION)
    return re.findall(r"[^\W_]+", value, re.UNICODE)


def has_merchant_words(description, merchant):
    # Only a complete name/phrase matches; a shared generic word is insufficient.
    return bool(merchant) and any(description[i:i + len(merchant)] == merchant
               for i in range(len(description) - len(merchant) + 1))


def same_merchant(merchant, comment):
    words = merchant_words(merchant)
    description = merchant_words(comment.split("\n[moneybot:", 1)[0])
    return len("".join(words)) >= 4 and has_merchant_words(description, words)


def confirmation_only(question):
    text = " ".join(question.casefold().split())
    if re.search(r"\b(?:како\w*|какую|какая|когда|сколько|куда)\b", text):
        return False
    return bool(re.search(
        r"^(?:(?:нужно|надо|можно|хотите|желаете|давайте)\s+(?:ли\s+)?)?"
        r"(?:записать|добавить|сохранить|учесть|внести)\b|"
        r"^(?:подтвердите|подтверждаете)\b|"
        r"уже.{0,80}(?:потрат|оплат|соверш).{0,80}или.{0,40}(?:планир|собира)", text))


def image_hashes(images):
    # Metadata/recompression may differ; the normalized pixels identify exact copies.
    from PIL import Image
    result = []
    for path in images:
        with Image.open(Path(path)) as image:
            image = image.convert("RGB")
            digest = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
            result.append(digest)
    return result


def normalize_payment_time(entry, timezone):
    if entry.source_image and entry.time_precision in ("day", "unknown"):
        entry.occurred_at = local_datetime(entry.occurred_at, timezone).replace(
            hour=12, minute=0, second=0, microsecond=0).isoformat()


def payment_fingerprints(entries, images):
    hashes = image_hashes(images)
    positions = Counter()
    result = []
    for entry in entries:
        identities = []
        if entry.source_image:
            if not 1 <= entry.source_image <= len(hashes):
                raise ValueError("Invalid payment image index")
            position = positions[entry.source_image]
            positions[entry.source_image] += 1
            identities.append(["image", hashes[entry.source_image - 1], position,
                               entry.kind, cents(entry.amount), entry.currency])
        reference = "".join(c for c in unicodedata.normalize("NFKC", entry.payment_reference).casefold()
                            if c.isalnum())
        # Short authorization codes, card tails and receipt numbers are not unique references.
        if len(reference) >= 12:
            identities.append(["reference", reference, entry.kind, entry.account_id,
                               cents(entry.amount), entry.currency])
        result.append([hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()
                       for value in identities])
    return result
