"""Invoice totals with tax and discount handling."""


def apply_discount(amount, percent):
    """Reduce amount by percent (0-100)."""
    return amount - (amount * percent / 100)


def invoice_total(subtotal, tax_rate=0.0, discount_percent=0.0):
    """Discount first, then tax."""
    discounted = apply_discount(subtotal, discount_percent)
    return round(discounted * (1 + tax_rate), 2)
