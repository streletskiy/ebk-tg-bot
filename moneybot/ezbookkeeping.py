import asyncio
import copy
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

import httpx

from .models import cents, local_datetime, money
from .payments import same_merchant


class EzError(Exception):
    pass


class UncertainWrite(EzError):
    pass


class UserInputError(Exception):
    pass


def leaves(items, child_key, parent=""):
    result = []
    if isinstance(items, dict):
        items = [item for group in items.values() for item in group]
    for item in items:
        if item.get("hidden"):
            continue
        children = item.get(child_key, [])
        if children:
            result.extend(leaves(children, child_key, item["name"]))
        else:
            result.append({**item, "name": f"{parent} / {item['name']}" if parent else item["name"]})
    return result


class EzBookkeeping:
    CATALOG_TTL = 300
    CATALOG_MAX_AGE = 86400

    def __init__(self, settings, store):
        self.s, self.store = settings, store
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False)
        self._catalog = None
        self._catalog_at = 0
        self._catalog_scope = None
        self._catalog_lock = asyncio.Lock()

    async def call(self, path, *, params=None, body=None):
        try:
            response = await self.http.request("POST" if body is not None else "GET",
                self.s.ez_url + "/api/v1/" + path, params=params, json=body,
                headers={"Authorization": "Bearer " + self.s.ez_token(),
                         "X-Timezone-Name": str(self.s.timezone)})
            if response.status_code >= 500:
                raise EzError("EZ_SERVER_ERROR")
            response.raise_for_status()
            result = response.json()
            if result.get("success") is not True:
                raise EzError("EZ_API_" + str(result.get("errorCode", "REJECTED")))
            return result.get("result")
        except (httpx.HTTPError, ValueError) as exc:
            raise EzError("EZ_REQUEST_FAILED") from exc

    def load_catalog_cache(self):
        scope = hashlib.sha256((self.s.ez_url + "\0" + self.s.ez_token()).encode()).hexdigest()
        if self._catalog_scope != scope:
            self._catalog_scope, self._catalog, self._catalog_at = scope, None, 0
            if self.store:
                try:
                    cached = json.loads(self.store.meta("catalog_cache", "{}"))
                    if cached.get("scope") == scope and 0 <= time.time()-cached["saved_at"] < self.CATALOG_MAX_AGE:
                        self._catalog, self._catalog_at = cached["value"], cached["saved_at"]
                except (ValueError, KeyError, TypeError):
                    pass

    async def catalog(self, *, fresh=True):
        """Menu reads may use cache; financial callers retain a mandatory fresh read."""
        self.load_catalog_cache()
        requested = time.time()
        if not fresh and self._catalog is not None and requested-self._catalog_at < self.CATALOG_MAX_AGE:
            return copy.deepcopy(self._catalog)
        async with self._catalog_lock:
            # Coalesce concurrent requests that were already satisfied while waiting.
            if self._catalog is not None and self._catalog_at >= requested:
                return copy.deepcopy(self._catalog)
            scope = self._catalog_scope
            accounts, categories = await asyncio.gather(self.call("accounts/list.json"),
                                                       self.call("transaction/categories/list.json"))
            self.load_catalog_cache()
            if scope != self._catalog_scope:
                raise EzError("EZ_CATALOG_SCOPE_CHANGED")
            catalog = {"accounts": leaves(accounts, "subAccounts"), "categories": leaves(categories, "subCategories")}
            saved_at = time.time()
            if self.store:
                self.store.set_meta("catalog_cache", json.dumps({"scope": self._catalog_scope,
                    "saved_at": saved_at, "value": catalog}, ensure_ascii=False))
            self._catalog, self._catalog_at = catalog, saved_at
            return copy.deepcopy(catalog)

    async def warm_catalog(self):
        self.load_catalog_cache()
        if self._catalog is None or time.time()-self._catalog_at >= self.CATALOG_TTL:
            await self.catalog()

    @staticmethod
    def model_catalog(catalog):
        return {"accounts": [{k: a.get(k) for k in ("id", "name", "currency")} for a in catalog["accounts"]],
                "categories": [{k: a.get(k) for k in ("id", "name", "type", "parentId")} for a in catalog["categories"]]}

    def payload(self, entry, catalog, marker):
        accounts = {a["id"]: a for a in catalog["accounts"]}
        categories = {c["id"]: c for c in catalog["categories"]}
        if entry.account_id not in accounts:
            raise UserInputError("Уточните счёт для записи.")
        account = accounts[entry.account_id]
        if account.get("type") == 2 or account["currency"] != entry.currency:
            raise UserInputError("Валюта покупки не совпадает с валютой счёта. Уточните счёт и сумму в его валюте.")
        category = categories.get(entry.category_id)
        expected = 2 if entry.kind == "expense" else 1
        if not category or category.get("type") != expected or str(category.get("parentId", "0")) == "0":
            raise UserInputError("Уточните подходящую подкатегорию дохода или расхода.")
        try:
            amount = cents(entry.amount)
            dt = local_datetime(entry.occurred_at, self.s.timezone)
        except ValueError as exc:
            raise UserInputError("Уточните сумму (не более двух знаков после запятой) и дату.") from exc
        if dt.year < 1970 or dt > datetime.now(self.s.timezone) + timedelta(days=1):
            raise UserInputError("Уточните дату: обычная запись не может быть в будущем.")
        return {"type": 3 if entry.kind == "expense" else 2,
                "categoryId": entry.category_id, "time": int(dt.timestamp()),
                "utcOffset": int(dt.utcoffset().total_seconds() // 60),
                "sourceAccountId": entry.account_id, "sourceAmount": amount,
                "comment": f"{entry.description}\n{marker}", "tagIds": []}

    async def reconcile(self, marker, payload):
        rows = await self.call("transactions/list/all.json", params={
            "keyword": marker, "start_time": payload["time"] - 86400,
            "end_time": payload["time"] + 86400, "trim_account": "true", "trim_category": "true"})
        matches = [r for r in rows if marker in r.get("comment", "")]
        if len(matches) == 1:
            result = matches[0]
            self.store.write_result(marker, result)
            return result
        raise UncertainWrite("EZ_WRITE_UNCERTAIN")

    async def payment_duplicate(self, payload, fingerprints):
        for fingerprint in fingerprints:
            previous_marker = self.store.payment_marker(fingerprint)
            if previous_marker:
                previous = self.store.write(previous_marker)
                if not previous:
                    raise UncertainWrite("EZ_PAYMENT_IDENTITY_UNCERTAIN")
                if previous["state"] == "done":
                    result = json.loads(previous["result"] or '{}')
                    if not result.get("id"):
                        result = await self.reconcile(previous_marker, payload)
                    return result, previous_marker
                return await self.reconcile(previous_marker, json.loads(previous["payload"])), previous_marker
            tag = f"[moneybot:payment:{fingerprint}]"
            rows = await self.call("transactions/list/all.json", params={
                "keyword": tag, "start_time": payload["time"] - 86400,
                "end_time": payload["time"] + 86400, "trim_account": "true", "trim_category": "true"})
            matches = [r for r in rows if tag in r.get("comment", "")]
            if matches:
                if len(matches) != 1 or matches[0].get("sourceAmount") != payload["sourceAmount"] or matches[0].get("type") != payload["type"]:
                    raise UncertainWrite("EZ_PAYMENT_IDENTITY_UNCERTAIN")
                return matches[0], ""
        return None

    async def matching_payment(self, payload, payment, used_ids):
        day = datetime.fromtimestamp(payload["time"], self.s.timezone).replace(
            hour=0, minute=0, second=0, microsecond=0)
        rows = await self.statement_history(payload["sourceAccountId"], int(day.timestamp()),
                                            int((day + timedelta(days=1)).timestamp()) - 1)
        matches = []
        for row in rows:
            references = re.findall(r"\[moneybot:reference:([a-f0-9]{64})\]", row.get("comment", ""))
            if not references:
                legacy = re.findall(r"\[moneybot:payment:([a-f0-9]{64})\]", row.get("comment", ""))
                references = legacy[1:]
            if payment.get("reference") and references and payment["reference"] not in references:
                continue
            account = row.get("sourceAccountId") or row.get("sourceAccount", {})
            account = account.get("id") if isinstance(account, dict) else account
            if (str(row.get("id", "")) in used_ids or not row.get("id")
                    or str(account) != payload["sourceAccountId"]
                    or row.get("type") != payload["type"] or row.get("sourceAmount") != payload["sourceAmount"]
                    or datetime.fromtimestamp(row["time"], self.s.timezone).date() != day.date()
                    or not same_merchant(payment["merchant"], row.get("comment", ""))):
                continue
            if payment["time_precision"] in ("second", "minute"):
                existing = datetime.fromtimestamp(row["time"], self.s.timezone)
                # Older screenshots without a time were recorded at noon. They still need deduplication.
                if existing.time().isoformat() != "12:00:00" and row["time"] // 60 != payload["time"] // 60:
                    continue
            matches.append(row)
        return sorted(matches, key=lambda row: (row["time"], str(row["id"])))

    async def create_once(self, marker, job_id, payload, *, fingerprints=(), payment=None, used_ids=()):
        previous = self.store.write(marker)
        if previous:
            if previous["state"] == "done":
                result = json.loads(previous["result"] or '{}')
                if payment and not result.get("id"):
                    result = await self.reconcile(marker, payload)
                return result
            return await self.reconcile(marker, json.loads(previous["payload"]))
        duplicate = await self.payment_duplicate(payload, fingerprints) if fingerprints else None
        if duplicate is None and payment and payment.get("merchant"):
            matches = await self.matching_payment(payload, payment, used_ids)
            if matches:
                duplicate = matches[0], ""
        if duplicate is not None:
            result, previous_marker = duplicate
            result = {**result, "_moneybot_duplicate": True}
            with self.store.transaction():
                self.store.prepare(marker, job_id, payload)
                for fingerprint in fingerprints:
                    if not self.store.payment_marker(fingerprint):
                        self.store.claim_payment(fingerprint, previous_marker or marker)
                self.store.write_result(marker, result)
            return result
        # Commit the intent BEFORE network I/O. An ambiguous POST is never automatically repeated.
        with self.store.transaction():
            self.store.prepare(marker, job_id, payload)
            for fingerprint in fingerprints:
                self.store.claim_payment(fingerprint, marker)
        try:
            result = await self.call("transactions/add.json", body=payload)
        except EzError:
            return await self.reconcile(marker, payload)
        self.store.write_result(marker, result)
        return result

    async def statement_history(self, account_id, start, end):
        params = {"count": 50, "min_time": start * 1000, "max_time": end * 1000,
                  "account_ids": account_id, "with_count": "true"}
        rows, seen, cursors = [], set(), set()
        while True:
            page = await self.call("transactions/list.json", params=params)
            if page.get("totalCount", 0) > self.s.max_rows:
                raise UserInputError("Слишком много записей для сверки. Пришлите выписку за меньший период.")
            for item in page.get("items", []):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    rows.append(item)
            if len(rows) > self.s.max_rows:
                raise UserInputError("Слишком много записей для сверки. Сократите период выписки.")
            cursor = page.get("nextTimeSequenceId")
            if not cursor:
                return rows
            if cursor in cursors or not page.get("items"):
                raise EzError("EZ_PAGINATION_STALLED")
            cursors.add(cursor)
            params["max_time"] = cursor

    async def transaction_detail(self, transaction_id):
        return await self.call("transactions/get.json", params={"id": transaction_id,
            "with_pictures": "true", "trim_account": "false", "trim_category": "false", "trim_tag": "false"})

    @staticmethod
    def editable_transaction(row):
        def identifier(value):
            return str(value.get("id", "")) if isinstance(value, dict) else str(value or "")
        if row.get("type") not in (2, 3) or row.get("editable") is False:
            raise UserInputError("Нельзя изменять время этой операции.")
        return {"id": str(row["id"]), "type": row["type"], "time": row["time"],
                "utcOffset": row.get("utcOffset", 0),
                "categoryId": identifier(row.get("categoryId") or row.get("category")),
                "sourceAccountId": identifier(row.get("sourceAccountId") or row.get("sourceAccount")),
                "sourceAmount": row["sourceAmount"], "destinationAccountId": "0",
                "destinationAmount": 0, "hideAmount": row.get("hideAmount", False), "comment": row.get("comment", ""),
                "tagIds": row.get("tagIds", [identifier(t) for t in row.get("tags", [])]),
                "pictureIds": [str(p["pictureId"]) for p in row.get("pictures", [])],
                "geoLocation": row.get("geoLocation")}

    async def update_time_once(self, marker, job_id, before, after):
        previous = self.store.write(marker)
        if previous and previous["state"] == "done":
            return json.loads(previous["result"] or '{}')
        current = await self.transaction_detail(after["id"])
        snapshot = self.editable_transaction(current)
        if previous:
            # A lost modification response is reconciled by ID and the complete expected state.
            if snapshot == after:
                self.store.write_result(marker, current)
                return current
            raise UncertainWrite("EZ_WRITE_UNCERTAIN")
        if snapshot != before:
            raise UserInputError("Запись изменилась после предпросмотра. Импорт остановлен; нужна новая сверка.")
        self.store.prepare(marker, job_id, {"before": before, "after": after})
        try:
            result = await self.call("transactions/modify.json", body=after)
        except EzError:
            return await self.update_time_once(marker, job_id, before, after)
        self.store.write_result(marker, result)
        return result

    async def report(self, query, catalog, progress=None):
        if progress:
            progress("Получаю данные для отчёта из ezBookkeeping")
        if query is None:
            raise UserInputError("Уточните период отчёта.")
        if query.view == "balances":
            return "Остатки по счетам:\n" + "\n".join(
                f"{a['name']}: {money(a['balance'])} {a['currency']}" for a in catalog["accounts"])
        try:
            start, end = (local_datetime(v, self.s.timezone) for v in (query.start, query.end))
        except ValueError as exc:
            raise UserInputError("Уточните даты начала и конца периода.") from exc
        if end <= start or (end - start).days > 3660:
            raise UserInputError("Выберите период от одного дня до десяти лет.")
        account_ids = {a["id"] for a in catalog["accounts"]}
        category_ids = {c["id"] for c in catalog["categories"]}
        if not set(query.account_ids) <= account_ids or not set(query.category_ids) <= category_ids:
            raise UserInputError("Не удалось определить счёт или категорию для отчёта.")
        params = {"count": 50, "max_time": int(end.timestamp()) * 1000 - 1,
                  "min_time": int(start.timestamp()) * 1000, "with_count": "true"}
        if query.kind != "all":
            params["type"] = 3 if query.kind == "expense" else 2
        if query.account_ids:
            params["account_ids"] = ",".join(query.account_ids)
        if query.category_ids:
            params["category_ids"] = ",".join(query.category_ids)
        if query.keyword:
            params["keyword"] = query.keyword[:100]
        rows, seen, cursors = [], set(), set()
        while True:
            if progress and rows:
                progress(f"Загружаю операции для отчёта: получено {len(rows)}")
            page = await self.call("transactions/list.json", params=params)
            if page.get("totalCount", 0) > self.s.max_rows:
                raise UserInputError("Слишком много записей для одного отчёта. Уточните период или категорию.")
            for row in page.get("items", []):
                if row["id"] not in seen:
                    rows.append(row)
                    seen.add(row["id"])
            if len(rows) > self.s.max_rows:
                raise UserInputError("Слишком много записей. Уточните период.")
            cursor = page.get("nextTimeSequenceId")
            if not cursor or not page.get("items"):
                break
            if cursor in cursors:
                raise EzError("EZ_PAGINATION_STALLED")
            cursors.add(cursor)
            params["max_time"] = cursor
        if progress:
            progress("Считаю итоги и формирую отчёт")
        return self.format_report(rows, query, start, end, catalog)

    def format_report(self, rows, query, start, end, catalog):
        accounts = {a["id"]: a for a in catalog["accounts"]}
        totals, by_category = {}, {}
        statistics = defaultdict(list)
        relevant = []
        for row in rows:
            if row["type"] not in (2, 3):
                continue
            if not start.timestamp() <= row["time"] < end.timestamp():
                continue
            account = row.get("sourceAccount", {})
            if not isinstance(account, dict):
                account = accounts.get(str(account), {})
            account = account or accounts.get(str(row.get("sourceAccountId")), {})
            currency = account.get("currency")
            if not currency:
                raise EzError("EZ_UNKNOWN_CURRENCY")
            amount = int(row["sourceAmount"])
            statistics[(currency, row["type"])].append(amount)
            total = totals.setdefault(currency, {2: 0, 3: 0})
            total[row["type"]] += amount
            category = row.get("category", {})
            name = category.get("name", "Без категории") if isinstance(category, dict) else str(category)
            if query.group_by in ("month", "day"):
                dt = datetime.fromtimestamp(row["time"], self.s.timezone)
                name = dt.strftime("%Y-%m" if query.group_by == "month" else "%Y-%m-%d")
            elif query.group_by == "account":
                name = account.get("name", "Счёт")
            elif query.group_by == "merchant":
                name = row.get("comment", "").split("\n[moneybot:", 1)[0][:120] or "Без описания"
            key = (currency, row["type"], name)
            by_category[key] = by_category.get(key, 0) + amount
            relevant.append((row, currency, name))
        heading = f"{start:%d.%m.%Y} — {(end-timedelta(seconds=1)):%d.%m.%Y} ({self.s.timezone})"
        if not relevant:
            return heading + "\nЗаписей по запросу нет."
        lines = [heading, ""]
        for currency, values in sorted(totals.items()):
            lines.append(f"Итоги · {currency}\nДоходы: {money(values[2])} {currency}\n"
                         f"Расходы: {money(values[3])} {currency}\n"
                         f"Разница: {money(values[2]-values[3])} {currency}\n")
        lines.append(f"Операций: {len(relevant)}. Переводы и корректировки баланса не включены.")
        for (currency, kind), amounts in sorted(statistics.items()):
            from decimal import Decimal, ROUND_HALF_UP
            average = int((Decimal(sum(amounts)) / len(amounts)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            lines.append(f"\n{'Расходы' if kind == 3 else 'Доходы'} · {currency}\nЗаписей: {len(amounts)}\n"
                         f"Среднее: {money(average)} · минимум: {money(min(amounts))} · максимум: {money(max(amounts))}")
        if query.view == "transactions":
            lines.append("\nПоследние записи (до 30):")
            for row, currency, name in relevant[:30]:
                dt = datetime.fromtimestamp(row["time"], self.s.timezone)
                comment = row.get("comment", "").split("\n[moneybot:", 1)[0][:140]
                sign = "−" if row["type"] == 3 else "+"
                lines.append(f"{dt:%d.%m}: {sign}{money(row['sourceAmount'])} {currency} · {name} · {comment}")
        else:
            labels = {"category": "По категориям", "month": "По месяцам", "day": "По дням", "account": "По счетам", "merchant": "По описаниям (магазины не нормализованы)"}
            lines.append("\n" + labels[query.group_by] + ":")
            ordered = sorted(by_category.items(), key=(lambda pair: pair[0]) if query.group_by in ("month", "day") else (lambda pair: -pair[1]))
            for (currency, kind, name), amount in ordered[:30]:
                lines.append(f"{'−' if kind == 3 else '+'} {name}: {money(amount)} {currency}")
            if len(ordered) > 30:
                lines.append(f"Показаны 30 из {len(ordered)} групп; итог включает все.")
        return "\n".join(lines)
