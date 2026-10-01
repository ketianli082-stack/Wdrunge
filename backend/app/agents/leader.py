"""Leader model: reply directly or delegate one or several assignments."""

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.base import Agent
from app.collaboration.tools import active, collaborate
from app.tools.delegate_task import delegate_task
from app.tools.delegate_tasks import delegate_tasks
from app.tools.checkpoint_context import checkpoint_context


class LeaderAgent(Agent):
    async def next_message(
        self, messages: list[BaseMessage], *, allow_delegation: bool = True
    ) -> AIMessage:
        model = self.model()
        if allow_delegation:
            model = model.bind_tools([delegate_task, delegate_tasks, checkpoint_context] + ([collaborate] if active.get() else []), tool_choice="auto")
        return await model.ainvoke(messages)
