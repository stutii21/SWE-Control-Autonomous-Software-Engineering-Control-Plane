"""Simple inventory ledger."""


def total_value(items):
    """Return the total value of all items."""
    return sum(item["qty"] * item["price"] for item in items)


def restock_needed(items, threshold=5):
    """Return names of items at or below the reorder threshold."""
    return [item["name"] for item in items if item["qty"] < threshold]
