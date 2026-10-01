from datetime import datetime, timezone

from app.tools.validation import tool


@tool
def current_utc_time() -> str:
    """Return the current UTC date and time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()
