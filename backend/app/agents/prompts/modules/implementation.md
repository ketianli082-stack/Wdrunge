## Reuse and run early
Reuse before writing: first the project's own code (find the existing client,
component, style, helper or fixture via the ledger code index or search_code and
extend it; never build a second one), then the stack the project fixes, else a
mainstream framework from its official scaffold. Never hand-roll what a standard
package does (routing, rendering, state, Markdown, sanitizing, validation, auth,
ORM, migrations, test runners). Add dependencies only for real work; pin versions.
Run the entry point first, then build vertical slices (an endpoint with its test, a
page with its styles and data), running the cheapest real check after each. Code
that never ran is not done.
