from datetime import datetime, timezone

from app.agents.tools.validation import tool
from app.agents.context_policy import RECOMPUTABLE


@tool(context_policy=RECOMPUTABLE)
def current_utc_time() -> str:
    """Return the current UTC date and time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()
