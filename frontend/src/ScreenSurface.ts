import * as THREE from "three";
import { actionEnvelope } from "./CharacterBehavior";
import { drawGame, drawWorkAnimation, screenMode, type ScreenActivity } from "./ScreenAnimations";

export type ScreenKind = "overview" | "code" | "terminal" | "metrics" | "preview" | "design" | "roadmap" | "checks";
export type ScreenState = "idle" | "running" | "failed";

/** Each display owns a texture, so future live previews/video can replace its
 * material map without changing the desk geometry or rebuilding the office. */
export interface ScreenSurface {
  mesh: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  texture: THREE.CanvasTexture;
  update: (elapsed: number, state: ScreenState, animate: boolean, activity?: ScreenActivity) => boolean;
}

const titles: Record<ScreenKind, string> = {
  overview: "TEAM / OVERVIEW", code: "workspace.tsx", terminal: "TERMINAL", metrics: "SERVICES",
  preview: "DESIGN / PREVIEW", design: "Untitled — Studio", roadmap: "PRODUCT / ROADMAP", checks: "QUALITY / CHECKS",
};

export function createScreenSurface(width: number, height: number, kind: ScreenKind, accent: string): ScreenSurface {
  const canvas = document.createElement("canvas");
  canvas.width = 768;
  canvas.height = 480;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Screen canvas is unavailable");
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  const surface = new THREE.MeshBasicMaterial({ map: texture, toneMapped: false });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(width, height), surface);
  mesh.name = `screen:${kind}`;
  let previousFrame = "";
  let workTime = 0;
  let previousState: ScreenState = "idle";

  function draw(elapsed: number, state: ScreenState, animate: boolean, activity?: ScreenActivity) {
    // The scene owns the simulation clock. Pausing keeps its exact timestamp;
    // idle holds the last work frame, and status changes still redraw at once.
    if (state === "running" && (animate || previousState !== state || activity?.previewTime !== undefined)) workTime = elapsed;
    previousState = state;
    const moving = state === "running";
    const mode = screenMode(state, activity);
    const time = mode === "work" ? workTime : activity!.clip.time;
    const tick = Math.floor(time * (mode === "work" ? 12 : 20));
    const frame = `${state}:${mode}:${tick}:${activity?.clip.input ?? "none"}`;
    if (previousFrame === frame) return false;
    previousFrame = frame;
    mesh.userData.mode = mode; mesh.userData.frame = tick;
    elapsed = workTime;
    const dark = kind === "code" || kind === "terminal" || kind === "metrics";
    const ink = dark ? "#d8e1e6" : "#424b50";
    const muted = dark ? "#798b99" : "#aab2b5";
    const rect = (x: number, y: number, w: number, h: number, color: string, r = 0) => {
      ctx!.fillStyle = color;
      ctx!.beginPath();
      ctx!.roundRect(x, y, w, h, r);
      ctx!.fill();
    };
    const text = (value: string, x: number, y: number, size = 14, color = ink) => {
      ctx!.fillStyle = color;
      ctx!.font = `${size}px ui-monospace, SFMono-Regular, monospace`;
      ctx!.fillText(value, x, y);
    };
    rect(0, 0, 768, 480, dark ? "#222b36" : "#f4f6f7");
    rect(0, 0, 768, 43, dark ? "#303a46" : "#e5e9eb");
    ["#be9c94", "#cfba89", "#97afa4"].forEach((color, i) => rect(18 + i * 18, 17, 8, 8, color, 4));
    text(titles[kind], 104, 28, 13, muted);
    rect(690, 17, 52, 8, state === "failed" ? "#c7766c" : moving ? accent : muted, 4);

    if (kind === "code") {
      rect(0, 43, 138, 437, "#28323e");
      for (let i = 0; i < 9; i++) rect(20 + (i % 3) * 9, 78 + i * 31, 65 - (i % 3) * 11, 6, "#697b89", 2);
      const code = ["import { workspace } from './studio'", "", "export async function build() {", "  const team = await connect()", "  const result = {", "    status: 'ready',", "    services: await team.resolve(),", "    version: '1.0.0'", "  }", "", "  return result", "}"];
      code.forEach((line, i) => {
        text(`${i + 1}`.padStart(2, "0"), 153, 82 + i * 27, 13, "#637481");
        text(line, 192, 82 + i * 27, 15, ["#a9bdcf", "#c1abd1", "#85b9ae", "#c8b993"][i % 4]);
      });
      if (moving && tick % 12 < 7) rect(214, 409, 8, 18, "#b7d5ce");
      rect(138, 452, 630, 28, "#354550");
      text("main   •   TypeScript                          UTF-8", 155, 470, 12, "#b6c8cc");
    } else if (kind === "terminal") {
      const lines = ["~/workspace  $ run services", "", "  gateway      connected", "  database     connected", "  queue        listening", "", "  > waiting for next task"];
      lines.forEach((line, i) => text(line, 37, 94 + i * 38, 20, i === 0 ? "#dbdfdc" : "#9dc6b7"));
      if (moving) text(`[${String(tick).padStart(4, "0")}] processing…`, 37, 407, 17, "#8ea4b7");
    } else if (kind === "metrics") {
      text("System health", 34, 99, 27);
      for (let i = 0; i < 3; i++) {
        rect(32 + i * 245, 127, 216, 83, "#303e49", 7);
        text(["API", "DATABASE", "WORKERS"][i], 49 + i * 245, 155, 13, muted);
        text(["24 ms", "HEALTHY", "READY"][i], 49 + i * 245, 188, 22, "#acd0bd");
      }
      ctx!.strokeStyle = "#455460";
      ctx!.lineWidth = 1;
      for (let i = 0; i < 4; i++) { ctx!.beginPath(); ctx!.moveTo(36, 259 + i * 47); ctx!.lineTo(734, 259 + i * 47); ctx!.stroke(); }
      ctx!.strokeStyle = "#9ac1b2";
      ctx!.lineWidth = 3;
      ctx!.beginPath();
      for (let i = 0; i <= 36; i++) {
        const y = 334 + Math.sin(i * .62 + (moving ? elapsed * .6 : 0)) * 27 + Math.cos(i * 1.7) * 12;
        if (i === 0) ctx!.moveTo(36, y); else ctx!.lineTo(36 + i * 19, y);
      }
      ctx!.stroke();
    } else if (kind === "design" || kind === "preview") {
      rect(0, 43, 55, 437, "#e4e8e9");
      for (let i = 0; i < 8; i++) rect(21, 67 + i * 37, 13, 13, i === 2 ? accent : "#a7afb2", 3);
      rect(600, 43, 168, 437, "#e9edee");
      text("LAYERS", 622, 77, 12, muted);
      for (let i = 0; i < 6; i++) rect(622, 99 + i * 35, 96 - (i % 3) * 17, 7, "#b7bec1", 2);
      rect(98, 76, 462, 360, "#ffffff", 4);
      if (kind === "design") {
        rect(123, 103, 411, 307, "#e6e2ed");
        ctx!.fillStyle = "#aaa3bf";
        ctx!.beginPath(); ctx!.ellipse(322, 304, 123, 86, -.3, 0, Math.PI * 2); ctx!.fill();
        ctx!.fillStyle = "#d0bbad";
        ctx!.beginPath(); ctx!.arc(359, 204, 78, 0, Math.PI * 2); ctx!.fill();
        rect(203, 181, 75, 177, "#f6f0e5", 37);
        text("Form & feeling.", 146, 387, 21, "#595264");
        ["#aaa3bf", "#d0bbad", "#f6f0e5", "#728c84"].forEach((color, i) => rect(622 + i % 2 * 45, 335 + Math.floor(i / 2) * 40, 30, 28, color, 4));
        if (moving) {
          const x = 330 + Math.cos(elapsed * .7) * 63, y = 260 + Math.sin(elapsed * .7) * 50;
          ctx!.strokeStyle = "#7b728d"; ctx!.lineWidth = 2; ctx!.beginPath(); ctx!.arc(x, y, 13, 0, Math.PI * 2); ctx!.stroke();
        }
      } else {
        text("STUDIO", 126, 111, 15);
        rect(410, 101, 120, 6, "#c8d1d1", 2);
        rect(122, 143, 413, 154, "#d7e4df", 4);
        text("A little more", 145, 187, 28, "#516b62"); text("possibility.", 145, 223, 28, "#516b62");
        rect(145, 251, 95, 24, "#7c9b8c", 4);
        for (let i = 0; i < 3; i++) { rect(123 + i * 141, 319, 129, 66, ["#ded7cb", "#cbd6e0", "#ddd6e4"][i], 4); rect(123 + i * 141, 399, 86, 5, "#bbc5c7", 2); }
      }
    } else {
      text(kind === "checks" ? "Test suite" : kind === "roadmap" ? "Next, together." : "Good work, together.", 34, 100, 28);
      if (kind === "checks") {
        for (let i = 0; i < 5; i++) {
          rect(33, 128 + i * 60, 702, 47, "#ffffff", 5);
          rect(49, 144 + i * 60, 14, 14, i === 4 && moving ? "#cdb489" : "#9cb8a8", 7);
          text(["Components", "API integration", "Accessibility", "Responsive layouts", "End-to-end"][i], 83, 159 + i * 60, 17);
          text(i === 4 && moving ? "RUNNING" : "READY", 632, 157 + i * 60, 12, muted);
        }
      } else {
        const columns = kind === "overview" ? ["PLAN", "IN PROGRESS", "REVIEW"] : ["DISCOVER", "DESIGN", "DELIVER"];
        columns.forEach((title, i) => {
          const x = 33 + i * 245;
          text(title, x, 143, 13, muted);
          rect(x, 158, 216, 278, "#e9edef", 7);
          for (let j = 0; j < 3 - i % 2; j++) {
            rect(x + 12, 174 + j * 81, 192, 67, "#ffffff", 5);
            rect(x + 25, 190 + j * 81, 49, 6, [accent, "#9ab3c2", "#c4b594"][i], 3);
            rect(x + 25, 209 + j * 81, 133 - j * 23, 6, "#b9c2c7", 2);
            rect(x + 25, 224 + j * 81, 83, 4, "#dbe0e2", 2);
          }
        });
      }
    }
    if (moving) {
      drawWorkAnimation(ctx!, kind, workTime, activity);
      rect(0, 476, (elapsed * 90) % 768, 4, accent);
    }
    if (mode !== "work") {
      ctx!.save();
      ctx!.globalAlpha = actionEnvelope(activity!.clip.time, activity!.clip.duration, .5, .8);
      drawGame(ctx!, mode, Math.min(time, activity!.clip.duration - 1.4));
      ctx!.restore();
    }
    if (state === "failed") { rect(0, 442, 768, 38, "#9d625b"); text("ATTENTION REQUIRED", 24, 467, 15, "#ffffff"); }
    texture.needsUpdate = true;
    return true;
  }
  draw(0, "idle", false);
  return { mesh, texture, update: draw };
}
