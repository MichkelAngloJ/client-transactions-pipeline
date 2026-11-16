import re
from decimal import ROUND_HALF_UP, Decimal


def inr(amount) -> str:
    """Format an amount in whole rupees with Indian digit grouping: 1234567 -> '₹12,34,567'."""
    value = Decimal(amount).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        # Group the leading digits in pairs from the right: 1234 -> 12,34.
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        digits = f"{head},{tail}"
    return f"{sign}₹{digits}"
