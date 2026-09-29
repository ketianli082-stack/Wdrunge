import type { ActionSample } from "./CharacterBehavior";
import { canPlayAction } from "./CharacterBehavior";
import type { DeskRole } from "./DeskScene";
import { buildSeconds, codeLineSeconds, penTip, workCodeLine, workPage, type WorkProgress } from "./WorkAnimation";
import type { ScreenKind, ScreenState } from "./ScreenSurface";

export interface ScreenActivity { role: DeskRole; clip: ActionSample; screenIndex: number; previewTime?: number; work?: WorkProgress }
export function screenMode(state: ScreenState, activity?: ScreenActivity) {
  if (activity?.clip.action === "gaming" && canPlayAction(activity.role, state, "gaming")) {
    if (activity.role === "backend" && activity.screenIndex === 1) return "arena";
    if (activity.role === "testing" && activity.screenIndex === 0) return "racing";
  }
  return "work";
}

function painter(ctx: CanvasRenderingContext2D) {
  return {
    rect(x: number, y: number, w: number, h: number, color: string, radius = 0) {
      ctx.fillStyle = color; ctx.beginPath(); ctx.roundRect(x, y, w, h, radius); ctx.fill();
    },
    text(value: string, x: number, y: number, size = 16, color = "#c5d6d5") {
      ctx.font = `${size}px ui-monospace, monospace`; ctx.fillStyle = color; ctx.fillText(value, x, y);
    },
    line(points: number[][], color: string, width = 2) {
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
      points.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.stroke();
    },
    circle(x: number, y: number, radius: number, color: string) {
      ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.fill();
    },
  };
}

/** Large moving shapes remain legible from the office camera. Both games are
 * deterministic local canvas illustrations; no external game or network. */
export function drawGame(ctx: CanvasRenderingContext2D, mode: "arena" | "racing", time: number) {
  const { rect, text, line, circle } = painter(ctx);
  if (mode === "arena") {
    rect(0, 0, 768, 480, "#17302f");
    for (let i = 0; i < 12; i++) line([[i * 70, 45], [i * 70, 440]], "#294b43");
    for (let i = 0; i < 7; i++) line([[0, 50 + i * 65], [768, 50 + i * 65]], "#294b43");
    for (const [x, y, w, h] of [[80, 95, 140, 55], [545, 305, 150, 62], [75, 326, 75, 77], [565, 80, 95, 80]]) {
      rect(x + 6, y + 8, w, h, "#102323", 10); rect(x, y, w, h, "#4f7360", 10);
      rect(x + 9, y + 8, w - 18, 8, "#6d9275", 3);
    }
    const player = [365 + Math.sin(time * 1.4) * 102, 245 + Math.cos(time * 1.9) * 67];
    const aim = [535 + Math.cos(time * 1.6) * 91, 211 + Math.sin(time * 1.2) * 111];
    for (let i = 0; i < 4; i++) {
      const x = 384 + Math.cos(time * .7 + i * 1.57) * 239, y = 242 + Math.sin(time * .9 + i * 1.57) * 145;
      circle(x + 3, y + 5, 17, "#142b27"); circle(x, y, 15, "#d9a379");
      rect(x - 17, y - 29, 34, 4, "#f0c39e", 2);
    }
    circle(player[0], player[1], 29, "#437f7a"); circle(player[0], player[1], 20, "#91d8d3");
    line([player, [player[0] + (aim[0] - player[0]) * .13, player[1] + (aim[1] - player[1]) * .13]], "#e1ffff", 9);
    // A continuous projectile trail, rather than full-screen flashes.
    for (let i = 0; i < 3; i++) {
      const flight = (time * 1.8 + i / 3) % 1;
      circle(player[0] + (aim[0] - player[0]) * flight, player[1] + (aim[1] - player[1]) * flight, 4, "#e3d7a2");
    }
    line([[aim[0] - 12, aim[1]], [aim[0] + 12, aim[1]]], "#e6e6b6");
    line([[aim[0], aim[1] - 12], [aim[0], aim[1] + 12]], "#e6e6b6");
    rect(0, 0, 768, 45, "#102626"); text("GARDEN ARENA", 23, 30, 22);
    text(`WAVE  ${1 + Math.floor(time / 4) % 3}`, 579, 29, 18, "#d9c796");
    rect(0, 433, 768, 47, "#102626"); rect(25, 449, 166, 12, "#35594d", 5);
    rect(25, 449, 136 + Math.sin(time) * 20, 12, "#91caa9", 5);
    text(`SCORE ${String(Math.floor(time * 3) * 100).padStart(5, "0")}`, 489, 462, 20);
  } else {
    rect(0, 0, 768, 480, "#aec4d0");
    circle(584, 85, 32, "#eee4c1");
    ctx.fillStyle = "#738d9a"; ctx.beginPath(); ctx.moveTo(0, 180);
    for (let i = 0; i <= 8; i++) ctx.lineTo(i * 110, 125 + Math.sin(i * 2.4) * 40);
    ctx.lineTo(768, 220); ctx.lineTo(0, 220); ctx.fill();
    rect(0, 182, 768, 298, "#79988b");
    const bend = Math.sin(time * .65) * 60;
    for (let strip = 0; strip < 45; strip++) {
      const p = strip / 44, y = 183 + p * p * 300, nextY = 183 + ((strip + 1) / 44) ** 2 * 300;
      const center = 384 + bend * (1 - p), half = 35 + p * 260;
      rect(center - half - 10, y, half * 2 + 20, nextY - y + 1, (Math.floor(strip / 4 + time * 9) % 2) ? "#e0c6aa" : "#a96965");
      rect(center - half, y, half * 2, nextY - y + 1, "#4d606a");
      if (Math.floor(strip / 4 + time * 9) % 2) for (const lane of [-.33, .33]) rect(center + half * lane, y, 2 + p * 5, nextY - y + 1, "#e5e0c9");
    }
    const car = (x: number, y: number, scale: number, color: string) => {
      rect(x - 33 * scale, y - 16 * scale, 66 * scale, 83 * scale, "#263943", 8 * scale);
      rect(x - 26 * scale, y - 25 * scale, 52 * scale, 86 * scale, color, 9 * scale);
      rect(x - 19 * scale, y - 6 * scale, 38 * scale, 22 * scale, "#b7d3dc", 5 * scale);
      rect(x - 20 * scale, y + 44 * scale, 40 * scale, 7 * scale, "#ecdfb8", 2);
    };
    for (let i = 0; i < 2; i++) {
      const progress = (time * .14 + i * .5) % 1;
      car(384 + (i ? 1 : -1) * (40 + progress * 115) + bend * (1 - progress), 199 + progress * 235, .25 + progress * .6, i ? "#b7a4c5" : "#d1b579");
    }
    car(384 + Math.sin(time * 1.3) * 83, 374, 1.05, "#8dc9c9");
    rect(17, 17, 237, 59, "#253d4c", 9); text("COASTLINE SPRINT", 30, 43, 20); text("LAP  02 / 03", 30, 65, 14);
    rect(585, 389, 166, 73, "#253d4c", 10); text(`${168 + Math.floor(Math.sin(time * 2) * 12)} km/h`, 605, 435, 25);
    text("P2 / 8", 635, 40, 22, "#263d4e");
  }
}

export function drawWorkAnimation(ctx: CanvasRenderingContext2D, kind: ScreenKind, time: number, activity?: ScreenActivity) {
  const { rect, text, line, circle } = painter(ctx);
  const tick = Math.floor(time * 12);
  if (kind === "code") {
    const editing = activity?.work?.editing ?? time;
    const currentLine = Math.floor(editing / codeLineSeconds);
    const fraction = editing / codeLineSeconds - currentLine;
    const firstLine = Math.max(0, currentLine - 6);
    const scrolling = currentLine >= 6 ? fraction * 43 : 0;
    rect(139, 51, 629, 399, "#222b36");
    ctx.save(); ctx.beginPath(); ctx.rect(139, 54, 629, 335); ctx.clip();
    for (let index = firstLine; index <= currentLine; index++) {
      const value = workCodeLine(index, activity?.role);
      const visible = index < currentLine ? value : value.slice(0, Math.floor(fraction * value.length));
      const y = 90 + (index - firstLine) * 43 - scrolling;
      if (index === currentLine) rect(185, y - 25, 557, 34, "#334e59", 4);
      text(String(index + 1), 151, y, 15, "#718e9c");
      text(visible, 193, y, 21, ["#abc3dc", "#a3c7b1", "#c5b2d6"][index % 3]);
      if (index === currentLine && tick % 12 < 8 && (!activity || activity.clip.input === "keyboard")) rect(193 + visible.length * 12.6, y - 19, 3, 23, "#dce8df");
    }
    ctx.restore();
    const revision = workPage(editing).revision;
    rect(186, 398, 537, 34, "#304d48", 4);
    text(`BUILD ${revision} ✓   EDITING ${revision + 1}`, 204, 422, 17, "#9cc6b1");
  } else if (kind === "terminal") {
    rect(0, 48, 768, 432, "#222b36");
    const log = ["GET /api/team        200   24ms", "queue: task accepted", "worker: building response", "db: transaction committed", "tests: 24 passed, 0 failed", "deploy: health check OK"];
    const scroll = Math.floor(time * 1.7);
    for (let i = 0; i < 8; i++) {
      text(`[${String(scroll + i).padStart(3, "0")}]`, 24, 91 + i * 43, 17, "#6d8999");
      text(log[(scroll + i) % log.length], 102, 91 + i * 43, 20, i % 3 ? "#a3cbbb" : "#c5d6df");
    }
    rect(24, 439, 10, 20, tick % 12 < 8 ? "#a3cbbb" : "#222b36");
  } else if (kind === "checks") {
    const active = Math.floor(time * .75) % 6;
    for (let i = 0; i < 5; i++) {
      rect(49, 144 + i * 60, 14, 14, i < active ? "#76a88e" : i === active ? "#c7aa70" : "#c6ced1", 7);
      rect(599, 139 + i * 60, 125, 26, "#ffffff");
      text(i < active ? "PASS" : i === active ? "RUNNING" : "QUEUED", 618, 158 + i * 60, 14, "#637c74");
      if (i === active) rect(82, 167 + i * 60, 500 * ((time * .75) % 1), 3, "#bda572");
    }
  } else if (kind === "design") {
    const drawing = !activity || activity.clip.input === "pen";
    const drawTime = drawing ? activity?.clip.time ?? time : 0;
    // The texture and the actual stylus use the same tablet-local coordinates.
    const points = Array.from({ length: 70 }, (_, index) => {
      const point = penTip(Math.max(0, drawTime - (69 - index) * .025));
      return [(point.x / 1.82 + .5) * 768, (.5 - point.y / 1.13) * 480];
    });
    line(points, "#668779", 7);
    const [x, y] = points[points.length - 1]; circle(x, y, 6, "#fff9ed");
    rect(617, 99 + Math.floor(time / 2) % 5 * 35, 130, 22, "#d5cedf", 3);
  } else if (kind === "preview") {
    const editing = activity?.work?.editing ?? time;
    const page = workPage(editing);
    rect(0, 43, 768, 437, "#f5f5f0");
    rect(22, 56, 724, 37, "#e0e6e3", 7);
    text(`localhost / preview — v${page.revision}`, 44, 81, 16, "#667e75");
    text("STUDIO", 40, 125, 21, page.ink);
    rect(39, 146, 690, 171, page.color, 7);
    text(page.title, 70, 195, 32, page.ink); text(page.subtitle, 70, 231, 19, page.ink);
    rect(70, 255, 147, 36, page.ink, 5); text("Explore →", 88, 279, 17, "#f4f6f2");
    for (let i = 0; i < page.columns; i++) {
      const width = 670 / page.columns;
      rect(39 + i * (width + 10), 336, width, 87, ["#ded7cb", "#cbd6e0", "#ddd6e4"][i], 6);
      rect(55 + i * (width + 10), 352, width - 42, 9, page.ink, 4);
      rect(55 + i * (width + 10), 373, width - 72, 6, "#fafaf4", 3);
    }
    const progress = (editing % buildSeconds) / buildSeconds;
    rect(39, 449, 690, 5, "#d9e1db", 2); rect(39, 449, progress * 690, 5, page.ink, 2);
    text(`LIVE  ✓  v${page.revision}`, 533, 443, 14, page.ink);
    if (activity?.clip.input === "mouse") {
      const x = 180 + Math.sin(time * 1.5) * 55, y = 274 + Math.sin(time * 2) * 30;
      line([[x, y + 20], [x, y], [x + 15, y + 13], [x, y + 20]], "#415d52", 3);
    }
  } else if (kind === "overview" || kind === "roadmap") {
    const phase = (time % 6) / 6;
    const x = 45 + Math.min(1, phase * 1.6) * 245, y = 255 + Math.sin(phase * Math.PI) * -58;
    rect(45, 255, 192, 67, "#e9edef", 5);
    rect(x + 4, y + 5, 192, 67, "#c5d4cc", 6); rect(x, y, 192, 67, "#fcfdf8", 6);
    rect(x + 13, y + 12, 52, 7, "#8ca99b", 3);
    text(kind === "overview" ? "Review ready" : "User flow", x + 13, y + 42, 17, "#566b61");
    circle(x + 169, y + 51, 6, "#92b3a2");
  }
  if (activity?.clip.input === "mouse" && kind === "code") {
    const y = 105 + Math.sin(time * 1.1) * 24;
    line([[566, y], [566, y + 21], [580, y + 13], [566, y]], "#dfdfc2", 3);
  }
}
