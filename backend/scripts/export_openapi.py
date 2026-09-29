"""Write the FastAPI OpenAPI schema for frontend code generation."""

import json
from pathlib import Path

from app.main import app

output = Path(__file__).resolve().parents[1] / "openapi.json"
output.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
print(output)
