"""Bounded, user-scoped category preferences; never arbitrary model instructions."""

import re
import unicodedata

from .ezbookkeeping import UserInputError


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split()).strip(' «»"\'.,:;!?')


def active_rules(store, user, catalog):
    categories = {c["id"]: c for c in catalog["categories"]}
    result = []
    for rule in store.memories(user):
        category = categories.get(rule["category_id"])
        if category and category.get("parentId") not in (None, "", "0", 0) and category["type"] == (1 if rule["kind"] == "income" else 2):
            result.append({"merchant": rule["merchant"], "kind": rule["kind"],
                           "category_id": rule["category_id"], "category_name": category["name"]})
    return result


def matching_rules(description, kind, rules):
    """Match whole merchant words; conflicting explicit preferences stay unresolved."""
    words = " " + " ".join(re.findall(r"\w+", normalize(description))) + " "
    return [r for r in rules if r["kind"] == kind
            and (merchant := " ".join(re.findall(r"\w+", normalize(r["merchant"]))))
            and " " + merchant + " " in words]


def apply_changes(store, user, changes, catalog, source):
    """Caller owns the transaction including the frozen response/financial plan."""
    existing = {(r["kind"], r["merchant"]) for r in store.memories(user)}
    categories = {c["id"]: c for c in catalog["categories"]}
    lines = []
    for change in changes:
        merchant = normalize(change.merchant)
        evidence = normalize(change.evidence)
        if len(merchant) < 2 or evidence not in normalize(source) or merchant not in evidence:
            raise UserInputError("Напишите правило явно, например: «Запомни: расходы в Самокате — доставка еды».")
        key = (change.kind, merchant)
        if change.operation == "forget":
            store.forget_merchant(user, *key)
            existing.discard(key)
            lines.append(f"Правило удалено: {merchant} ({'доход' if change.kind == 'income' else 'расход'}).")
            continue
        category = categories.get(change.category_id)
        if (not category or category.get("parentId") in (None, "", "0", 0)
                or category["type"] != (1 if change.kind == "income" else 2)):
            raise UserInputError("Для правила нужна существующая подкатегория нужного типа. Посмотрите /categories и уточните название.")
        existing.add(key)
        if len(existing) > 100:
            raise UserInputError("В памяти уже 100 правил. Удалите ненужные через /memory и /forget ID.")
        store.remember(user, *key, category["id"], category["name"])
        lines.append(f"Запомнил: {merchant} ({'доход' if change.kind == 'income' else 'расход'}) → {category['name']}.")
    return "\n".join(lines)
