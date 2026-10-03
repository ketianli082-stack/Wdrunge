"""Print each role's fixed per-call overhead (system prompt + tool schemas)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DASHSCOPE_API_KEY', 'benchmark')
from app.agents.overhead import role_overhead  # noqa: E402

for role, data in role_overhead().items():
    print(f"{role:9} tools={data['tool_count']:2} schema~{data['schema_tokens']:6} prompt~{data['prompt_tokens']:6} total~{data['total_tokens']:6}")
