from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

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


class PaymentEntry(Entry):
    merchant: str = Field(max_length=180, description=(
        "Merchant/payee name visible in the source, without purchase details; empty if unknown."))
    source_image: int = Field(ge=0, le=5, description="1-based source image index; 0 for text or voice")
    time_precision: Literal["second", "minute", "day", "unknown"] = Field(
        description="Precision visible in the source; day/unknown when an image does not show a time")
    payment_reference: str = Field(max_length=160, description=(
        "Unique bank transaction/reference ID copied from the payment, otherwise empty. "
        "Never a card number, phone, account number, short authorization code or invented identifier."))


class Query(StrictModel):
    start: str
    end: str
    kind: Literal["all", "expense", "income"]
    account_ids: list[str]
    category_ids: list[str]
    keyword: str
    view: Literal["summary", "transactions", "balances"]
    group_by: Literal["category", "month", "day", "account", "merchant"]


class MemoryChange(StrictModel):
    operation: Literal["remember", "forget"]
    kind: Literal["expense", "income"]
    merchant: str = Field(min_length=2, max_length=100)
    category_id: str
    evidence: str = Field(min_length=3, max_length=1000)


class Decision(StrictModel):
    action: Literal["record", "query", "clarify", "help", "memory", "ignore"] = Field(description=(
        "Completed payment screenshots, receipts and descriptions of expenses mean record by default. "
        "Clarify only missing essential data; never ask permission to record. "
        "Ignore transfers unless the user explicitly asks about them; exclude them from mixed payment lists."))
    entries: list[PaymentEntry] = Field(max_length=10)
    query: Query | None
    message: str = Field(max_length=3000)
    reply_options: list[Annotated[str, Field(min_length=1, max_length=48)]] = Field(
        max_length=3, description=(
            "For clarify with 2 or 3 short answers, provide every answer as a button label, including Yes/No. "
            "For open-ended questions or other actions return []. Free text is always accepted."))
    memory_changes: list[MemoryChange] = Field(max_length=10)


class VisiblePayment(StrictModel):
    source_image: int = Field(ge=1, le=5)
    kind: Literal["expense", "income"]
    amount: str
    currency: str


class ImagePaymentReview(StrictModel):
    visible_payments: list[VisiblePayment] = Field(max_length=100, description=(
        "Inventory of every requested completed purchase/income row, top to bottom in each image. "
        "Keep separate rows even with identical merchants and amounts. Exclude transfers and pending payments. "
        "One receipt total is one payment; its item lines are not payments."))
    decision: Decision


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
