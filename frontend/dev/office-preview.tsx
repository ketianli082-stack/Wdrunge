// Local visual fixture: deliberately outside the production app entry point.
import { useState } from "react";
import { createRoot } from "react-dom/client";
import OfficeScene, { stations } from "../src/OfficeScene";
import { deskDetails } from "../src/DeskScene";
import { actionNames, type CharacterAction } from "../src/CharacterBehavior";
import type { TeamMember } from "../src/api";
import "../src/App.css";

function OfficePreview() {
  const [status, setStatus] = useState<TeamMember["status"]>("idle");
  const [animate, setAnimate] = useState(true);
  const [visible, setVisible] = useState(true);
  const [action, setAction] = useState<CharacterAction | "auto">("auto");
  const [poseTime, setPoseTime] = useState(3);
  const [width, setWidth] = useState(920);
  const agents = stations.map(({ role }) => ({ id: role, name: deskDetails[role].name, status, tools: [], skills: [] }));
  return <main style={{ padding: 24, maxWidth: 1200, margin: "auto" }}>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 12, paddingBottom: 16 }}>
      <label>角色状态 <select aria-label="角色状态" value={status} onChange={(e) => setStatus(e.target.value as TeamMember["status"])}><option value="idle">待命</option><option value="running">工作中</option><option value="failed">失败</option></select></label>
      <label>角色动作 <select aria-label="角色动作" value={action} onChange={(e) => setAction(e.target.value as CharacterAction | "auto")}><option value="auto">自动</option>{Object.entries(actionNames).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      {action !== "auto" && !animate && <label>定格秒数 <input aria-label="定格秒数" type="number" min={0} max={60} step={.25} value={poseTime} style={{ width: 60 }} onChange={(e) => setPoseTime(Number(e.target.value))} /></label>}
      <label>场景宽度 <select aria-label="场景宽度" value={width} onChange={(e) => setWidth(Number(e.target.value))}><option value={920}>920</option><option value={600}>600</option><option value={360}>360</option></select></label>
      <button aria-pressed={animate} onClick={() => setAnimate(!animate)}>播放动画</button>
      <button onClick={() => setVisible(!visible)}>{visible ? "卸载场景" : "挂载场景"}</button>
    </div>
    <div style={{ width, height: Math.max(560, width / .9), maxWidth: "100%" }}>{visible && <OfficeScene agents={agents} animate={animate} actionOverride={action === "auto" ? null : action} poseTime={action !== "auto" && !animate ? poseTime : undefined} />}</div>
  </main>;
}
const root = createRoot(document.getElementById("root")!);
root.render(<OfficePreview />);
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount());
