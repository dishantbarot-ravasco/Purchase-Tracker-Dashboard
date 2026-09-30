"""
The pure rules behind RM stock entry (2026-09-29): stock units, a lot's
rate, voucher numbers and the day-by-day balance check every posting runs.
No Django imports, like procurement_rules.py, so each rule is tested on its
own and migration 0082 can use the same unit and rate rules when it turns the
MIRs posted before stock existed into lots.

STOCK UNITS. A lot keeps its MIR line's own unit (2026-09-30): every issue
names the MIR receipt it comes out of, so no two receipts are ever added
together and nothing needs converting. stock_unit() below is the earlier rule
(weight held in KG, 1 MT = 1000 KG) - kept only because migration 0082 made
the first lots with it; migration 0084 turned those back into their MIR
line's unit.

THE BALANCE CHECK. Stock may never go below zero on ANY day, not only
today: a backdated issue that fits today's balance can still take a lot
negative on the day it is dated. min_running_balance() replays a lot's
dated movements and returns the lowest end-of-day balance from a given day
on; every posting, cancellation and MIR change checks that it stays >= 0.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal

QTY = Decimal("0.001")
RATE = Decimal("0.0001")
MONEY = Decimal("0.01")

# unit -> (stock unit, how many stock units one of it is)
_CONVERSIONS = {
    "KG": ("KG", Decimal("1")),
    "MT": ("KG", Decimal("1000")),
    "G": ("KG", Decimal("0.001")),
}


def stock_unit(uom: str) -> tuple[str, Decimal]:
    """(stock unit, factor) as migration 0082 made its lots: weight held in
    KG, every other unit as it is. Not used for new lots - see STOCK UNITS
    above."""
    code = (uom or "").strip().upper()
    return _CONVERSIONS.get(code, (code, Decimal("1")))


def qty(value) -> Decimal:
    return Decimal(value).quantize(QTY, rounding=ROUND_HALF_UP)


def lot_rate(taxable, qty_received, factor) -> Decimal | None:
    """The value of one stock unit of a MIR lot: the line's taxable value
    (after discount, with freight and other charges, before GST - GST is
    claimed back as input credit, so it is not the cost of the stock) over
    the quantity invoiced, in stock units. Rejected material is billed on the
    same invoice and returned, so the rate is over everything received."""
    received = Decimal(qty_received) * Decimal(factor)
    if received <= 0 or taxable is None:
        return None
    return (Decimal(taxable) / received).quantize(RATE, rounding=ROUND_HALF_UP)


def value(quantity, rate) -> Decimal:
    if rate is None:
        return Decimal("0.00")
    return (Decimal(quantity) * Decimal(rate)).quantize(MONEY, rounding=ROUND_HALF_UP)


def voucher_number(prefix: str, kind_code: str, fy: str, seq: int) -> str:
    """"HRS/ISS/26-27/0007" - the plant's MIR prefix, the document kind, the
    short financial year and a running number per plant, kind and year."""
    short = fy[2:4] + "-" + fy[5:7]
    return f"{prefix}/{kind_code}/{short}/{seq:04d}"


def min_running_balance(events, from_date: datetime.date | None = None) -> Decimal:
    """The lowest end-of-day balance of a lot on or after `from_date`, given
    its dated movements [(date, signed qty), ...]. Before its first movement a
    lot holds nothing. With no movement on or after `from_date` the answer is
    the balance carried into that day. Same-day movements net off: stock is
    counted at the end of the day, which is how the store's books count it."""
    by_day: dict = {}
    for day, delta in events:
        by_day[day] = by_day.get(day, Decimal("0")) + Decimal(delta)
    days = sorted(by_day)
    running = Decimal("0")
    lowest = None
    if from_date is not None:
        # The end of from_date itself counts even if nothing moves on it -
        # a later receipt must not hide a shortfall on that day.
        running = sum((by_day[d] for d in days if d <= from_date), Decimal("0"))
        lowest = running
        days = [d for d in days if d > from_date]
    for day in days:
        running += by_day[day]
        lowest = running if lowest is None else min(lowest, running)
    return Decimal("0") if lowest is None else lowest
