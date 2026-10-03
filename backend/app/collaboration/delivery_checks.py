"""Delivery checks: findings for review, never a block.

Run when a task submits (and on demand before submitting) over the delivered files
and published documents. Each finding names a file and line so Leader or the user
can accept it or send the task back:
- platform_leak: Wdrunge workspace paths or the platform's own ports inside project
  deliverables (they break outside the platform). Wording about the platform or the
  team is prevented by the prompts, not checked: phrase lists either miss or misfire;
- port_mismatch: a loopback address in a document whose port is not a registered
  project service (documents must match the environment registry);
- evidence_in_contract: raw test transcripts inside interface documents (evidence
  belongs in the task result, journal or a test report);
- oversized: more delivered text than the goal scale suggests;
- generated_without_command: generated artifacts (OpenAPI, clients) without a
  registered command that regenerates them.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SKIP_DIRS = {'node_modules', '.venv', 'vendor', 'dist', 'build', '.git', '__pycache__', 'target', '.wdrunge-runs'}
TEXT_SUFFIXES = {'.py', '.js', '.ts', '.tsx', '.jsx', '.vue', '.html', '.css', '.md', '.txt', '.json', '.yaml', '.yml',
                 '.toml', '.sh', '.go', '.rs', '.java', '.kt', '.rb', '.php', '.cfg', '.ini', '.env', '.example', ''}
SIZE_LINES = {'small': 400, 'medium': 2500, 'large': None}
# Package-manager output: neither authored text nor a place for platform wording.
LOCKFILES = {'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock'}
LOOPBACK_URL = re.compile(r'(?:https?|wss?)://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):(\d{2,5})')
EVIDENCE_HEADING = re.compile(r'^#{1,6}\s*.*(验证证据|实测|证据|evidence|test results?|verification)', re.I)
HTTP_TRANSCRIPT = re.compile(r'^\s*(HTTP/1\.[01] \d{3}|< HTTP/|> (GET|POST|OPTIONS|PUT|DELETE) )')
GENERATED = re.compile(r'(^|/)(openapi|swagger)\.(json|ya?ml)$|\.gen\.(ts|js|py)$|(^|/)generated/', re.I)
CONTRACT_NAME = re.compile(r'(contract|契约|api|interface|openapi|schema)', re.I)


def delivered_files(root: Path, paths) -> list[Path]:
    files = []
    for path in dict.fromkeys(paths):
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()) or not target.exists():
            continue
        if target.is_file():
            files.append(target)
            continue
        for directory, dirs, names in os.walk(target, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
            files.extend(Path(directory) / name for name in names)
    result = []
    for file in files:
        if file.is_symlink() or any(part in SKIP_DIRS for part in file.relative_to(root.resolve()).parts[:-1]):
            continue
        if (file.suffix.lower() not in TEXT_SUFFIXES or file.name.endswith(('.min.js', '.lock')) or file.name in LOCKFILES
                or file.stat().st_size > 512 * 1024):
            continue
        result.append(file)
    return result


def scan_file(relative: str, text: str, context: dict) -> list[dict]:
    findings, seen = [], set()
    def add(kind, line, excerpt, message, severity='medium'):
        if (kind, line) in seen:
            return
        seen.add((kind, line))
        findings.append({'kind': kind, 'path': relative, 'line': line, 'excerpt': excerpt.strip()[:160],
                         'message': message, 'severity': severity})
    is_doc = relative.lower().endswith(('.md', '.txt', '.rst'))
    contract = is_doc and bool(CONTRACT_NAME.search(relative))
    transcript_lines = 0
    for number, line in enumerate(text.splitlines(), 1):
        if '.wdrunge-runs' in line or any(prefix in line for prefix in context['private_paths']):
            add('platform_leak', number, line, 'Contains a Wdrunge workspace or application path; deliverables should use project-relative paths.', 'high')
        for match in LOOPBACK_URL.finditer(line):
            port = int(match.group(1))
            if port in context['reserved_ports']:
                add('platform_leak', number, line, f"Points at port {port}, which belongs to the Wdrunge platform.", 'high')
            elif context['registry_ports'] and port not in context['registry_ports']:
                add('port_mismatch', number, line, f"Port {port} is not a registered project service ({sorted(context['registry_ports'])}); "
                    'update the document or the environment registry so they agree.')
        if contract and EVIDENCE_HEADING.match(line):
            add('evidence_in_contract', number, line, 'Interface documents describe the interface; put verification evidence in the task result, journal or test report.', 'low')
        if contract and HTTP_TRANSCRIPT.match(line):
            transcript_lines += 1
    if contract and transcript_lines >= 3:
        add('evidence_in_contract', 0, f'{transcript_lines} raw HTTP transcript lines', 'Interface document contains raw request/response transcripts; keep one example per case and move evidence to the test report.', 'low')
    return findings


async def delivery_findings(service, project, request, root, artifacts=None) -> list[dict]:
    """Findings over a task's delivered files (under `root`) and its published documents."""
    root = Path(root)
    async with service.reading() as c:
        project_root = Path((await service._project(c, project))['workspace_path'])
    from app.agents.tools._workspace import WORKSPACE_ROOT
    if project_root.resolve() == WORKSPACE_ROOT.resolve():
        return []  # Wdrunge developing itself legitimately names its own paths and ports.
    async with service.reading() as c:
        documents = await service._list(c, 'document', project)
        goal = await service._get(c, 'goal', project, request['goal_id'], False) if request.get('goal_id') else None
    environment = await service.environment(project, discover_manifests=False)
    context = {
        'reserved_ports': set(environment['reserved_ports']),
        'registry_ports': {s['port'] for s in environment['services'] if s.get('port')},
        'private_paths': [str(project_root.resolve() / '.wdrunge-runs')],
    }
    from platformdirs import user_data_path
    context['private_paths'].append(str(user_data_path('Wdrunge', 'Wdrunge')))
    paths = list(artifacts if artifacts is not None else request.get('artifacts') or [])
    published = {d.get('path') for d in documents}
    doc_paths = [p for p in [*request.get('documents', []), *paths] if p in published]
    files = [(f, root) for f in delivered_files(root, [p for p in paths if p not in published])]
    files += [(f, project_root) for f in delivered_files(project_root, doc_paths)]
    findings, lines = [], 0
    for file, base in files:
        relative = str(file.relative_to(base.resolve()))
        try:
            text = file.read_text(errors='replace')
        except OSError:
            continue
        lines += text.count('\n') + 1
        findings.extend(scan_file(relative, text, context))
        if GENERATED.search(relative) and not any(relative in ' '.join(c['argv']) or relative in (c.get('purpose') or '')
                                                  for c in environment['commands']):
            findings.append({'kind': 'generated_without_command', 'path': relative, 'line': 0, 'excerpt': relative, 'severity': 'low',
                             'message': 'Generated artifact without a registered regenerate command; record it with '
                                        'collaboration_register_command (argv or purpose naming this file) or remove the hand-written copy.'})
    scale = (goal or {}).get('scale')
    limit = SIZE_LINES.get(scale) if scale else None
    if limit and lines > limit:
        findings.append({'kind': 'oversized', 'path': '', 'line': 0, 'excerpt': f'{lines} lines delivered', 'severity': 'medium',
                         'message': f'{lines} lines of text delivered for a {scale} goal (guide: {limit}). Justify the size in the '
                                    'result or remove what the criteria do not require.'})
    # Keep the list readable: the most important first, bounded per kind.
    order = {'high': 0, 'medium': 1, 'low': 2}
    findings.sort(key=lambda f: (order[f['severity']], f['kind'], f['path'], f['line']))
    kept, per_kind = [], {}
    for finding in findings:
        per_kind[finding['kind']] = per_kind.get(finding['kind'], 0) + 1
        if per_kind[finding['kind']] <= 8:
            kept.append(finding)
    return kept


def findings_summary(findings) -> str:
    if not findings:
        return ''
    counts = {}
    for finding in findings:
        counts[finding['kind']] = counts.get(finding['kind'], 0) + 1
    return 'Delivery findings (review before accepting): ' + ', '.join(f'{k} ×{v}' for k, v in counts.items()) + '. ' + ' | '.join(
        f"{f['path']}:{f['line']} {f['message']}" for f in findings[:5])
