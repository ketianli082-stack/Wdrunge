"""Shared configuration and lifecycle state for every team member."""

from dataclasses import dataclass, field
from enum import StrEnum
import os
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from app.agents.identity import team_identity_prompt


class AgentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    prompt_file: str
    tools: tuple[BaseTool, ...] = ()
    skills: tuple[str, ...] = ()
    context_window: int | None = None


def build_model(agent, **options):
    """agent.model(**options), tolerating test doubles that take no arguments."""
    try:
        return agent.model(**options)
    except TypeError:
        return agent.model() if not options.get("variant") else None


@dataclass
class Agent:
    """A configured, ready team member. Model calls begin only when assigned work."""

    spec: AgentSpec
    status: AgentStatus = field(default=AgentStatus.IDLE, init=False)
    total_tokens: int = field(default=0, init=False)
    context_tokens: int = field(default=0, init=False)
    # Input tokens from calls that reported cache data, and the cached part of them.
    cache_input_tokens: int = field(default=0, init=False)
    cached_tokens: int = field(default=0, init=False)
    last_cached_tokens: int | None = field(default=None, init=False)

    def context_window(self) -> int:
        if self.spec.context_window:
            return self.spec.context_window
        try:
            return max(1, int(os.getenv("QWEN_CONTEXT_WINDOW", "131072")))
        except ValueError:
            return 131072

    def record_usage(self, response: object) -> bool:
        """Track reported model usage; never guess when the provider omits it."""
        usage = getattr(response, "usage_metadata", None) or {}
        metadata = getattr(response, "response_metadata", None) or {}
        fallback = metadata.get("token_usage") if isinstance(metadata, dict) else None
        if not isinstance(usage, dict):
            usage = {}
        if not isinstance(fallback, dict):
            fallback = {}
        input_tokens = usage.get("input_tokens", fallback.get("prompt_tokens"))
        total_tokens = usage.get("total_tokens", fallback.get("total_tokens"))
        if not isinstance(total_tokens, int) or total_tokens < 0:
            return False
        self.total_tokens += total_tokens
        cached = (usage.get("input_token_details") or {}).get("cache_read")
        if cached is None:
            cached = (fallback.get("prompt_tokens_details") or {}).get("cached_tokens")
        self.last_cached_tokens = None
        if isinstance(input_tokens, int) and input_tokens >= 0:
            self.context_tokens = input_tokens
            if isinstance(cached, int) and 0 <= cached <= input_tokens:
                self.last_cached_tokens = cached
                self.cache_input_tokens += input_tokens
                self.cached_tokens += cached
        return True

    async def record_usage_durable(self, response: object, purpose: str | None = None) -> bool:
        before = self.total_tokens
        if not self.record_usage(response):
            return False
        store = getattr(self, "_store", None)
        if store:
            from app.agents.usage import current_scope, response_usage
            details = response_usage(response)
            await store.record_agent_usage(
                self.spec.id, self.total_tokens - before, self.context_tokens, self.last_cached_tokens,
                purpose=purpose or ('coordination' if self.spec.id == 'leader' else 'work'),
                **{key: details[key] for key in ('output_tokens', 'reasoning_tokens', 'model', 'latency_ms', 'retries')}, **current_scope())
        return True

    def usage_snapshot(self) -> dict:
        window = self.context_window()
        return {
            "id": self.spec.id,
            "total_tokens": self.total_tokens,
            "context_tokens": self.context_tokens,
            "context_window": window,
            "context_percent": round(min(100.0, self.context_tokens * 100 / window), 2),
            "cache_input_tokens": self.cache_input_tokens,
            "cached_tokens": self.cached_tokens,
        }

    def system_prompt(self) -> str:
        """Instructions without team-collaboration modules (direct conversations)."""
        return self.full_system_prompt(collaborative=False)

    def full_system_prompt(self, collaborative: bool = True) -> str:
        """Shared platform text first and role-specific text after it, so every role's
        prompt starts with the same cacheable prefix; modules only where the role uses them."""
        root = Path(__file__).resolve().parent
        modules = root / "prompts" / "modules"
        from app.agents.memory import MEMORY_PROMPT
        parts = [(modules / "shared.md").read_text(encoding="utf-8")] if collaborative else []
        parts.append(MEMORY_PROMPT)
        parts.append('工具失败会返回 error 和 error_details（category、code）。invalid_arguments：参数没有通过校验，操作没有执行，'
                     '按 fields 里的字段名和 example 修正。返回里有 next_action 就照它做；有 notice 说明你传的某个参数不存在、已被忽略。'
                     'permission_denied 必须遵守授权边界；internal_error 是工具或运行时的问题，不是项目缺陷。'
                     '不重复相同的失败调用，不伪称验证通过；解决不了就向 Leader 报告阻塞。')
        parts += [(root / "prompts" / self.spec.prompt_file).read_text(encoding="utf-8"), team_identity_prompt(self.spec.id)]
        for skill in self.spec.skills:
            parts.append((root / "skills" / skill).read_text(encoding="utf-8"))
        runs_commands = any(tool.name == 'execute_command' for tool in self.spec.tools)
        if runs_commands:
            # Only what the tool descriptions do not already say.
            parts.append("开发工具：先用 detect_environment 确认环境和宿主命令约定（macOS 为 BSD 工具，不用 GNU 专有参数）。"
                         "不要把后端宿主 Python 当作项目环境；依赖用 execute_command 按锁文件装进项目 .venv 或 node_modules。"
                         "命令经审核并在沙箱中运行，出现人工授权时等待用户，不用其他工具绕过。项目外的配置用 read_external_file 请求授权，"
                         "移动/删除文件用 manage_file；页面证据以浏览器验证结果和截图为准。")
        if collaborative:
            names = ['documents.md', 'leader.md' if self.spec.id == 'leader' else 'worker.md']
            if self.spec.id == 'leader' or runs_commands:
                names.append('environment.md')
            parts += [(modules / name).read_text(encoding="utf-8") for name in names]
        if runs_commands and self.spec.id != 'leader':
            # Members who write and run code: reuse what exists, run each slice.
            parts.append((modules / 'implementation.md').read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    def model_name(self, purpose: str = "work", scale: str | None = None) -> str:
        """Model routing by job: a purpose override (e.g. summary), then a role override,
        then a small-goal model, then the default. All optional environment settings."""
        for key in (f"WDRUNGE_MODEL_{purpose.upper()}", f"WDRUNGE_MODEL_{self.spec.id.upper()}",
                    "WDRUNGE_MODEL_SMALL_TASKS" if scale == "small" and self.spec.id != "leader" else ""):
            if key and os.getenv(key, "").strip():
                return os.environ[key].strip()
        return os.getenv("QWEN_MODEL", "qwen3.7-max")

    def model(self, purpose: str = "work", variant: str | None = None, scale: str | None = None) -> ChatOpenAI | None:
        """variant: None (normal streaming), 'non_streaming' (after repeated stream stalls)
        or 'fallback' (QWEN_FALLBACK_MODEL; None when not configured)."""
        # Usage must be reported on streamed calls too: inside a streaming graph even
        # ainvoke streams, and without include_usage the provider returns no usage.
        options = {"stream_usage": True}
        if purpose == "summary" and os.getenv("WDRUNGE_SUMMARY_THINKING", "").strip().lower() not in {"1", "true"}:
            # Summaries need no reasoning, and reasoning tokens count against max_tokens.
            options["extra_body"] = {"enable_thinking": False}
        timeout = os.getenv("QWEN_STREAM_CHUNK_TIMEOUT")
        if timeout:
            # Seconds without a streamed chunk before the call is abandoned and retried.
            options["stream_chunk_timeout"] = float(timeout)
        cap = os.getenv("QWEN_MAX_OUTPUT_TOKENS", "").strip()
        if cap.isdigit():
            options["max_tokens"] = int(cap)
        name = self.model_name(purpose, scale)
        if variant == "fallback":
            name = os.getenv("QWEN_FALLBACK_MODEL", "").strip()
            if not name:
                return None
        if variant == "non_streaming":
            options["disable_streaming"] = True
        return ChatOpenAI(
            model=name,
            api_key=os.environ["DASHSCOPE_API_KEY"],
            base_url=os.getenv(
                "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            **options,
        )

    def snapshot(self) -> dict:
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "status": self.status,
            "tools": [tool.name for tool in self.spec.tools],
            "skills": list(self.spec.skills),
        }
