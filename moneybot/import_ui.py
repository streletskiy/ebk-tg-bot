"""Persistent, private-chat controls for bank imports (Telegram Bot API 10.3)."""

import secrets
from collections import Counter
from datetime import datetime

from .ezbookkeeping import UserInputError
from .statements import transfer_details
from .telegram import Reply, button, card_reply


def clean(value, limit=180):
    return " ".join(str(value).split())[:limit]


def amount(value):
    return f"{value // 100:,}".replace(",", " ") + f",{value % 100:02d}"


def transfer_summary(statement, row):
    details = row.get("transfer") or transfer_details(row["description"], statement["bank"], row["kind"])
    if not details:
        return ""
    party = "Получатель" if row["kind"] == "expense" else "Отправитель"
    lines = [f"{party}: {clean(details.get('party') or 'не указан в выписке')}",
             f"Банк: {clean(details.get('bank') or 'не указан в выписке')}"]
    if details.get("reference"):
        lines.append(clean(details["reference"]))
    return "\n".join(lines)


class StatementUI:
    PAGE_SIZE = 5

    def __init__(self, service):
        self.svc, self.engine, self.store = service, service.imports, service.store

    @staticmethod
    def control(statement, text, action, argument="", style=None):
        return button(text, f"s:{statement['token']}:{action}:{argument}", style)

    def unresolved(self, statement):
        return sorted((r for r in statement["rows"] if self.engine.decision(r)[0] == "review"), key=lambda r: r["number"])

    def current_view(self, statement, notice=""):
        number = statement.get("ui_row", 0)
        if 1 <= number <= len(statement["rows"]) and statement.get("ui_catalog"):
            return self.row_view(statement, statement["rows"][number-1], statement["ui_catalog"],
                                 statement.get("ui_category_page", 0), notice)
        return self.view(statement, statement.get("ui_page", 1), notice)

    def open_row(self, job, statement, row, catalog, category_page=0, notice=""):
        statement.update(ui_row=row["number"], ui_category_page=category_page)
        self.engine.save(job["user_id"], statement)
        return self.row_view(statement, row, catalog, category_page, notice)

    def open_plan(self, job, statement, page=1, notice=""):
        statement.pop("ui_row", None)
        statement.pop("ui_category_page", None)
        statement["ui_page"] = max(1, min(page, (len(statement["rows"])+self.PAGE_SIZE-1)//self.PAGE_SIZE))
        self.engine.save(job["user_id"], statement)
        return self.view(statement, statement["ui_page"], notice)

    def view(self, statement, page=1, notice=""):
        rows = statement["rows"]
        pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(1, min(page, pages))
        counts = Counter(self.engine.decision(r)[0] for r in rows)
        lines = [notice] if notice else []
        bank = "Т-Банк" if statement["bank"] == "tbank" else "Яндекс Банк"
        lines += [f"{bank} · {len(rows)} операций\nСчёт: {clean(statement.get('account_name', 'ещё не выбран'), 100)}"]
        if statement.get("verification", {}).get("totals_verified"):
            lines.append("Количество строк и итоговые суммы PDF проверены.")
        if not statement.get("ready"):
            lines.append("Продолжу подбор категорий и проверку дублей автоматически.")
            return Reply("\n".join(lines), {"inline_keyboard": [
                [self.control(statement, "Продолжить проверку", "auto", style="primary")],
                [self.control(statement, "Отменить", "cancel", style="danger")]]})
        lines += [f"План импорта\n＋ Добавить: {counts['new']} · ◷ Уточнить время: {counts['time']}\n"
                  f"↷ Пропустить: {counts['skip']} · ? Уточнить: {counts['review']}"]
        for kind, label in (("expense", "Расходы"), ("income", "Доходы")):
            total = sum(r["amount"] for r in rows if r["kind"] == kind and self.engine.decision(r)[0] == "new")
            if total:
                lines.append(f"{label} к добавлению: {amount(total)} {statement['currency']}")
        lines.append(f"Операции · страница {page}/{pages}")
        keyboard = []
        for row in rows[(page-1)*self.PAGE_SIZE:page*self.PAGE_SIZE]:
            action, _ = self.engine.decision(row)
            label = {"new": "добавить", "time": "уточнить время", "skip": "пропустить", "review": "уточнить"}[action]
            dt = datetime.fromisoformat(row["occurred_at"])
            lines.append(f"№ {row['number']} · {'−' if row['kind']=='expense' else '+'}{amount(row['amount'])} {row['currency']}\n"
                         f"{dt:%d.%m.%Y · %H:%M} · {label}\n{clean(row['description'])}"
                         + ("\n" + details if (details := transfer_summary(statement, row)) else "")
                         + ("\nКатегория: " + clean(row["category_name"], 100) if row.get("category_name") and action == "new" else ""))
            keyboard.append([self.control(statement, f"{row['number']}. {row['description'][:45]}", "row", str(row["number"]))])
        navigation = []
        if page > 1:
            navigation.append(self.control(statement, "←", "page", str(page-1)))
        navigation.append(button(f"{page}/{pages}"))
        if page < pages:
            navigation.append(self.control(statement, "→", "page", str(page+1)))
        keyboard.append(navigation)
        if counts["review"]:
            first = self.unresolved(statement)[0]["number"]
            keyboard.append([self.control(statement, f"Разобрать спорные · {counts['review']} · с № {first}", "review", str(first), "primary")])
        if counts["new"] or counts["time"]:
            keyboard.append([self.control(statement, f"Подтвердить: добавить {counts['new']}, время {counts['time']}",
                                          "confirm", style="success")])
        keyboard += [[self.control(statement, "Другой счёт", "accounts"), self.control(statement, "Обновить сверку", "refresh")],
                     [self.control(statement, "Отменить импорт", "cancel", style="danger")]]
        lines.append("Спорные строки не записываются. Откройте строку, чтобы выбрать вариант или пояснить её.")
        return card_reply(lines, keyboard)

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
        if statement.get("ready"):
            return self.current_view(statement)
        catalog = catalog or await self.svc.ez.catalog(fresh=False)
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

    def row_view(self, statement, row, catalog, category_page=None, notice=""):
        action, reason = self.engine.decision(row)
        dt = datetime.fromisoformat(row["occurred_at"])
        label = {"new": "Готово к добавлению", "time": "Уточнить время", "skip": "Будет пропущено", "review": "Нужно ваше решение"}[action]
        sections = [notice, f"Операция № {row['number']} из {len(statement['rows'])}\n{dt:%d.%m.%Y · %H:%M:%S}",
                    f"{'−' if row['kind']=='expense' else '+'}{amount(row['amount'])} {row['currency']}\n{clean(row['description'])}",
                    transfer_summary(statement, row),
                    f"{label}\n{clean(reason, 300)}"]
        if row.get("manual_category_id"):
            selected = next((c["name"] for c in catalog["categories"] if c["id"] == row["manual_category_id"]), "")
            if selected:
                sections.append("Выбрано вами\n" + clean(selected, 140))
        elif row.get("rule_category_id"):
            sections.append("Ваше правило\n" + clean(row["rule_merchant"], 100) + " → " + clean(row.get("category_name", ""), 140))
        keyboard = []
        if not row.get("blocked"):
            categories = [c for c in catalog["categories"] if c["type"] == (2 if row["kind"] == "expense" else 1)
                          and str(c.get("parentId", "0")) != "0"]
            suggested = [row.get("manual_category_id"), row.get("rule_category_id"), *row.get("category_suggestions", [])]
            for phase in ("review", "proposal"):
                choice = row.get(phase, {})
                suggested += [choice.get("category_id"), *choice.get("suggested_category_ids", [])]
            suggested = list(dict.fromkeys(c for c in suggested if c))
            rank = {c: i for i, c in enumerate(suggested)}
            categories.sort(key=lambda c: (rank.get(c["id"], len(rank)), c["name"].casefold()))
            page = max(0, min(category_page or 0, max(0, (len(categories)-1)//6)))
            for category in categories[page*6:page*6+6]:
                keyboard.append([self.control(statement, ("★ " if category['id'] in rank else "") + category["name"], "category", f"{row['number']},{category['id']}",
                                              "primary" if category["id"] in suggested else None)])
            nav = []
            if page:
                nav.append(self.control(statement, "← Категории", "categories", f"{row['number']},{page-1}"))
            if len(categories) > page*6+6:
                nav.append(self.control(statement, "Ещё категории →", "categories", f"{row['number']},{page+1}"))
            if nav:
                keyboard.append(nav)
            if row.get("candidates"):
                matches = ["Похожие записи в учёте"]
                for candidate in row["candidates"]:
                    old = datetime.fromisoformat(candidate["occurred_at"])
                    matches.append(f"• {old:%d.%m · %H:%M} — {clean(candidate['description'])}")
                matches.append("Выбор категории не снимает проверку дублей.")
                sections.append("\n".join(matches))
            sections.append(f"Категории · {page+1}/{max(1, (len(categories)+5)//6)}\n"
                            "★ — подходящие варианты в порядке рекомендации.\nВыберите кнопку или напишите пояснение.")
        unresolved = self.unresolved(statement)
        previous = next((r for r in reversed(unresolved) if r["number"] < row["number"]), None)
        following = next((r for r in unresolved if r["number"] > row["number"]), None)
        if action == "review":
            position = next(i for i, r in enumerate(unresolved, 1) if r["number"] == row["number"])
            sections.append(f"Спорная {position} из {len(unresolved)}\nПереходы идут по номерам строк выписки.")
        elif unresolved:
            sections.append(f"Осталось спорных: {len(unresolved)}")
        if not following:
            sections.append("Конец списка спорных строк\nДальше спорных операций нет. Вернуться к предыдущим можно кнопкой ниже.")
        navigation = []
        if previous:
            navigation.append(self.control(statement, f"← Спорная № {previous['number']}", "review", str(previous["number"])))
        if following:
            navigation.append(self.control(statement, f"Следующая · № {following['number']} →", "review", str(following["number"])))
        if navigation:
            keyboard.append(navigation)
        keyboard += [[self.control(statement, "Пропустить эту строку", "skip", str(row["number"]))],
                     [self.control(statement, "К плану", "page", str(statement.get("ui_page", (row["number"]-1)//self.PAGE_SIZE+1)))]]
        return card_reply(sections, keyboard)

    async def callback(self, job, data):
        context = self.store.context(job["user_id"]) or {}
        statement = context.get("statement")
        if not statement:
            return "Этот импорт уже завершён или отменён. Пришлите новую выписку."
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "s" or parts[1] != statement["token"]:
            return self.current_view(statement, notice="План обновился. Используйте актуальные кнопки ниже.")
        action, argument = parts[2:]
        if action == "cancel":
            self.store.save_context(job["user_id"], None)
            return "Импорт отменён. Можно прислать другую выписку или отдельную покупку."
        if action == "page":
            return self.open_plan(job, statement, int(argument or 1))
        catalog = statement.get("ui_catalog")
        if not catalog or action in ("confirm", "account", "refresh"):
            catalog = await self.svc.ez.catalog(fresh=action in ("confirm", "account", "refresh"))
            self.engine.cache_catalog(statement, catalog)
            self.engine.save(job["user_id"], statement)
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
        if action == "questions":
            return self.current_view(statement, notice="Навигация обновлена. Выберите кнопку с номером строки.")
        elif action in ("row", "review", "categories", "category", "skip"):
            pieces = argument.split(",")
            if not pieces[0].isdigit() or not 1 <= int(pieces[0]) <= len(statement["rows"]):
                raise UserInputError("Не удалось найти строку выписки.")
            row = statement["rows"][int(pieces[0])-1]
            if action == "category":
                if len(pieces) != 2:
                    raise UserInputError("Не удалось определить категорию.")
                self.engine.category(row, pieces[1], catalog)
                if row.get("blocked"):
                    return self.open_row(job, statement, row, catalog)
                row.update(manual_category_id=pieces[1], category_name=self.engine.category(row, pieces[1], catalog)["name"], excluded=False)
                statement["token"] = secrets.token_hex(4)
            elif action == "skip":
                row["excluded"] = True
                statement["token"] = secrets.token_hex(4)
                following = next((r for r in self.unresolved(statement) if r["number"] > row["number"]), None)
                notice = f"Строка № {row['number']} пропущена."
                if following:
                    return self.open_row(job, statement, following, catalog, notice=notice)
                return self.open_plan(job, statement, (row["number"]-1)//self.PAGE_SIZE+1,
                                      notice + " Дальше спорных строк нет.")
            elif action == "categories":
                return self.open_row(job, statement, row, catalog, int(pieces[1]))
        else:
            return self.current_view(statement)
        return self.open_row(job, statement, row, catalog)

    async def text(self, job, text):
        statement = self.engine.current(job["user_id"])
        if statement.get("ui_row") and text.strip():
            row = statement["rows"][statement["ui_row"]-1]
            if not row.get("blocked"):
                row["user_note"] = text[:1000]
                row.pop("proposal", None)
                row.pop("review", None)
                row.pop("category_suggestions", None)
                statement["ready"] = False
                self.engine.save(job["user_id"], statement)
                catalog = await self.svc.ez.catalog()
                await self.engine.prepare(job, statement, statement["account_id"], catalog)
                return self.row_view(statement, row, catalog)
        return await self.start(job, statement)
