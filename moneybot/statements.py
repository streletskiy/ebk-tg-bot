"""Bounded, deterministic bank statement readers. No model interprets amounts.

Yandex's text format follows streletskiy/ebk-bank-helper (MIT, see LICENSE).
"""

import csv
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

MAX_ROWS = 1000
MAX_PAGES = 100
MAX_TEXT = 2_000_000
DATE = r"\d{2}\.\d{2}\.\d{4}"
TIME = r"\d{2}:\d{2}(?::\d{2})?"
AMOUNT = r"([+-]?\s*\d[\d ]*[.,]\d{2})\s*(₽|RUB|RUR|руб\.?|USD|EUR|CNY|GBP|KZT|BYN|\$|€)"
MONEY_RE = re.compile(AMOUNT, re.I)
ALIASES = {"₽": "RUB", "RUR": "RUB", "РУБ": "RUB", "РУБ.": "RUB", "$": "USD", "€": "EUR"}


class StatementError(ValueError):
    pass


def clean(text):
    return re.sub(r"\s+", " ", str(text).translate(str.maketrans("‐‑‒–—−", "------"))).strip()


def amount(text):
    try:
        value = Decimal(re.sub(r"\s", "", str(text)).replace(",", "."))
        if not value.is_finite() or abs(value) > Decimal("9999999999.99") or value * 100 != int(value * 100):
            raise ValueError
        return int(value * 100)
    except (ValueError, InvalidOperation):
        raise StatementError("В выписке некорректная сумма; импорт остановлен.") from None


def currency(text):
    code = clean(text).upper()
    code = ALIASES.get(code, code)
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise StatementError("Не удалось определить валюту операции.")
    return code


def redact(text):
    text = re.sub(r"(?:\+?\d[\d ()*-]{8,}\d)", "[реквизиты]", clean(text))
    return text[:180]


def transfer_details(description, bank, kind, fields=None):
    """Extract only stated counterparties; retain masked references, never raw numbers."""
    text = clean(description)[:2000]
    fields = fields or {}
    incoming = kind == "income"
    party_key = "Отправитель" if incoming else "Получатель"
    bank_key = "Банк отправителя" if incoming else "Банк получателя"
    party, destination = fields.get(party_key, ""), fields.get(bank_key, "")
    if not re.search(r"\bперевод\b", text, re.I) and not (party or destination):
        return {}
    labels = r"отправитель|от кого" if incoming else r"получатель|кому"
    boundary = r"(?=\s*[,;]\s*(?:банк|получатель|отправитель|кому|от кого|сч[её]т|телефон)\b|$)"
    named = re.search(rf"(?:{labels})\s*:\s*(.+?){boundary}", text, re.I)
    named_bank = re.search(rf"банк(?:\s+(?:получателя|отправителя))?\s*:\s*(.+?){boundary}", text, re.I)
    if not party and named:
        party = named[1]
    if not destination and named_bank:
        destination = named_bank[1]
    if re.fullmatch(r"Перевод себе[.]?", text, re.I):
        party = "Себе"
    account_bank = re.search(r"сч[её]т\s+(?:\[реквизиты\]|[\d *-]{5,})\s*,\s*(.+)$", text, re.I)
    if not destination and account_bank:
        destination = account_bank[1]
    if not destination and bank == "tbank" and re.match(r"(?:Внутренний перевод|Перевод с договора)\b", text, re.I):
        destination = "Т-Банк (внутренний перевод)"
    reference = ""
    for label, pattern in (
        ("Телефон", r"(?:номеру телефона|телефон)\s*:?\s*(\+?[\d(][\d ()*-]{7,}\d)"),
        ("Договор", r"договор(?:а)?\s*:?\s*([\d*][\d *-]{4,}\d)"),
        ("Счёт", r"сч[её]т(?:а)?\s*:?\s*([\d*][\d *-]{4,}\d)"),
    ):
        match = re.search(pattern, text, re.I)
        if match:
            digits = re.sub(r"\D", "", match[1])
            if len(digits) >= 4:
                reference = label + " ••" + digits[-4:]
                break
    return {"party": redact(party), "bank": redact(destination), "reference": reference}


def row(bank, date, value, curr, description, tz, *, status="", precise=True):
    try:
        dt = datetime.strptime(clean(date), "%d.%m.%Y %H:%M:%S" if len(clean(date)) == 19 else
                               "%d.%m.%Y %H:%M" if ":" in date else "%d.%m.%Y").replace(tzinfo=tz)
    except ValueError:
        raise StatementError("Не удалось прочитать дату операции.") from None
    if dt.year < 1970 or dt.timestamp() > datetime.now(tz).timestamp() + 86400:
        raise StatementError("В выписке некорректная или будущая дата операции.")
    signed = amount(value)
    transfer = transfer_details(description, bank, "expense" if signed < 0 else "income")
    description = redact(description)
    if not description:
        raise StatementError("В выписке есть операция без описания.")
    curr = currency(curr)
    identity = json.dumps([bank, dt.isoformat(), signed, curr, description.casefold()], ensure_ascii=False)
    return {"occurred_at": dt.isoformat(), "amount": abs(signed), "currency": curr,
            "kind": "expense" if signed < 0 else "income", "description": description,
            "fingerprint": hashlib.sha256(identity.encode()).hexdigest(), "precise": precise,
            "source_skip": status or ("Нулевая сумма" if not signed else ""),
            **({"transfer": transfer} if transfer else {})}


def tabular(rows, tz):
    result = []
    for raw in rows:
        if not any(v is not None and str(v).strip() for v in raw.values()):
            continue
        if len(result) >= MAX_ROWS:
            raise StatementError(f"В одной выписке допускается до {MAX_ROWS} операций.")
        data = {clean(k): clean(v) if v is not None else "" for k, v in raw.items() if k is not None}
        date = raw.get("Дата операции", "")
        if isinstance(date, datetime):
            date = date.strftime("%d.%m.%Y %H:%M:%S")
        date = str(date)
        payment = data.get("Сумма платежа", "")
        curr = data.get("Валюта платежа", "") if payment else data.get("Валюта операции", "")
        value = payment or data.get("Сумма операции", "")
        if not curr or not value or not date:
            raise StatementError("Нужны дата, сумма и валюта платежа (либо операции) в каждой строке.")
        status = data.get("Статус", "").upper()
        skipped = "Неисполненная операция" if status and status not in ("OK", "SUCCESS", "ИСПОЛНЕНО", "ВЫПОЛНЕНА") else ""
        parsed = row("tbank", date, value, curr, data.get("Описание", ""), tz,
                     status=skipped, precise=":" in date)
        parsed.update(bank_category=redact(data.get("Категория", "")), mcc=data.get("MCC", "")[:4])
        transfer = transfer_details(data.get("Описание", ""), "tbank", parsed["kind"], data)
        if transfer:
            parsed["transfer"] = transfer
        result.append(parsed)
    return result


def parse_table(path, fmt, tz):
    required = {"Дата операции", "Описание"}
    if fmt == "csv":
        content = path.read_bytes()
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251")
        # Ignore optional Excel delimiter hint, but never guess column positions.
        if text.lower().startswith("sep="):
            text = text.split("\n", 1)[-1]
        sample = text.splitlines()[0] if text.splitlines() else ""
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        reader.fieldnames = [clean(v) for v in (reader.fieldnames or [])]
        if not required <= set(reader.fieldnames):
            raise StatementError("Нужна CSV-выгрузка Т-Банка с колонками «Дата операции» и «Описание».")
        return tabular(reader, tz)
    from openpyxl import load_workbook
    with ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 30_000_000 or len(archive.infolist()) > 1000:
            raise StatementError("XLSX слишком большой после распаковки.")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for sheet in workbook.worksheets:
            values = sheet.iter_rows(values_only=True)
            # Exports may have a title above the column names.
            for _ in range(20):
                header = next(values, ())
                names = [clean(v) if v is not None else "" for v in header]
                if required <= set(names):
                    sheets.append(tabular((dict(zip(names, v)) for v in values), tz))
                    break
        if len(sheets) != 1:
            raise StatementError("Нужен XLSX с одной таблицей операций Т-Банка.")
        return sheets[0]
    finally:
        workbook.close()


def noise(line):
    key = re.sub(r"\s", "", line).lower()
    return (not key or re.match(r"^(?:БИК|ИНН|КПП)\b", line, re.I) is not None
            or key in {"операции", "мск", "дата", "обработки", "договора", "списания",
            "картасуммаввалюте", "суммаввалюте", "номеркарты", "валютеоперации", "валютекарты"}
            or any(part in key for part in ("описаниеоперациидатаивремя", "датаивремядата", "универсальнаялицензия",
                  "банкароссии", "продолжениенаследующейстранице", "страница", "входящийостаток",
                  "исходящийостаток", "всегорасходных", "всегоприходных",
                  "операциисписанияоперацииввалютекарты")))


def parse_yandex(text, tz):
    # The original helper's description/date/time/settlement state machine, accepting
    # both text-flow and horizontal layout extraction. Unfinished rows fail closed.
    lines = [clean(v) for v in text.splitlines()]
    active = False
    pending = []
    result = []
    for line in lines:
        if "описаниеоперациидатаивремя" in line.replace(" ", "").lower():
            active = True
            continue
        if not active or noise(line):
            continue
        if re.search(r"^(Итого|С уважением|Пополнения:|Расходы:)", line, re.I):
            break
        pending.append(line)
        combined = " ".join(pending)
        amounts = list(MONEY_RE.finditer(combined))
        if len(amounts) < 2:
            continue
        if len(amounts) != 2:
            raise StatementError("Неоднозначные суммы в строке PDF Яндекса.")
        before = combined[:amounts[0].start()]
        dates = list(re.finditer(DATE, before))
        clock = re.search(TIME, before)
        if len(dates) == 2 and not clock:
            clock = re.search(TIME, combined[amounts[-1].end():])
            if not clock:
                # Horizontal PDF layouts place the operation's time on the next line.
                continue
        if len(dates) != 2 or not clock:
            raise StatementError("Не удалось полностью разобрать строку PDF Яндекса.")
        description = before[:dates[0].start()].strip()
        if not description:
            raise StatementError("Не удалось прочитать описание в PDF Яндекса.")
        payment = amounts[1]
        result.append(row("yandex", dates[0].group() + " " + clock.group(), payment[1], payment[2], description, tz))
        pending = []
    # Amount-only totals and bank footer text are not transactions, but a leftover
    # date or amount means a truncated or unsupported transaction, never a partial import.
    if pending and (re.search(DATE, " ".join(pending)) or MONEY_RE.search(" ".join(pending))):
        raise StatementError("В PDF Яндекса осталась неразобранная строка. Импорт остановлен.")
    return result


def parse_tbank(text, tz):
    blocks, pending = [], []
    active = False
    for raw in text.splitlines():
        line = clean(raw)
        if "движение средств за период" in line.lower() or "дата и время" in line.lower():
            active = True
            continue
        if not active or noise(line):
            continue
        if re.search(r"^(Пополнения:|Расходы:|С уважением|Итого)", line, re.I):
            break
        if re.match(rf"^{DATE}", line) and pending and len(MONEY_RE.findall(" ".join(pending))) >= 2:
            blocks.append(" ".join(pending))
            pending = []
        if re.match(rf"^{DATE}", line) and len(MONEY_RE.findall(line)) == 2:
            # The card column ends the first physical line, not the wrapped description.
            line = re.sub(r"\s+\*?\d{4}\s*$", "", line)
        if pending or re.match(rf"^{DATE}", line):
            pending.append(line)
    if pending:
        blocks.append(" ".join(pending))
    result = []
    for block in blocks:
        amounts = list(MONEY_RE.finditer(block))
        if len(amounts) != 2:
            raise StatementError("Не удалось полностью разобрать суммы PDF Т-Банка.")
        prefix = block[:amounts[0].start()]
        dates = re.findall(DATE, prefix)
        clocks = re.findall(TIME, prefix)
        # Layout extraction can put the times below the amounts and description.
        tail = block[amounts[1].end():]
        if not clocks:
            clocks = re.findall(TIME, tail)
            tail = re.sub(TIME, "", tail)
        if len(dates) != 2 or not clocks:
            raise StatementError("Не удалось прочитать дату и время PDF Т-Банка.")
        description = re.sub(r"\s+\*?\d{4}\s*$", "", tail).strip()
        payment = amounts[1]
        result.append(row("tbank", dates[0] + " " + clocks[0], payment[1], payment[2], description, tz))
    return result


def parse_pdf(path, tz):
    from pypdf import PdfReader
    if not path.read_bytes()[:1024].lstrip().startswith(b"%PDF-"):
        raise StatementError("Файл не является PDF.")
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise StatementError("PDF защищён паролем. Пришлите незашифрованную выписку.")
    if len(reader.pages) > MAX_PAGES:
        raise StatementError(f"В одной выписке допускается до {MAX_PAGES} страниц.")
    parts, flow_parts = [], []
    for page in reader.pages:
        text = page.extract_text(extraction_mode="layout")
        flow_parts.append(page.extract_text())
        parts.append(text)
        if sum(map(len, parts)) + sum(map(len, flow_parts)) > MAX_TEXT:
            raise StatementError("Слишком большой текст PDF.")
    text = "\n".join(parts)
    compact = re.sub(r"\s", "", text[:5000]).lower()
    # A merchant named Yandex inside a T-Bank statement is not the issuing bank.
    if any(v in compact for v in ("тбанк", "т-банк", "тинькоффбанк", "tinkoffbank", "t-bank")):
        bank, parser = "tbank", parse_tbank
    elif any(v in compact for v in ("яндексбанк", "yandexbank", "описаниеоперациидатаивремя")):
        bank, parser = "yandex", parse_yandex
    else:
        raise StatementError("Нужна текстовая PDF-выписка Яндекс Банка или Т-Банка; сканы не поддерживаются.")
    # Yandex's exported text flow is stable across wrapped descriptions; T-Bank's
    # horizontal table benefits from preserving layout. Fall back only on total failure.
    variants = ["\n".join(flow_parts), text] if bank == "yandex" else [text, "\n".join(flow_parts)]
    failure = None
    for variant in variants:
        try:
            rows = parser(variant, tz)
            if rows:
                verification = {"pages": len(reader.pages), "totals_verified": False}
                if bank == "tbank":
                    expected = len(re.findall(rf"^\s*{DATE}\s+{DATE}\b", text, re.M))
                    if expected and expected != len(rows):
                        raise StatementError("Количество строк PDF не совпало с разбором. Импорт остановлен.")
                    totals = [re.search(label + r"\s*:\s*" + AMOUNT, text, re.I)
                              for label in ("Пополнения", "Расходы")]
                    if all(totals):
                        for kind, total in zip(("income", "expense"), totals):
                            code = currency(total[2])
                            if (any(r["currency"] != code for r in rows)
                                    or sum(r["amount"] for r in rows if r["kind"] == kind) != amount(total[1])):
                                raise StatementError("Суммы строк не совпали с итогами PDF. Импорт остановлен.")
                        verification["totals_verified"] = True
                return bank, rows, verification
        except StatementError as exc:
            failure = exc
    raise failure or StatementError("В PDF не найдено распознаваемых строк операций.")


def parse_file(path, fmt, timezone):
    tz = ZoneInfo(timezone)
    path = Path(path)
    if path.stat().st_size > 15 * 1024 * 1024:
        raise StatementError("Выписка должна быть не больше 15 МБ.")
    if fmt == "pdf":
        bank, rows, verification = parse_pdf(path, tz)
    elif fmt in ("csv", "xlsx"):
        bank, rows = "tbank", parse_table(path, fmt, tz)
        verification = {"totals_verified": False}
    else:
        raise StatementError("Поддерживаются PDF, CSV и XLSX.")
    if not rows or len(rows) > MAX_ROWS:
        raise StatementError(f"Выписка должна содержать от 1 до {MAX_ROWS} распознанных операций.")
    return {"bank": bank, "format": fmt, "rows": rows, "parser_version": 3, "verification": verification}


def main():
    # Resource limits belong to this short-lived parser process, not the bot.
    # openpyxl optionally imports the NumPy bundled with Whisper; BLAS is unnecessary
    # for reading cells and its default thread pools exhaust the parser's address space.
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    try:
        result = parse_file(*sys.argv[1:])
    except StatementError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "Не удалось прочитать выписку. Пришлите исходный PDF, CSV или XLSX из банка."}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
