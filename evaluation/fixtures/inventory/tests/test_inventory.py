from inventory import restock_needed, total_value


def test_total_value():
    assert total_value([{"qty": 2, "price": 3.0}, {"qty": 1, "price": 4.0}]) == 10.0


def test_restock_needed_includes_threshold_boundary():
    items = [{"name": "bolt", "qty": 5}, {"name": "nut", "qty": 9}]
    assert restock_needed(items, threshold=5) == ["bolt"]
