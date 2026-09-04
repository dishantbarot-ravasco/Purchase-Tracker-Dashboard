"""Shared helpers for the Drive sync management commands."""

from decimal import ROUND_HALF_UP, Decimal


def unchanged(model_cls, existing, parsed, fields: list[str]) -> bool:
    """Field-by-field equality between a saved model instance and a freshly
    parsed value, quantizing Decimal fields to the model field's own
    decimal_places first.

    Two things make a naive `getattr(existing, f) == getattr(parsed, f)`
    unreliable here: a value gets rounded to the column's decimal_places
    somewhere between Python and storage - so a source sheet value like
    8633.625 comes back from the DB as 8633.62 or 8633.63, and would look
    "changed" forever when compared against the un-rounded parsed value
    even though nothing actually changed. Quantizing both sides to the
    field's real stored precision before comparing avoids that - but the
    rounding mode matters: this uses ROUND_HALF_UP, not Python decimal's
    default ROUND_HALF_EVEN, because that's what actually happens at the
    DB layer here. Confirmed directly against this project's real Postgres
    (`SELECT CAST('353057.445' AS numeric(16,2))` returns 353057.45, not
    Python's format_number()'s 353057.44) - psycopg3 hands Postgres the
    full-precision Decimal and Postgres's own numeric(p,s) cast does the
    rounding, using standard round-half-away-from-zero, not banker's
    rounding. Using ROUND_HALF_EVEN here previously caused two real Vapi
    MIR rows landing exactly on a X.XX5 boundary to "change" on every
    single re-sync, never converging - confirmed and fixed this session.
    """
    for f in fields:
        existing_val = getattr(existing, f)
        parsed_val = getattr(parsed, f)
        if isinstance(existing_val, Decimal) or isinstance(parsed_val, Decimal):
            dp = model_cls._meta.get_field(f).decimal_places
            quantum = Decimal(1).scaleb(-dp)
            # A parser can hand back a plain int (e.g. a `to_decimal(...) or 0`
            # fallback) where the DB always stores a Decimal - coerce through
            # Decimal(str(...)) before quantizing so int/Decimal compare fairly.
            existing_val = Decimal(str(existing_val)).quantize(quantum, rounding=ROUND_HALF_UP) if existing_val is not None else None
            parsed_val = Decimal(str(parsed_val)).quantize(quantum, rounding=ROUND_HALF_UP) if parsed_val is not None else None
        if existing_val != parsed_val:
            return False
    return True
