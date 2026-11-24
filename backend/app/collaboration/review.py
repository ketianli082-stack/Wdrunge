"""What the platform knows about a delivery when it is reviewed, and when the review
may start.

A reviewer (the Leader, or the member who asked for the work) otherwise judges from the
assignee's own words, written before the merge, and from services that the platform
may be migrating or restarting at that moment. Blog10: a delivery was sent back because
a service answered 404 while the platform was still restarting it on the merged code,
and a read-only task was assigned to learn whether cache files had been merged.
"""
import time
from pathlib import Path

# The longest a review notice waits for the platform's work on the merge (a dependency
# installation at the project root may take this long); after it, the notice goes out
# and says that work is still in progress.
REVIEW_HOLD = 900
# How often a task's delivery is refused over its own failing checks before it goes
# through with the failure shown to the reviewer.
CHECK_REFUSALS = 2


def service_fact(runtime: dict, request: dict) -> str:
    state = runtime['state']
    if state == 'restarting':
        return 'the platform is restarting it from the project root right now'
    if state == 'stopped':
        return 'not running'
    if state != 'running':
        return runtime.get('note') or state
    changes = ', '.join(runtime.get('stale', [])[:3])
    if runtime.get('relaunch') == 'pending':
        return 'the platform is starting it from the project root right now'
    if runtime.get('from_snapshot'):
        own = runtime.get('task') == request['id']
        if runtime.get('relaunch') == 'failed':
            return ('still served by the copy in ' + ("this task's" if own else f"task {runtime.get('task')}'s")
                    + ' snapshot: the platform could not start it from the project root (see the service_attention notice)')
        return ("served from this task's snapshot, which holds the delivered files; it moves to the project root when the task's turn ends"
                if own else f"served from task {runtime.get('task')}'s snapshot, which does not have this merge")
    if changes:
        return f'running from the project root, started before this merge changed {changes}: it has not loaded them'
    if runtime.get('since', 0) >= (request.get('merged_at') or float('inf')) - 1:
        return 'running from the project root, restarted after this merge: it serves the merged files'
    return 'running from the project root; this merge changed nothing under its directory'


class ReviewMixin:
    """Methods of Collaboration."""

    async def delivery_verification(self, project, request: dict, root, references: str, refuse: bool) -> list[str]:
        """Before a delivery merges: the platform runs the registered test and build
        commands that cover what the task changed, and looks up every screenshot the
        delivery cites. Returns one line per check for the reviewer.

        `refuse` (the member is delivering in its own turn and can still act): a failing
        command or a cited screenshot that does not exist raises ValueError and nothing
        merges, so it is fixed in the turn that caused it instead of after a review.
        Not without end: after CHECK_REFUSALS refusals of a task the delivery goes
        through with the failure stated for the reviewer, because the cause may lie
        outside the task (another member's files, the environment)."""
        import asyncio
        import json
        from app.collaboration.environment import verification_line
        from app.collaboration.workspaces import IsolatedWorkspace
        workspace = IsolatedWorkspace(root, request['workspace_id'])
        results = await self.run_verification(project, request, root, workspace.work)

        def cited():
            texts = [references, request.get('result') or '', json.dumps(request.get('evidence') or [], ensure_ascii=False)]
            documents = [workspace.work / path for path in workspace.changed_paths() if path.endswith('.md')]
            documents += [Path(root) / path for path in [*(request.get('documents') or []), *(request.get('artifacts') or [])]
                          if path.endswith('.md')]
            for path in documents:
                if path.is_file() and path.stat().st_size < 512 * 1024:
                    texts.append(path.read_text(errors='replace'))
            return workspace.missing_screenshots(texts)
        missing, present = await asyncio.to_thread(cited)
        failed = [result for result in results if result['ok'] is False]
        if refuse and (failed or missing) and request.get('check_refusals', 0) < CHECK_REFUSALS:
            async with self.transaction() as c:
                current = await self._get(c, 'request', project, request['id'])
                current['check_refusals'] = current.get('check_refusals', 0) + 1
                await self._put(c, 'request', project, request['id'], current)
            problems = []
            for result in failed:
                status = 'timed out' if result.get('timed_out') else f"exit {result.get('exit_code')}"
                problems.append(f"- {result['name']} ({' '.join(result['argv'])[:120]}, cwd {result['cwd']}) fails in your workspace: "
                                f"{status}. Output ends:\n{result.get('output') or '(no output)'}")
            if missing:
                problems.append('- These cited screenshots do not exist: ' + ', '.join(missing[:8]) + '. Screenshots in your workspace: '
                                + (', '.join(present[:20]) if present else 'none (browser_verify saves one only when screenshot is on)')
                                + '. Correct the paths where you cite them (result, evidence, report).')
            raise ValueError('Not delivered, nothing merged:\n' + '\n'.join(problems) + '\nFix this and deliver again. If a failing command '
                             'is broken by something outside this task (another member\'s files, the environment), say so in the result.')
        lines = [verification_line(result) for result in results]
        if missing:
            lines.append('cited screenshots that do not exist: ' + ', '.join(missing[:8]))
        return lines

    @staticmethod
    def review_waits(root, request: dict, notified: float) -> bool:
        """Whether a review notice should not be shown yet: the platform is still
        migrating or restarting services for that delivery's merge."""
        from app.execution.processes import processes
        return time.time() - notified < REVIEW_HOLD and processes.merge_settling(root, request['id'])

    async def review_facts(self, c, project, request: dict, root) -> dict:
        """Facts about a delivered task that the platform holds itself, as of now."""
        from app.collaboration.environment import service_runtime
        from app.execution.processes import processes
        facts = {}
        merged = request.get('merged_paths') or []
        if merged:
            facts['merged'] = (f'{len(merged)} file(s) merged into the project: ' + ', '.join(merged[:15])
                               + (f' … and {len(merged) - 15} more' if len(merged) > 15 else ''))
        elif request.get('workspace_published'):
            facts['merged'] = 'no file changed in the project by this delivery'
        left = [*(request.get('left_out') or []),
                *(f'{path}: a screenshot no result, evidence or document cites' for path in (request.get('unmerged_screenshots') or [])[:5])]
        if left:
            facts['not_merged'] = left
        if checks := request.get('checks'):
            facts['checks'] = checks
        if migration := request.get('migration'):
            facts['migration'] = (f"{' '.join(migration['argv'])} ran at the project root after the merge: "
                                  + ('succeeded' if migration['ok'] else
                                     'FAILED, so services keep the code they had (see the migration_failed notice)'))
        services = []
        for service in sorted(await self._list(c, 'service', project), key=lambda s: s['name']):
            port = f" (port {service['port']})" if service.get('port') else ''
            services.append(f"{service['name']}{port}: {service_fact(await service_runtime(root, service), request)}")
        if services:
            facts['services'] = services
        if processes.merge_settling(root, request['id']):
            facts['in_progress'] = ('The platform is still migrating or restarting services for this merge. What a service answers '
                                    'right now is not this delivery; judge the services once this note is gone (inspect the task again).')
        return facts
