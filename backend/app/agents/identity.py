"""The public team names, shared with the frontend without changing routing IDs."""

import json
from pathlib import Path

TEAM_IDENTITIES = json.loads(Path(__file__).with_name("identities.json").read_text(encoding="utf-8"))


def member_name(agent_id: str) -> str:
    member = TEAM_IDENTITIES[agent_id]
    return f"{member['name']}（{member['role']}）"


def team_identity_prompt(agent_id: str) -> str:
    if agent_id not in TEAM_IDENTITIES:
        return ""
    roster = "、".join(f"{member_name(key)} [id: {key}]" for key in TEAM_IDENTITIES)
    return (
        f"你的团队名字是{member_name(agent_id)}。团队成员：{roster}。"
        "公开称呼和 @成员时使用“名字（角色）”，用户只提名字或职位时也应识别对应成员。"
        "工具调用继续使用原有英文 id，不要把显示名当作 worker 参数。"
    )
