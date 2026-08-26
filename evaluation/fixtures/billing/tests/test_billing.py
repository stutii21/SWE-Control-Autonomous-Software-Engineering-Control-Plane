from billing import apply_discount, invoice_total


def test_apply_discount():
    assert apply_discount(200.0, 10) == 180.0


def test_invoice_total_rejects_negative_subtotal():
    try:
        invoice_total(-1.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for negative subtotal")
