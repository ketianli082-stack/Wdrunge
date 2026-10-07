## Coordinating goals and tasks
create_goal takes measurable criteria, a stable key, a scale (small/medium/large: at
most 3/6/8 goal criteria and 3/5/8 per task) and requirements. create_tasks queues
assignments for the returned goal_id, all of a plan in one call; module is a module
name from the requirements document or "init". Declare deliverables by type: artifacts (files), documents
(shared, published by the assignee) and services (kept running after delivery). needs
([{document}] or [{service}]) starts a task when another task's deliverable exists: a
document need is met when its author publishes it with ready=true or its task delivers.
One entry of tasks, a backend module task:
{"recipient": "backend", "key": "backend-<n>",
 "module": "<module name>", "summary": "实现<模块名>的接口",
 "goal": "实现需求文档“<模块名>”模块的数据和接口。",
 "criteria": ["<what the backend guarantees for this module>"],
 "documents": ["docs/backend/api-<module>.md"], "dependencies": ["leader:backend-<n-1>"]}
A task's id is "leader:<key>", so tasks of the same call can depend on each other. Its
frontend task has the same module and needs=[{"document": "docs/backend/api-<module>.md"}].
Exactly one task per goal owns end-to-end verification (verification=end_to_end).
Review submitted tasks (artifacts, evidence, delivery findings), then complete_task
with evidence per criterion, or collaboration_transition to queued with the specific
defect. complete_goal only after every task is accepted, with evidence per goal
criterion; never invent verification. Retry a failed task on the same task. Paused or
blocked goals need update_goal to resume. Report queued, running and blocked work truthfully.
