"""Generate a short title after a conversation's first completed turn."""

import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI


def clean_title(value: str) -> str:
    title = " ".join(value.split()).strip(" \"'“”‘’《》`#")
    return title[:15]


async def generate_title(user_message: str, assistant_reply: str) -> str:
    model = ChatOpenAI(
        model=os.getenv("QWEN_TITLE_MODEL", "qwen-flash"),
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        temperature=0,
        max_tokens=48,
    )
    response = await model.ainvoke([
        SystemMessage(content=(
            "根据首轮用户请求和助手回复，提炼一个准确的中文对话标题，最多15个字。"
            "只输出标题，不要引号、标点、解释或换行。"
        )),
        HumanMessage(content=f"用户请求：{user_message}\n助手回复：{assistant_reply[:2000]}"),
    ])
    if not isinstance(response.content, str):
        return ""
    return clean_title(response.content)
