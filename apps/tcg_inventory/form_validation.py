"""Boundary validation for the money/type form inputs (issue #228, part a).

Every route that writes a transaction type, a price/amount, or a date
parses it through these helpers instead of trusting FastAPI's own `float`
coercion -- which happily accepts "nan"/"inf" (one NaN turns every money
figure into "nan kr") and answers an unparseable value with a JSON 422
that htmx would show raw, or not at all.

A rejected value raises `FormError`. app.py turns that into a 422 with a
short plain-text message naming the field; static/form-errors.js puts the
message into the submitting form's `[data-form-error]` slot without
swapping anything, so the user's input stays as typed. The plain (non-htmx)
listing forms catch `FormError` themselves and re-render with the message.
"""

from __future__ import annotations

import datetime as dt
import math

from models import TRANSACTION_TYPES

# Readable names for the form fields FastAPI itself may still reject (e.g.
# a non-integer Order ID), for the plain-text RequestValidationError reply.
FIELD_LABELS = {
    "card_id": "Card",
    "purchase_id": "Order ID",
    "new_purchase_id": "Order ID",
    "tx_id": "Transaction",
    "delete_tx_id": "Transaction",
    "qty": "Quantity",
    "date": "Date",
    "type": "Type",
    "price": "Price",
    "fees": "Fees",
    "purchase_total": "Total",
    "purchase_shipping": "Shipping",
}


class FormError(ValueError):
    """A submitted form value failed validation; `str(exc)` is the message
    shown to the user."""


def _row(label: str, row: int | None) -> str:
    return f"{label} on row {row}" if row is not None else label


def parse_tx_type(value: str | None, row: int | None = None, allowed=TRANSACTION_TYPES) -> str:
    value = (value or "").strip()
    if value not in allowed:
        shown = f" (got '{value}')" if value else ""
        raise FormError(f"{_row('Type', row)} must be one of: {', '.join(allowed)}{shown}.")
    return value


def parse_amount(
    raw: str | float | None, label: str = "Price", row: int | None = None, required: bool = True
) -> float | None:
    """A finite, non-negative number. Accepts a decimal comma. Blank is
    None when `required` is False, else rejected."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        if required:
            raise FormError(f"{_row(label, row)} is required.")
        return None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or value < 0:
        what = "a number of 0 or more" if required else "blank or a number of 0 or more"
        raise FormError(f"{_row(label, row)} must be {what}.")
    return value


def parse_optional_amount(raw: str | float | None, label: str, row: int | None = None) -> float | None:
    return parse_amount(raw, label, row, required=False)


def parse_date(raw: str | None, label: str = "Date", row: int | None = None) -> dt.date:
    try:
        return dt.date.fromisoformat((raw or "").strip())
    except ValueError:
        raise FormError(f"{_row(label, row)} must be a date (YYYY-MM-DD).") from None


def require_same_length(**lists: list) -> int:
    """All parallel form lists must line up row for row; returns the length."""
    lengths = {name: len(values) for name, values in lists.items()}
    if len(set(lengths.values())) > 1:
        detail = ", ".join(f"{n} {name}" for name, n in lengths.items())
        raise FormError(
            f"The form's rows don't line up ({detail}) -- nothing was saved. Reload the page and try again."
        )
    return next(iter(lengths.values()), 0)


def describe_request_validation_error(errors: list[dict]) -> str:
    """A one-line plain-text version of FastAPI's RequestValidationError
    detail, naming the field(s)."""
    parts = []
    for err in errors:
        loc = [p for p in err.get("loc", ()) if p not in ("body", "query", "path", "form")]
        name = next((p for p in loc if isinstance(p, str)), "")
        row = next((p for p in loc if isinstance(p, int)), None)
        label = FIELD_LABELS.get(name, name.replace("_", " ").capitalize() or "A field")
        if row is not None:
            label = f"{label} on row {row + 1}"
        if err.get("type") == "missing":
            parts.append(f"{label} is required.")
        elif err.get("type", "").startswith("int"):
            parts.append(f"{label} must be a whole number.")
        else:
            parts.append(f"{label} is not valid.")
    return " ".join(dict.fromkeys(parts)) or "The form could not be read -- nothing was saved."
