## Shared documents
Shared Markdown under docs/ is managed: read and change it with collaboration_read
and collaboration_publish, never with file writes. collaboration_read(whole=true)
returns a whole document and its outline in one call; without whole it returns the
outline, and with section_id one direct section body (parent bodies exclude children).
Use IDs and versions exactly as returned. Publishing returns the new outline;
edit from it. Dependents start when you submit, or on publish with ready=true.
Publish a new document with changes=[{path, base_version:0, summary, content}]; change
an existing one with changes=[{document_id, base_version, summary, edits}] where edits
are replace_text, replace_section_body, insert_section (full Markdown with heading) or
delete_section. Edits run in order and atomically. On validation errors correct the
named fields instead of repeating the call.
Declare based_on versions for documents your change depends on. Allow auto-merge only for
semantically independent sections; only Leader recovers an interrupted publication.
Each document has one job. Interface contract: base URL by service name and resolved
address, paths, fields, errors, auth, compatibility; examples for representative
endpoints only. README:
what the project is, how to install, start (registry commands) and use it. Test or
verification report: checks, expected, actual, verdict, evidence paths. No team
process or platform notes in any deliverable; no test transcripts in contracts or
READMEs.
