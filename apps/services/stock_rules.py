"""
The pure rules behind RM stock entry (2026-09-29): stock units, a lot's
rate, voucher numbers and the day-by-day balance check every posting runs.
No Django imports, like procurement_rules.py, so each rule is tested on its
own and migration 0082 can use the same unit and rate rules when it turns the
MIRs posted before stock existed into lots.

BASE UNITS (2026-09-30, project owner: "keep the base units as KG, L, Nos,
m"). Every material has a base unit on the material master - KG for solids, L
for liquids, NOS for pieces, M for length - and a MIR receipt is held in it,
converted exactly when the MIR line's unit is of the same kind (1 MT = 1000
KG, 1 ML = 0.001 L, 1 MM = 0.001 M - EXACT below), or by a factor entered for
that material when it is a pack unit (1 ROLL = 660 M, 1 SET = 2 NOS; nothing
general converts those). A unit with neither stays as the MIR line had it.
The lot records the factor it was converted by, so the MIR's own figure can
always be shown beside it. stock_unit() is the first rule (weight held in KG)
- kept only because migration 0082 made the first lots with it.

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


BASE_UNITS = ("KG", "L", "NOS", "M")

# unit -> (base unit, how many base units one of it is). Exact only.
EXACT = {
    "KG": ("KG", Decimal("1")), "MT": ("KG", Decimal("1000")), "G": ("KG", Decimal("0.001")),
    "L": ("L", Decimal("1")), "ML": ("L", Decimal("0.001")), "KL": ("L", Decimal("1000")),
    "NOS": ("NOS", Decimal("1")),
    "M": ("M", Decimal("1")), "CM": ("M", Decimal("0.01")), "MM": ("M", Decimal("0.001")),
}


def base_of(uom: str) -> str:
    """The base unit a unit converts into exactly (KG for MT), or "" for a
    pack or unknown unit (ROLL, SET, M2, BQ2)."""
    return EXACT.get((uom or "").strip().upper(), ("", None))[0]


def to_base(uom: str, base: str, factors: dict) -> tuple[str, Decimal]:
    """(unit a receipt is held in, factor) for a MIR line in `uom` of a
    material whose base unit is `base` and whose entered pack factors are
    `factors` {unit: base units per one}. Exact first, then the material's
    factor; otherwise the MIR line's own unit, factor 1."""
    code = (uom or "").strip().upper()
    if base:
        exact = EXACT.get(code)
        if exact and exact[0] == base:
            return base, exact[1]
        if factors.get(code):
            return base, Decimal(factors[code])
    return code, Decimal("1")


def stock_unit(uom: str) -> tuple[str, Decimal]:
    """(stock unit, factor) as migration 0082 made its lots: weight held in
    KG, every other unit as it is. Not used for new lots - see BASE UNITS
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
