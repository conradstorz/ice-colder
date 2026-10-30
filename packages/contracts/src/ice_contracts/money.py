"""Money in integer cents.

Dollars exist only at the human edge: dollar text is parsed with `Decimal`
(never `float`) through `dollars_to_cents`, and shown through `format_cents`.
"""

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Annotated

from pydantic import Field, StrictInt

Cents = Annotated[StrictInt, Field(ge=0)]
PositiveCents = Annotated[StrictInt, Field(gt=0)]

MAX_PRICE_CENTS = 100_000


def dollars_to_cents(x: float | int | str | Decimal) -> int:
    """Convert a dollar amount to whole cents, rounding half-even.

    Raises `ValueError` on a negative, NaN, infinite or unparsable value.
    More than two decimals is accepted (legacy floats are rounded).
    """
    try:
        d = x if isinstance(x, Decimal) else Decimal(str(x).strip())
    except InvalidOperation:
        raise ValueError(f"not a dollar amount: {x!r}") from None
    if not d.is_finite():
        raise ValueError(f"not a finite dollar amount: {x!r}")
    if d < 0:
        raise ValueError(f"negative dollar amount: {x!r}")
    try:
        return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    except InvalidOperation:
        raise ValueError(f"dollar amount out of range: {x!r}") from None


def cents_to_dollar_text(c: int) -> str:
    """`125` -> `"1.25"` (no dollar sign, no thousands separator)."""
    sign = "-" if c < 0 else ""
    whole, frac = divmod(abs(c), 100)
    return f"{sign}{whole}.{frac:02d}"


def format_cents(c: int) -> str:
    """`125` -> `"$1.25"`, `-50` -> `"-$0.50"`."""
    text = cents_to_dollar_text(c)
    return f"-${text[1:]}" if text.startswith("-") else f"${text}"
