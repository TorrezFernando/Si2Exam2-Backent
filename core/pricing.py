"""Shared promotion calculation; UTC half-open validity interval."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


def active(product, now=None):
    start, end = product.promotion_start, product.promotion_end
    if not product.promotion_percent or not start or not end:
        return False
    start = start.replace(tzinfo=start.tzinfo or timezone.utc)
    end = end.replace(tzinfo=end.tzinfo or timezone.utc)
    return start <= (now or datetime.now(timezone.utc)) < end


def effective(product, amount, now=None):
    price = Decimal(str(amount))
    if active(product, now):
        price *= (Decimal(100)-Decimal(str(product.promotion_percent)))/Decimal(100)
    return float(price.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
