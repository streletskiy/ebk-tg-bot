"""Persistent, private-chat controls for bank imports (Telegram Bot API 10.3)."""

import secrets
from collections import Counter
from datetime import datetime

from .ezbookkeeping import UserInputError
from .models import money
from .telegram import Reply, button


class StatementUI:
    PAGE_SIZE = 5

    def __init__(self, service):
        self.svc, self.engine, self.store = service, service.imports, service.store

    @staticmethod
    def control(statement, text, action, argument="", style=None):
        return button(text, f"s:{statement['token']}:{action}:{argument}", style)

    def view(self, statement, page=1, notice=""):
        rows = statement["rows"]
        pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(1, min(page, pages))
        counts = Counter(self.engine.decision(r)[0] for r in rows)
        lines = [notice] if notice else []
        bank = "Т-Банк" if statement["bank"] == "tbank" else "Яндекс Банк"
        lines += [f"{bank} · {len(rows)} операций", f"Счёт: {statement.get('account_name', 'ещё не выбран')}"]
        if statement.get("verification", {}).get("totals_verified"):
            lines.append("Количество строк и итоговые суммы PDF проверены.")
        if not statement.get("ready"):
            lines.append("Продолжу подбор категорий и проверку дублей автоматически.")
            return Reply("\n".join(lines), {"inline_keyboard": [
                [self.control(statement, "Продолжить проверку", "auto", style="primary")],
                [self.control(statement, "Отменить", "cancel", style="danger")]]})
        lines += [f"Добавить: {counts['new']} · уточнить время: {counts['time']}",
                  f"Пропустить: {counts['skip']} · требуют решения: {counts['review']}"]
        for kind, label in (("expense", "Расходы"), ("income", "Доходы")):
            total = sum(r["amount"] for r in rows if r["kind"] == kind and self.engine.decision(r)[0] == "new")
            if total:
                lines.append(f"{label} к добавлению: {money(total)} {statement['currency']}")
        lines.append(f"Операции · страница {page}/{pages}")
        keyboard = []
        for row in rows[(page-1)*self.PAGE_SIZE:page*self.PAGE_SIZE]:
            action, _ = self.engine.decision(row)
            label = {"new": "добавить", "time": "уточнить время", "skip": "пропустить", "review": "уточнить"}[action]
            dt = datetime.fromisoformat(row["occurred_at"])
            lines.append(f"{row['number']}. {dt:%d.%m %H:%M} · {'−' if row['kind']=='expense' else '+'}"
                         f"{money(row['amount'])} {row['currency']} · {label}\n{row['description']}"
                         + (" → " + row["category_name"] if row.get("category_name") and action == "new" else ""))
            keyboard.append([self.control(statement, f"{row['number']}. {row['description'][:45]}", "row", str(row["number"]))])
        navigation = []
        if page > 1:
            navigation.append(self.control(statement, "←", "page", str(page-1)))
        navigation.append(button(f"{page}/{pages}"))
        if page < pages:
            navigation.append(self.control(statement, "→", "page", str(page+1)))
        keyboard.append(navigation)
        if counts["review"]:
            keyboard.append([self.control(statement, f"Разобрать спорные · {counts['review']}", "questions", style="primary")])
        if counts["new"] or counts["time"]:
            keyboard.append([self.control(statement, f"Подтвердить: добавить {counts['new']}, время {counts['time']}",
                                          "confirm", style="success")])
        keyboard += [[self.control(statement, "Другой счёт", "accounts"), self.control(statement, "Обновить сверку", "refresh")],
                     [self.control(statement, "Отменить импорт", "cancel", style="danger")]]
        lines.append("Спорные строки не записываются. Откройте строку, чтобы выбрать вариант или пояснить её.")
        return Reply("\n".join(lines), {"inline_keyboard": keyboard})

    def accounts(self, statement, catalog, page=0):
        currencies = {r["currency"] for r in statement["rows"]}
        accounts = [a for a in catalog["accounts"] if a.get("type") != 2 and a["currency"] in currencies]
        page = max(0, min(page, max(0, (len(accounts)-1)//8)))
        keyboard = [[self.control(statement, f"{a['name'][:48]} · {a['currency']}", "account", a["id"], "primary")]
                    for a in accounts[page*8:page*8+8]]
        if page:
            keyboard.append([self.control(statement, "← Счета", "accounts", str(page-1))])
        if len(accounts) > page*8+8:
            keyboard.append([self.control(statement, "Ещё счета →", "accounts", str(page+1))])
        keyboard.append([self.control(statement, "Отменить", "cancel", style="danger")])
        return Reply(f"Прочитано {len(statement['rows'])} операций. " + (
            "На какой счёт импортировать выписку?" if accounts else "Нет счёта в валюте выписки. Добавьте его в ezBookkeeping."),
            {"inline_keyboard": keyboard})

    async def start(self, job, statement, catalog=None):
        catalog = catalog or await self.svc.ez.catalog()
        if statement.get("ready"):
            return self.view(statement)
        currencies = {r["currency"] for r in statement["rows"]}
        available = [a for a in catalog["accounts"] if a.get("type") != 2 and a["currency"] in currencies]
        contact = self.svc.s.contacts[job["user_id"]]
        preferred = statement.get("account_id") or contact.account or self.svc.s.default_account
        account = next((a for a in available if a["id"] == preferred), None)
        if not account and len(available) == 1:
            account = available[0]
        if not account:
            bank_words = ("т-банк", "тбанк", "тинькофф", "tbank") if statement["bank"] == "tbank" else ("яндекс", "yandex")
            matching = [a for a in available if any(w in a["name"].lower() for w in bank_words)]
            account = matching[0] if len(matching) == 1 else None
        if not account:
            return self.accounts(statement, catalog)
        await self.engine.prepare(job, statement, account["id"], catalog)
        return self.view(statement)

    def row_view(self, statement, row, catalog, category_page=None):
        action, reason = self.engine.decision(row)
        dt = datetime.fromisoformat(row["occurred_at"])
        text = (f"Операция {row['number']} · {dt:%d.%m.%Y %H:%M:%S}\n"
                f"{'Расход' if row['kind']=='expense' else 'Доход'}: {money(row['amount'])} {row['currency']}\n"
                f"{row['description']}\n{reason}")
        keyboard = []
        if not row.get("blocked"):
            categories = [c for c in catalog["categories"] if c["type"] == (2 if row["kind"] == "expense" else 1)
                          and str(c.get("parentId", "0")) != "0"]
            suggested = {row.get(p, {}).get("category_id") for p in ("review", "proposal")}
            categories.sort(key=lambda c: (c["id"] not in suggested, c["name"]))
            page = max(0, min(category_page or 0, max(0, (len(categories)-1)//6)))
            for category in categories[page*6:page*6+6]:
                keyboard.append([self.control(statement, category["name"], "category", f"{row['number']},{category['id']}",
                                              "primary" if category["id"] in suggested else None)])
            nav = []
            if page:
                nav.append(self.control(statement, "← Категории", "categories", f"{row['number']},{page-1}"))
            if len(categories) > page*6+6:
                nav.append(self.control(statement, "Ещё категории →", "categories", f"{row['number']},{page+1}"))
            if nav:
                keyboard.append(nav)
            if row.get("candidates"):
                text += "\nПохожие записи:"
                for candidate in row["candidates"]:
                    old = datetime.fromisoformat(candidate["occurred_at"])
                    text += f"\n{old:%d.%m %H:%M} · {candidate['description']}"
                text += "\nПри сомнении оставлю строку для ручной проверки."
            text += "\nВыберите категорию или напишите пояснение обычным сообщением."
        keyboard += [[self.control(statement, "Пропустить эту строку", "skip", str(row["number"]))],
                     [self.control(statement, "Следующая спорная", "questions"), self.control(statement, "К плану", "page", "1")]]
        return Reply(text, {"inline_keyboard": keyboard})

    async def callback(self, job, data):
        context = self.store.context(job["user_id"]) or {}
        statement = context.get("statement")
        if not statement:
            return "Этот импорт уже завершён или отменён. Пришлите новую выписку."
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "s" or parts[1] != statement["token"]:
            return self.view(statement, notice="План обновился. Используйте актуальные кнопки ниже.")
        action, argument = parts[2:]
        catalog = await self.svc.ez.catalog()
        if action == "cancel":
            self.store.save_context(job["user_id"], None)
            return "Импорт отменён. Можно прислать другую выписку или отдельную покупку."
        if action == "confirm":
            result = await self.engine.confirm(job, statement, catalog)
            current = (self.store.context(job["user_id"]) or {}).get("statement")
            return self.view(current) if current else result
        if action == "auto":
            return await self.start(job, statement, catalog)
        if action == "accounts":
            return self.accounts(statement, catalog, int(argument or 0))
        if action in ("account", "refresh"):
            account_id = argument if action == "account" else statement.get("account_id", "")
            if not account_id:
                return self.accounts(statement, catalog)
            await self.engine.prepare(job, statement, account_id, catalog, refresh=True)
            return self.view(statement)
        if action == "page":
            statement.pop("ui_row", None)
            self.engine.save(job["user_id"], statement)
            return self.view(statement, int(argument or 1))
        if action == "questions":
            unresolved = [r for r in statement["rows"] if self.engine.decision(r)[0] == "review"]
            after = statement.get("ui_row", 0)
            row = next((r for r in unresolved if r["number"] > after), unresolved[0] if unresolved else None)
            if not row:
                return self.view(statement, notice="Спорных строк больше нет.")
        elif action in ("row", "categories", "category", "skip"):
            pieces = argument.split(",")
            if not pieces[0].isdigit() or not 1 <= int(pieces[0]) <= len(statement["rows"]):
                raise UserInputError("Не удалось найти строку выписки.")
            row = statement["rows"][int(pieces[0])-1]
            if action == "category":
                if len(pieces) != 2:
                    raise UserInputError("Не удалось определить категорию.")
                self.engine.category(row, pieces[1], catalog)
                if row.get("blocked"):
                    return self.row_view(statement, row, catalog)
                row.update(manual_category_id=pieces[1], excluded=False)
                row.pop("proposal", None)
                row.pop("review", None)
                statement["ready"] = False
                self.engine.save(job["user_id"], statement)
                await self.engine.prepare(job, statement, statement["account_id"], catalog)
            elif action == "skip":
                row["excluded"] = True
                statement["token"] = secrets.token_hex(4)
                self.engine.save(job["user_id"], statement)
                return self.view(statement, (row["number"]-1)//self.PAGE_SIZE+1)
            elif action == "categories":
                return self.row_view(statement, row, catalog, int(pieces[1]))
        else:
            return self.view(statement)
        statement["ui_row"] = row["number"]
        self.engine.save(job["user_id"], statement)
        return self.row_view(statement, row, catalog)

    async def text(self, job, text):
        statement = self.engine.current(job["user_id"])
        if statement.get("ui_row") and text.strip():
            row = statement["rows"][statement["ui_row"]-1]
            if not row.get("blocked"):
                row["user_note"] = text[:1000]
                row.pop("proposal", None)
                row.pop("review", None)
                statement["ready"] = False
                self.engine.save(job["user_id"], statement)
                catalog = await self.svc.ez.catalog()
                await self.engine.prepare(job, statement, statement["account_id"], catalog)
                return self.row_view(statement, row, catalog)
        return await self.start(job, statement)
