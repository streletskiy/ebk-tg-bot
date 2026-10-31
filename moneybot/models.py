from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entry(StrictModel):
    kind: Literal["expense", "income"]
    amount: str
    currency: str
    account_id: str
    category_id: str
    occurred_at: str
    description: str = Field(max_length=180)


class Query(StrictModel):
    start: str
    end: str
    kind: Literal["all", "expense", "income"]
    account_ids: list[str]
    category_ids: list[str]
    keyword: str
    view: Literal["summary", "transactions", "balances"]
    group_by: Literal["category", "month", "day", "account", "merchant"]


class Decision(StrictModel):
    action: Literal["record", "query", "clarify", "help"]
    entries: list[Entry] = Field(max_length=10)
    query: Query | None
    message: str = Field(max_length=3000)


def cents(value: str) -> int:
    try:
        number = Decimal(value.replace(",", "."))
        if not number.is_finite() or number <= 0 or number > Decimal("9999999999.99"):
            raise ValueError("Amount is outside supported range")
        if number * 100 != (number * 100).to_integral_value():
            raise ValueError("Use at most two decimal places")
        return int(number * 100)
    except InvalidOperation as exc:
        raise ValueError("Invalid amount") from exc


def local_datetime(value, tz):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def money(value):
    return f"{Decimal(int(value)) / 100:,.2f}".replace(",", " ")
