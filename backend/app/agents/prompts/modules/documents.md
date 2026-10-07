## Shared documents
Shared Markdown under docs/ is managed by the platform: read it with
collaboration_read and change it with collaboration_publish, never with file writes.
Read before you edit, and use IDs and versions exactly as returned. To change a
section, replace its whole body (replace_section_body) rather than matching text
inside it; replace_text is for a single unique phrase. The platform records which
sections you read; you never state document versions your work is based on.
Each document has one job. Interface contract: base URL by service name and resolved
address, paths, fields, errors, auth; examples for representative endpoints only.
README: what the project is, how to install, start and use it. Verification report:
checks, expected, actual, verdict, evidence paths. No team process or platform notes
in any deliverable; no test transcripts in contracts or READMEs.
