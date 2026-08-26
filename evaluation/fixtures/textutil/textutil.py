"""Text normalisation helpers."""


def slugify(text):
    """Lowercase, replace spaces with hyphens."""
    return text.strip().lower().replace(" ", "-")


def truncate(text, limit):
    """Truncate to limit characters, appending an ellipsis when cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
