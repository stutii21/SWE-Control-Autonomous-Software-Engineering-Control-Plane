from textutil import slugify, truncate


def test_slugify_collapses_repeated_spaces():
    assert slugify("  Hello   World  ") == "hello-world"


def test_truncate():
    assert truncate("abcdef", 3) == "abc..."
