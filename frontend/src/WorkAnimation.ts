import type { ActionSample } from "./CharacterBehavior";
import type { DeskRole } from "./DeskScene";

export interface WorkProgress { elapsed: number; editing: number }
export function advanceWork(progress: WorkProgress, delta: number, running: boolean, input: ActionSample["input"]) {
  if (!running) return;
  progress.elapsed += delta;
  if (input === "keyboard" || input === "pen") progress.editing += delta;
}

const pages = [
  { title: "Build together.", subtitle: "A little more possibility.", color: "#d7e4df", ink: "#516b62", columns: 3 },
  { title: "Make room for ideas.", subtitle: "One space. Your whole team.", color: "#cbdbea", ink: "#4c6988", columns: 2 },
  { title: "Create something new.", subtitle: "Small ideas, beautifully made.", color: "#e4d3d8", ink: "#865e72", columns: 3 },
];
export const codeLineSeconds = .8;
export const buildSeconds = codeLineSeconds * 6;
export function workPage(editing: number) {
  const revision = Math.floor(editing / buildSeconds);
  return { ...pages[revision % pages.length], revision };
}
export function workCodeLine(line: number, role?: DeskRole) {
  const revision = Math.floor(line / 6) + 1;
  const page = pages[revision % pages.length];
  const source = role === "frontend" ? [
    `// Preview update ${revision}`,
    `hero.title = '${page.title}'`,
    `theme.surface = '${page.color}'`,
    `grid.columns = ${page.columns}`,
    "await preview.render(page)",
    `// Saved — revision ${revision}`,
  ] : [
    `// Request pipeline ${revision}`,
    "const job = await queue.next()",
    "const data = await db.query(job)",
    "await cache.set(job.id, data)",
    "return response.json(data)",
    `// Tests passed — build ${revision}`,
  ];
  return source[line % source.length];
}

// Shared tablet-local tip coordinates keep screen brush and physical pen together.
export function penTip(time: number) {
  return { x: .05 + Math.sin(time * 1.35) * .26, y: -.30 + Math.sin(time * 2.7) * .065 };
}
