"""Leader model: reply directly or delegate one or several assignments."""

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.base import Agent, build_model
from app.agents.resilience import ainvoke_with_retry
from app.collaboration.tools import model_collaboration_tools
from app.agents.tools.delegate_task import delegate_task
from app.agents.tools.delegate_tasks import delegate_tasks
from app.agents.tools.checkpoint_context import checkpoint_context
from app.agents.tools.recall_context import recall_context
from app.agents.tools.validation import model_tool_schemas


class LeaderAgent(Agent):
    async def next_message(
        self, messages: list[BaseMessage], *, allow_delegation: bool = True
    ) -> AIMessage:
        def prepared(variant=None):
            model = build_model(self, variant=variant) if variant else self.model()
            if model is not None and allow_delegation:
                model = model.bind_tools(model_tool_schemas(self.bound_tools(messages)), tool_choice="auto")
            return model
        return await ainvoke_with_retry(prepared(), messages, alternates=(
            lambda: prepared('non_streaming'), lambda: prepared('fallback')))

    def executable_tools(self):
        return [delegate_task, delegate_tasks, checkpoint_context, recall_context, *model_collaboration_tools()]

    def bound_tools(self, messages=()):
        """Coordination core plus groups enabled in this context (app.agents.tool_profiles)."""
        from app.agents.tool_profiles import select_tools
        return select_tools('leader', self.executable_tools(), messages)
