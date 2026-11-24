"""Leader model: reply directly or delegate one or several assignments."""

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.base import Agent, build_model
from app.agents.resilience import ainvoke_with_retry, route_of
from app.collaboration.tools import model_collaboration_tools
from app.agents.tools.delegate_task import delegate_task
from app.agents.tools.delegate_tasks import delegate_tasks
from app.agents.tools.checkpoint_context import checkpoint_context
from app.agents.tools.recall_context import recall_context
from app.agents.tools.validation import model_tool_schemas


def project_read_tools() -> dict:
    """Read-only project tools: checking one fact (a file exists, what a line says)
    takes the Leader one call instead of a task assigned to a member, which costs that
    member a whole turn and delays the work queued behind it (Blog10: 17 minutes)."""
    from app.agents.tools.file_management import make_file_management
    from app.agents.tools.read_project_file import make_read_project_file
    tools = [make_read_project_file(('.',)), *make_file_management(('.',), ())]
    return {tool.name: tool for tool in tools if tool.name in LEADER_READS}


# A Leader read returns at most this many characters: enough to check a fact, and its
# context is for coordination, not for holding source files.
LEADER_READ_LIMIT = 6000
LEADER_READS = ('read_project_file', 'search_code', 'browse_directory')
PROJECT_READ_TOOLS = project_read_tools()


class LeaderAgent(Agent):
    async def next_message(
        self, messages: list[BaseMessage], *, allow_delegation: bool = True
    ) -> AIMessage:
        def prepared(variant=None):
            model = build_model(self, variant=variant) if variant else self.model()
            if model is not None and allow_delegation:
                model = model.bind_tools(model_tool_schemas(self.bound_tools(messages)), tool_choice="auto")
            return model
        primary = prepared()
        return await ainvoke_with_retry(primary, messages, route=route_of(primary), alternates=(
            lambda: prepared('non_streaming'), lambda: prepared('fallback')))

    def executable_tools(self):
        return [delegate_task, delegate_tasks, checkpoint_context, recall_context, *PROJECT_READ_TOOLS.values(),
                *model_collaboration_tools()]

    def bound_tools(self, messages=()):
        """Coordination core plus groups enabled in this context (app.agents.tool_profiles)."""
        from app.agents.tool_profiles import select_tools
        return select_tools('leader', self.executable_tools(), messages)
