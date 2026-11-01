"""Trusted input policy and exact payment identities for the shared ledger."""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from .models import cents, local_datetime


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
