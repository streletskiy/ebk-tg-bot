"""Read-only ledger analytics in integer hundredths, separated by currency and direction."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .models import money
from .payments import has_merchant_words, merchant_words


KINDS = {"income": 2, "expense": 3}
DIRECTIONS = {2: "Доходы", 3: "Расходы"}
GROUPS = {"category": "категории", "category_family": "разделы", "year": "годы", "month": "месяцы",
          "week": "недели", "day": "дни", "account": "счета", "merchant": "магазины / описания"}
TIME_GROUPS = {"year", "month", "week", "day"}
METRICS = {"total": "Сумма", "count": "Операций", "average": "Среднее", "minimum": "Минимум",
           "maximum": "Максимум"}
EXCLUSIONS = "Переводы и корректировки баланса исключены."


@dataclass
class ReportRow:
    time: datetime
    currency: str
    kind: int
    amount: int
    category: str
    account: str
    merchant: str
    comment: str

    def group(self, dimension):
        if dimension == "category_family":
            return self.category.split(" / ", 1)[0]
        if dimension == "week":
            return (self.time.date() - timedelta(days=self.time.weekday())).isoformat()
        if dimension in TIME_GROUPS:
            return self.time.strftime({"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}[dimension])
        return getattr(self, dimension)


def label(value):
    return " ".join(value.split())


def report_rows(items, query, catalog, start, end, minimum, maximum):
    accounts = {str(a["id"]): a for a in catalog["accounts"]}
    categories = {str(c["id"]): c for c in catalog["categories"]}
    included_accounts, included_categories = set(query.account_ids), set(query.category_ids)
    excluded_categories = set(query.exclude_category_ids)
    matches = [(label(match.label), [merchant_words(term) for term in match.terms]) for match in query.matches]
    start_ts, end_ts = start.timestamp(), end.timestamp()
    for item in items:
        kind = item["type"]
        if kind not in DIRECTIONS or (query.kind != "all" and kind != KINDS[query.kind]):
            continue
        if not start_ts <= item["time"] < end_ts:
            continue
        account = item.get("sourceAccount") or {}
        account_id = str(item.get("sourceAccountId") or (account.get("id") if isinstance(account, dict) else account))
        if included_accounts and account_id not in included_accounts:
            continue
        category = item.get("category") or {}
        category_id = str(item.get("categoryId") or (category.get("id") if isinstance(category, dict) else category))
        if category_id in excluded_categories or (included_categories and category_id not in included_categories):
            continue
        amount = int(item["sourceAmount"])
        if (minimum is not None and amount < minimum) or (maximum is not None and amount > maximum):
            continue
        comment = label((item.get("comment") or "").split("\n[moneybot:", 1)[0])
        merchant = comment[:120] or "Без описания"
        if matches:
            words = merchant_words(comment)
            merchant = next((name for name, terms in matches if any(
                has_merchant_words(words, term) for term in terms)), None)
            if merchant is None:
                continue
        account = accounts.get(account_id, account if isinstance(account, dict) else {})
        currency = account.get("currency")
        if not currency:
            raise ValueError("EZ_UNKNOWN_CURRENCY")
        if query.currency and currency != query.currency.upper():
            continue
        category = categories.get(category_id, category if isinstance(category, dict) else {})
        yield ReportRow(datetime.fromtimestamp(item["time"], start.tzinfo), currency, kind, amount,
                        label(category.get("name") or "Без категории"), label(account.get("name") or "Счёт"),
                        merchant, comment)


def period_label(start, end):
    if start.time() == end.time() == datetime.min.time():
        return f"{start:%d.%m.%Y} — {(end - timedelta(days=1)):%d.%m.%Y}"
    return f"{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M} (до указанного времени)"


def metric_text(amounts, metrics, currency, *, compact=False):
    values = []
    for metric in metrics:
        if metric == "count":
            value = str(len(amounts))
        elif metric == "total":
            value = f"{money(sum(amounts))} {currency}"
        elif not amounts:
            value = "—"
        else:
            if metric == "average":
                result = (Decimal(sum(amounts)) / len(amounts)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            else:
                result = (min if metric == "minimum" else max)(amounts)
            value = f"{money(result)} {currency}"
        values.append(value if compact and metrics == ["total"] else f"{METRICS[metric]}: {value}")
    return " · ".join(values)


def format_report(periods, query, catalog):
    metrics = list(dict.fromkeys(query.metrics)) or ["total"]
    dimensions = list(dict.fromkeys(d for d in (query.group_by, query.then_by) if d))
    sections, totals = [], []
    limit = query.limit or 30
    for start, end, rows in periods:
        heading = f"{period_label(start, end)} ({start.tzinfo})"
        by_kind = defaultdict(list)
        totals.append(by_kind)
        if not rows:
            sections.append(heading + "\nЗаписей по запросу нет.")
            continue
        for row in rows:
            by_kind[row.currency, row.kind].append(row.amount)
        lines = [heading]
        for (currency, kind), amounts in sorted(by_kind.items()):
            lines.append(f"{DIRECTIONS[kind]} · {currency} — {metric_text(amounts, metrics, currency)}")
        if query.kind == "all" and "total" in metrics and not query.category_ids and not query.matches:
            for currency in sorted({row.currency for row in rows}):
                net = sum(by_kind.get((currency, 2), ())) - sum(by_kind.get((currency, 3), ()))
                lines.append(f"Доходы минус расходы · {currency} — {money(net)} {currency}")
        if query.view == "transactions":
            lines.extend(transaction_lines(rows, query.order, limit))
        elif dimensions:
            lines.extend(group_lines(rows, dimensions, metrics, query.order, limit))
            if query.matches and "merchant" in dimensions:
                missing = dict.fromkeys(label(match.label) for match in query.matches)
                for row in rows:
                    missing.pop(row.merchant, None)
                if missing:
                    lines.append("Без совпадений: " + ", ".join(missing))
        sections.append("\n".join(lines))
    if len(periods) > 1 and any(totals):
        sections.extend(comparison_lines(periods, totals, metrics))
    filters = []
    for ids, items, title in ((query.category_ids, catalog["categories"], "Категории"),
                              (query.exclude_category_ids, catalog["categories"], "Исключены категории"),
                              (query.account_ids, catalog["accounts"], "Счета")):
        if ids:
            selected = set(ids)
            names = [label(item["name"]) for item in items if str(item["id"]) in selected]
            filters.append(f"{title}: {', '.join(names)}")
    if query.matches:
        filters.append("По описаниям операций: " + ", ".join(label(match.label) for match in query.matches))
    if query.currency:
        filters.append("Валюта: " + query.currency.upper())
    if query.min_amount or query.max_amount:
        bounds = " ".join(value for value in (f"от {query.min_amount}" if query.min_amount else "",
                                               f"до {query.max_amount}" if query.max_amount else "") if value)
        filters.append("Сумма операции: " + bounds)
    sections.append("\n".join([*filters, EXCLUSIONS]))
    return "\n\n".join(sections)


def transaction_lines(rows, order, limit):
    if order in ("largest", "smallest"):
        # Rank within each currency; an amount in one currency cannot outrank another currency.
        ranked = defaultdict(list)
        for row in rows:
            ranked[row.currency].append(row)
        ordered = []
        for _, items in sorted(ranked.items()):
            ordered.extend(sorted(items, key=lambda row: row.amount, reverse=order == "largest")[:limit])
    else:
        ordered = sorted(rows, key=lambda row: row.time, reverse=order != "oldest")[:limit]
    lines = ["", f"Операции · показано {len(ordered)} из {len(rows)}"]
    for row in ordered:
        sign = "−" if row.kind == 3 else "+"
        lines.append(f"{row.time:%d.%m.%Y} · {sign}{money(row.amount)} {row.currency} · {row.category}"
                     + (f" · {row.comment[:140]}" if row.comment else ""))
    return lines


def group_lines(rows, dimensions, metrics, order, limit):
    groups = defaultdict(lambda: defaultdict(list))
    for row in rows:
        groups[row.currency, row.kind][tuple(row.group(dimension) for dimension in dimensions)].append(row.amount)
    lines = ["", "Разбивка · " + " / ".join(GROUPS[dimension] for dimension in dimensions)]
    for (currency, kind), buckets in sorted(groups.items()):
        lines.append(f"{DIRECTIONS[kind]} · {currency}")
        if dimensions[0] in TIME_GROUPS and order in ("default", "oldest", "newest"):
            ordered = sorted(buckets.items(), reverse=order == "newest")
        else:
            metric = metrics[0]
            def rank(amounts):
                if order in ("largest", "smallest") or metric == "total":
                    return sum(amounts)
                if metric == "count":
                    return len(amounts)
                if metric == "average":
                    return Decimal(sum(amounts)) / len(amounts)
                return (min if metric == "minimum" else max)(amounts)
            direction = 1 if order == "smallest" else -1
            ordered = sorted(buckets.items(), key=lambda item: (direction * rank(item[1]), item[0]))
        for names, amounts in ordered[:limit]:
            lines.append(f"{' · '.join(names)} — {metric_text(amounts, metrics, currency, compact=True)}")
        if len(ordered) > limit:
            lines.append(f"Показано {limit} из {len(ordered)} групп.")
    return lines


def comparison_lines(periods, totals, metrics):
    lines = []
    start, end, _ = periods[0]
    for (other_start, other_end, _), baseline in zip(periods[1:], totals[1:], strict=True):
        comparison = [f"Сравнение · {period_label(start, end)} к {period_label(other_start, other_end)}"]
        for currency, kind in sorted(totals[0].keys() | baseline.keys()):
            current = totals[0].get((currency, kind), ())
            previous = baseline.get((currency, kind), ())
            changes = []
            if "total" in metrics:
                before = sum(previous)
                change = sum(current) - before
                percent = f" ({Decimal(change) * 100 / abs(before):+.1f}%)" if before else ""
                changes.append(f"сумма {('+' if change > 0 else '')}{money(change)} {currency}{percent}")
            if "count" in metrics:
                changes.append(f"операций {len(current) - len(previous):+d}")
            if changes:
                comparison.append(f"{DIRECTIONS[kind]} · {currency} — " + " · ".join(changes))
        if len(comparison) > 1:
            lines.append("\n".join(comparison))
    return lines
