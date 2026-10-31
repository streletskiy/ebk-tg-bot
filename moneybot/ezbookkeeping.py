import json
from collections import defaultdict
from datetime import datetime, timedelta

import httpx

from .models import cents, local_datetime, money


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
    def __init__(self, settings, store):
        self.s, self.store = settings, store
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False)

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

    async def catalog(self):
        accounts = await self.call("accounts/list.json")
        categories = await self.call("transaction/categories/list.json")
        return {"accounts": leaves(accounts, "subAccounts"),
                "categories": leaves(categories, "subCategories")}

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

    async def create_once(self, marker, job_id, payload):
        previous = self.store.write(marker)
        if previous:
            if previous["state"] == "done":
                return json.loads(previous["result"] or '{}')
            return await self.reconcile(marker, json.loads(previous["payload"]))
        # Commit the intent BEFORE network I/O. An ambiguous POST is never automatically repeated.
        self.store.prepare(marker, job_id, payload)
        try:
            result = await self.call("transactions/add.json", body=payload)
        except EzError:
            return await self.reconcile(marker, payload)
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
        lines = [heading]
        for currency, values in sorted(totals.items()):
            lines.append(f"{currency}: доходы {money(values[2])}; расходы {money(values[3])}; разница {money(values[2]-values[3])}.")
        lines.append(f"Операций: {len(relevant)}. Переводы и корректировки баланса не включены.")
        for (currency, kind), amounts in sorted(statistics.items()):
            from decimal import Decimal, ROUND_HALF_UP
            average = int((Decimal(sum(amounts)) / len(amounts)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            lines.append(f"{'Расходы' if kind == 3 else 'Доходы'} {currency}: {len(amounts)} записей, среднее {money(average)}, минимум {money(min(amounts))}, максимум {money(max(amounts))}.")
        if query.view == "transactions":
            lines.append("Последние записи (до 30):")
            for row, currency, name in relevant[:30]:
                dt = datetime.fromtimestamp(row["time"], self.s.timezone)
                comment = row.get("comment", "").split("\n[moneybot:", 1)[0][:140]
                sign = "−" if row["type"] == 3 else "+"
                lines.append(f"{dt:%d.%m}: {sign}{money(row['sourceAmount'])} {currency} · {name} · {comment}")
        else:
            labels = {"category": "По категориям", "month": "По месяцам", "day": "По дням", "account": "По счетам", "merchant": "По описаниям (магазины не нормализованы)"}
            lines.append(labels[query.group_by] + ":")
            ordered = sorted(by_category.items(), key=(lambda pair: pair[0]) if query.group_by in ("month", "day") else (lambda pair: -pair[1]))
            for (currency, kind, name), amount in ordered[:30]:
                lines.append(f"{'−' if kind == 3 else '+'} {name}: {money(amount)} {currency}")
            if len(ordered) > 30:
                lines.append(f"Показаны 30 из {len(ordered)} групп; итог включает все.")
        return "\n".join(lines)
