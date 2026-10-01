import * as THREE from "three";
import { characterAppearance } from "./CharacterAppearance";
import { box, cylinder, material, type DeskModel, type DeskRole } from "./DeskScene";
import { actionEnvelope, createCharacterBehavior, easeAction, sampleAction, type ActionSample, type CharacterAction } from "./CharacterBehavior";
import { penTip } from "./WorkAnimation";
import type { ScreenState } from "./ScreenSurface";

type Vec3 = [number, number, number];
const UP = new THREE.Vector3(0, 1, 0);

function ellipsoid(parent: THREE.Group, size: Vec3, position: Vec3, surface: THREE.MeshStandardMaterial) {
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(1, 20, 14), surface);
  mesh.scale.set(...size); mesh.position.set(...position);
  mesh.castShadow = true; mesh.receiveShadow = true; parent.add(mesh);
  return mesh;
}
function segment(parent: THREE.Group, radius: number, surface: THREE.MeshStandardMaterial) {
  const mesh = new THREE.Mesh(new THREE.CapsuleGeometry(radius, 1, 4, 12), surface);
  mesh.castShadow = true; mesh.receiveShadow = true; parent.add(mesh);
  return { mesh, radius };
}
function placeSegment(part: ReturnType<typeof segment>, a: THREE.Vector3, b: THREE.Vector3) {
  const direction = b.clone().sub(a);
  part.mesh.position.copy(a).add(b).multiplyScalar(.5);
  part.mesh.scale.y = direction.length() / (1 + 2 * part.radius);
  part.mesh.quaternion.setFromUnitVectors(UP, direction.normalize());
}
const smooth = (a: number, b: number, value: number) => THREE.MathUtils.smoothstep(value, a, b);
const vector = (x: number, y: number, z: number) => new THREE.Vector3(x, y, z);

interface Pose {
  body: THREE.Euler; head: THREE.Euler; headSide: number; lean: THREE.Vector3;
  left: THREE.Vector3; right: THREE.Vector3;
  cup: THREE.Vector3; cupRotation: THREE.Quaternion; grip: number; stretch: number;
  phone: THREE.Vector3; phoneAmount: number;
  pen: THREE.Vector3; penRotation: THREE.Quaternion; penScale: number;
  mouse: THREE.Vector3;
}

function samplePose(desk: DeskModel, clip: ActionSample, clock: number): Pose {
  const t = clip.time;
  const breathing = Math.sin(clock * 1.7) * .004;
  const pose: Pose = {
    body: new THREE.Euler(-.06, 0, 0), head: new THREE.Euler(-.06, Math.sin(clock * .21) * .025, 0),
    headSide: 0, lean: vector(0, .86 + breathing, 1.34), left: vector(-.24, 1.05, 1.16), right: vector(.24, 1.05, 1.16),
    cup: desk.interaction.cupHome.clone(), cupRotation: desk.interaction.cupHomeRotation.clone(), grip: 0, stretch: 0,
    phone: vector(.37, .94, 1.32), phoneAmount: 0,
    pen: desk.interaction.penHome.clone(), penRotation: desk.interaction.penRotation.clone(), penScale: 1,
    mouse: desk.interaction.mouseHome.clone(),
  };
  if (clip.action === "rest") {
    // Small, offset adjustments keep awake rest alive without touching props.
    // Fade to the common neutral pose at both ends for seamless handoffs.
    const amount = actionEnvelope(t, clip.duration, .8, 1);
    pose.head.y += Math.sin(clock * .92) * .16 * amount;
    pose.head.x += Math.sin(clock * 1.2) * .045 * amount;
    pose.body.z = Math.sin(clock * 1.05) * .035 * amount;
    pose.lean.x += Math.sin(clock * .83) * .035 * amount;
    pose.lean.z += Math.sin(clock * .7) * .025 * amount;
    pose.left.y += (.5 + .5 * Math.sin(clock * 1.6)) * .045 * amount;
    pose.right.x += Math.sin(clock * 1.1) * .025 * amount;
  }
  if (clip.input === "keyboard-mouse") {
    const amount = actionEnvelope(t, clip.duration);
    pose.mouse.add(vector(Math.sin(t * 2.4) * .055 * amount, 0, Math.sin(t * 1.7) * .035 * amount));
    pose.left.lerp(desk.interaction.keyboard.clone().add(vector(-.22, Math.max(0, Math.sin(t * 16)) * .014, .015)), amount);
    pose.right.lerp(pose.mouse.clone().add(vector(0, .046, 0)), amount);
    pose.body.x = THREE.MathUtils.lerp(pose.body.x, -.13, amount); pose.body.z = Math.sin(t * 1.2) * .018 * amount;
    pose.head.x = THREE.MathUtils.lerp(pose.head.x, -.1, amount); pose.head.y = THREE.MathUtils.lerp(pose.head.y, Math.sin(t * 1.6) * .045, amount);
  } else if (clip.input === "keyboard") {
    const key = desk.interaction.keyboard;
    const amount = actionEnvelope(t, clip.duration);
    pose.left.lerp(key.clone().add(vector(-.20, Math.max(0, Math.sin(t * 19)) * .013, .015)), amount);
    pose.right.lerp(key.clone().add(vector(.20, Math.max(0, Math.sin(t * 17 + 2)) * .013, .015)), amount);
    pose.body.x = THREE.MathUtils.lerp(pose.body.x, -.12, amount); pose.head.x = THREE.MathUtils.lerp(pose.head.x, -.10, amount);
  } else if (clip.input === "mouse") {
    const amount = actionEnvelope(t, clip.duration);
    pose.mouse.add(vector(Math.sin(t * 1.5) * .045 * amount, 0, Math.sin(t * 2.0) * .03 * amount));
    pose.right.lerp(pose.mouse.clone().add(vector(0, .046, 0)), amount);
    pose.left.lerp(vector(-.27, 1.09, 1.12), amount);
    pose.body.x = THREE.MathUtils.lerp(pose.body.x, -.10, amount); pose.head.y = THREE.MathUtils.lerp(pose.head.y, -.075, amount);
  } else if (clip.input === "pen") {
    const tablet = desk.getObjectByName("drawing-tablet")!;
    tablet.updateMatrix();
    // Pen tip remains on the actual tilted tablet surface, not a world-space
    // approximation. The wrist follows the grip a short way up the stylus.
    const point = penTip(t);
    const tip = vector(point.x, point.y, .046).applyMatrix4(tablet.matrix);
    const normal = vector(0, 0, 1).applyQuaternion(tablet.quaternion);
    const amount = actionEnvelope(t, clip.duration);
    pose.pen.lerp(tip.clone().addScaledVector(normal, .217), amount);
    pose.penRotation.slerp(new THREE.Quaternion().setFromUnitVectors(UP, normal), amount); pose.penScale = THREE.MathUtils.lerp(1, .55, amount);
    pose.right.lerp(tip.clone().addScaledVector(normal, .14), amount);
    pose.left.lerp(vector(-.76, 1.47, .40), amount); pose.body.x = THREE.MathUtils.lerp(pose.body.x, -.23, amount); pose.head.x = THREE.MathUtils.lerp(pose.head.x, -.18, amount);
  }
  if (clip.action === "sleeping") {
    const amount = smooth(0, 1.2, t) * (1 - smooth(clip.duration - 1.2, clip.duration, t));
    pose.body.x = THREE.MathUtils.lerp(-.06, -.67, amount);
    pose.head.x = THREE.MathUtils.lerp(-.06, -.21, amount); pose.head.z = .10 * amount;
    pose.left.lerp(vector(.06, 1.44, .57), amount); pose.right.lerp(vector(-.12, 1.46, .47), amount);
  } else if (clip.action === "phone") {
    const amount = smooth(0, 1, t) * (1 - smooth(clip.duration - 1, clip.duration, t));
    pose.phoneAmount = amount; pose.phone.lerp(vector(.44, 1.60, 1.05), amount);
    pose.right.lerp(vector(.49, 1.51, 1.13), amount);
    pose.left.lerp(vector(.35, 1.62 + Math.sin(t * 3.5) * .025, 1.09), amount);
    pose.body.z = -.05 * amount; pose.head.x = THREE.MathUtils.lerp(pose.head.x, -.21, amount); pose.head.y = THREE.MathUtils.lerp(pose.head.y, -.22, amount);
  } else if (clip.action === "stretching") {
    const amount = smooth(0, 1.3, t) * (1 - smooth(clip.duration - 1.3, clip.duration, t));
    pose.stretch = amount;
    // Arc around the shoulders instead of passing through the folded-arm
    // singularity on a straight path from lap to overhead.
    const outward = Math.sin(Math.PI * amount) * .52;
    pose.left.lerp(vector(-.18, 2.53, 1.41), amount); pose.left.x -= outward;
    pose.right.lerp(vector(.18, 2.53, 1.41), amount); pose.right.x += outward;
    pose.body.x = THREE.MathUtils.lerp(-.06, .10, amount); pose.body.z = Math.sin(t * 1.0) * .04 * amount;
    pose.head.x = THREE.MathUtils.lerp(-.06, .16, amount);
  } else if (clip.action === "drinking") {
    // Reach → lift → sip → replace → release. The original desk cup is moved;
    // no second cup is spawned and interruption smoothly returns it home.
    const pickup = smooth(1.1, 1.55, t) * (1 - smooth(5.4, 5.9, t));
    const carry = smooth(1.55, 2.55, t) * (1 - smooth(4.35, 5.4, t));
    const turn = smooth(1.55, 2.4, t) * (1 - smooth(4.35, 5.3, t));
    const reach = smooth(0, 1.1, t) * (1 - smooth(5.95, 7, t));
    pose.cup.y += .14 * pickup;
    pose.cup.lerp(vector(.28, 1.72, 1.03), carry);
    // The handle starts diagonally toward the seated person. Only after the
    // cup clears the table does the wrist turn inward, bringing the handle
    // to the person's right and the rim toward their mouth. Reverse on return.
    const yaw = THREE.MathUtils.lerp(-.9, Math.PI / 2, turn);
    const sip = .61 * smooth(2.55, 3.05, t) * (1 - smooth(3.85, 4.35, t));
    pose.cupRotation.setFromAxisAngle(UP, yaw)
      .multiply(new THREE.Quaternion().setFromAxisAngle(vector(1, 0, 0), -sip));
    const grip = vector(0, .105, .15).applyQuaternion(pose.cupRotation).add(pose.cup);
    pose.grip = reach;
    pose.right.lerp(grip, reach); pose.lean.x = .10 * reach * (1 - carry);
    pose.body.x = THREE.MathUtils.lerp(pose.body.x, -.10, reach); pose.head.x = THREE.MathUtils.lerp(pose.head.x, .06, carry);
    pose.head.y = THREE.MathUtils.lerp(pose.head.y, -.55, carry); pose.headSide = .035 * carry;
  }
  return pose;
}

function makeArm(parent: THREE.Group, side: number, cloth: THREE.MeshStandardMaterial, skin: THREE.MeshStandardMaterial) {
  const upper = segment(parent, .092, cloth), lower = segment(parent, .070, cloth);
  const elbow = ellipsoid(parent, [.085, .085, .085], [0, 0, 0], cloth);
  const hand = new THREE.Group(); hand.name = side < 0 ? "left-hand" : "right-hand";
  elbow.name = side < 0 ? "left-elbow" : "right-elbow"; parent.add(hand);
  ellipsoid(hand, [.077, .043, .09], [0, 0, 0], skin);
  const fingers = Array.from({ length: 4 }, (_, i) => {
    const finger = new THREE.Group(); finger.position.set((i - 1.5) * .032, -.005, -.05); hand.add(finger);
    ellipsoid(finger, [.017, .018, .055], [0, 0, -.037], skin);
    return finger;
  });
  ellipsoid(hand, [.029, .028, .055], [-side * .075, -.009, 0], skin);
  return { upper, lower, elbow, hand, fingers, target: vector(side * .24, 1.05, 1.16) };
}

function armJoints(shoulder: THREE.Vector3, target: THREE.Vector3, side: number, stretching: number, wristDirection?: THREE.Vector3) {
  const upperLength = .48, lowerLength = .52;
  const direction = target.clone().sub(shoulder);
  const distance = THREE.MathUtils.clamp(direction.length(), .08, upperLength + lowerLength - .005);
  direction.normalize();
  const end = shoulder.clone().addScaledVector(direction, distance);
  const projection = (upperLength ** 2 - lowerLength ** 2 + distance ** 2) / (2 * distance);
  const bendLength = Math.sqrt(Math.max(.001, upperLength ** 2 - projection ** 2));
  // Continuous pole weights avoid elbow flips when the hand passes the
  // chest or shoulders. Never switch IK bend planes at a height threshold.
  const hint = vector(side * .51, 1.17, 1.47);
  const deskWeight = 1 - smooth(.65, 1.0, target.z);
  const sleepingWeight = 1 - smooth(1.53, 1.68, shoulder.y);
  hint.lerp(vector(side * .58, THREE.MathUtils.lerp(1.18, 1.47, sleepingWeight), THREE.MathUtils.lerp(1.18, .62, sleepingWeight)), deskWeight);
  hint.lerp(vector(side * .40, 1.08, 1.70), 1 - smooth(1.16, 1.40, target.y));
  hint.lerp(shoulder.clone().add(vector(side * .85, .15, .18)), stretching);
  if (wristDirection) hint.copy(end).addScaledVector(wristDirection, -lowerLength);
  const bend = hint.sub(shoulder).addScaledVector(direction, -hint.dot(direction));
  if (bend.lengthSq() < .0001) bend.set(side, 0, 0);
  bend.normalize();
  const joint = shoulder.clone().addScaledVector(direction, projection).addScaledVector(bend, bendLength);
  return { joint, end };
}
function solveArm(arm: ReturnType<typeof makeArm>, shoulder: THREE.Vector3, target: THREE.Vector3, side: number, stretching: number, wristDirection?: THREE.Vector3, maxBendAngle = Infinity) {
  const { joint, end } = armJoints(shoulder, target, side, stretching, wristDirection);
  if (Number.isFinite(maxBendAngle)) {
    // The elbow lies on a circle around the shoulder→wrist axis. Rotate along
    // that circle instead of flipping IK poles when rising or releasing a cup.
    // Both bone lengths and the exact hand/prop contact remain unchanged.
    const axis = end.clone().sub(shoulder).normalize();
    const center = shoulder.clone().addScaledVector(axis, joint.clone().sub(shoulder).dot(axis));
    const desired = joint.clone().sub(center), radius = desired.length();
    const previous = arm.elbow.position.clone().sub(center);
    previous.addScaledVector(axis, -previous.dot(axis));
    if (previous.lengthSq() > .000001 && radius > .000001) {
      previous.normalize(); desired.normalize();
      const turn = new THREE.Quaternion().setFromUnitVectors(previous, desired);
      const angle = new THREE.Quaternion().angleTo(turn);
      previous.applyQuaternion(new THREE.Quaternion().slerp(turn, Math.min(1, maxBendAngle / Math.max(angle, .000001))));
      joint.copy(center).addScaledVector(previous, radius);
    }
  }
  placeSegment(arm.upper, shoulder, joint); placeSegment(arm.lower, joint, end);
  arm.elbow.position.copy(joint); arm.hand.position.copy(end);
}

export interface OfficeCharacter {
  group: THREE.Group;
  update: (delta: number, state: ScreenState, animate: boolean, override?: CharacterAction | null, previewTime?: number, hour?: number) => ActionSample;
}

export function createOfficeCharacter(role: DeskRole, desk: DeskModel, random: () => number = Math.random): OfficeCharacter {
  const group = new THREE.Group(); group.name = `character:${role}`; desk.add(group);
  const colors = characterAppearance[role];
  const cloth = material(colors.shirt, .94), hair = material(colors.hair, .94), skin = material(colors.skin, .82);
  const pants = material(0x5c6467, .98), shoes = material(0x42494b, .75), sole = material(0xe1e0d9, .8);
  const body = new THREE.Group(); body.position.set(0, .86, 1.34); group.add(body);
  box(body, [.66, .79, .39], [0, .43, 0], cloth, .16);
  // A quiet seam and ribbed collar read well from the rear without faces.
  box(body, [.025, .45, .009], [0, .42, .201], material(colors.shirt, 1), .004);
  if (role === "frontend" || role === "backend") {
    box(body, [.43, .17, .21], [0, .78, .15], material(new THREE.Color(colors.shirt).multiplyScalar(.87), .94), .075);
  } else {
    box(body, [.36, .065, .29], [0, .84, 0], material(0xe3ded3, .95), .028);
  }
  cylinder(body, .084, .15, [0, .91, -.01], skin);
  const head = new THREE.Group(); head.position.set(0, 1.105, -.035); body.add(head);
  ellipsoid(head, [.265, .31, .255], [0, 0, 0], skin);
  for (const side of [-1, 1]) ellipsoid(head, [.052, .077, .045], [side * .262, -.035, 0], skin);
  const cap = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 16, 0, Math.PI * 2, 0, Math.PI * .59), hair);
  cap.scale.set(.28, .32, .272); cap.position.y = .035; cap.castShadow = true; cap.receiveShadow = true; head.add(cap);
  ellipsoid(head, [.256, .19, .15], [0, -.10, .12], hair);
  if (colors.style === "bob") {
    box(head, [.57, .49, .32], [0, -.04, .11], hair, .15);
  } else if (colors.style === "bun") {
    ellipsoid(head, [.145, .15, .14], [0, .31, .16], hair);
    const tie = new THREE.Mesh(new THREE.TorusGeometry(.098, .021, 8, 20), material(0xc6b4d2));
    tie.position.set(0, .24, .19); tie.rotation.x = -.6; head.add(tie);
  } else if (colors.style === "curly") {
    for (let i = 0; i < 6; i++) ellipsoid(head, [.12, .11, .13], [Math.sin(i * 2.4) * .17, .25 + (i % 2) * .035, Math.cos(i * 2.4) * .13], hair);
  } else if (colors.style === "headphones") {
    const band = new THREE.Mesh(new THREE.TorusGeometry(.302, .028, 8, 28, Math.PI), material(0x515d6a));
    band.position.set(0, .015, 0); band.castShadow = true; head.add(band);
    for (const side of [-1, 1]) ellipsoid(head, [.056, .10, .081], [side * .287, -.015, 0], material(0xabb7c7));
  } else if (colors.style === "ponytail") {
    ellipsoid(head, [.11, .20, .11], [0, -.10, .30], hair);
    ellipsoid(head, [.095, .04, .085], [0, .02, .29], material(0xa9b5a1));
  } else {
    const sweep = ellipsoid(head, [.255, .105, .22], [-.035, .273, -.015], hair); sweep.rotation.z = -.18;
  }
  for (const side of [-1, 1]) {
    const thigh = segment(group, .12, pants), shin = segment(group, .085, pants);
    const hip = vector(side * .17, .86, 1.34), knee = vector(side * .20, .59, .81), ankle = vector(side * .20, .17, .80);
    placeSegment(thigh, hip, knee); placeSegment(shin, knee, ankle);
    ellipsoid(group, [.119, .045, .22], [side * .20, .081, .69], sole);
    ellipsoid(group, [.11, .074, .205], [side * .20, .13, .69], shoes);
  }
  const left = makeArm(group, -1, cloth, skin), right = makeArm(group, 1, cloth, skin);
  const phone = new THREE.Group(); group.add(phone); phone.rotation.x = -.36;
  box(phone, [.185, .32, .026], [0, 0, 0], material(0x3e454d, .45), .026);
  box(phone, [.158, .275, .007], [0, .005, .017], material(0xbac6ce, .68), .018);
  for (let i = 0; i < 3; i++) box(phone, [.123, .037, .003], [0, .079 - i * .073, .022], material([0xe3d5c6, 0xc0cdb9, 0xe4e5e2][i]), .007);

  let elapsed = 0, clock = Object.keys(characterAppearance).indexOf(role) * 1.73, previousState: ScreenState | undefined, previousOverride: CharacterAction | null = null;
  const behavior = createCharacterBehavior(role, random);
  let lastPose: Pose | undefined, transitionPose: Pose | undefined, transitionTime = 0;
  let initialized = false;
  let stretchAmount = 0;
  let previousPreviewTime: number | undefined;
  const workingCupRotation = desk.interaction.cupHomeRotation.clone();
  const reference = samplePose(desk, { action: "drinking", input: "none", time: 1.1, duration: 7 }, 0);
  const referenceShoulder = vector(.32, .77, 0).applyEuler(reference.body).add(reference.lean);
  const referenceArm = armJoints(referenceShoulder, reference.right, 1, 0);
  const neutralWrist = new THREE.Quaternion().setFromUnitVectors(vector(0, 0, -1), referenceArm.end.clone().sub(referenceArm.joint).normalize());
  // A fixed grip in cup-local space: the palm and handle rotate together.
  const gripRotation = reference.cupRotation.clone().invert().multiply(neutralWrist);

  function update(delta: number, state: ScreenState, animate: boolean, override: CharacterAction | null = null, previewTime?: number, hour = 10) {
    const changedMode = previousState !== state || previousOverride !== override;
    const changedState = changedMode || previousPreviewTime !== previewTime;
    if (previousOverride && !override) behavior.reset();
    previousPreviewTime = previewTime;
    if (changedMode) { elapsed = 0; previousState = state; previousOverride = override; }
    if (animate) { elapsed += delta; clock += delta; }
    const clip = override || previewTime !== undefined ? sampleAction(role, state, previewTime ?? elapsed, override)
      : behavior.update(animate ? delta : 0, state, hour);
    // Live interruptions blend from the last pose; paused scrubbing stays exact.
    if (changedMode && animate && lastPose) { transitionPose = lastPose; transitionTime = 0; }
    if (!animate) transitionPose = undefined;
    // Preserve the exact frozen pose when paused. Status changes still replace
    // the pose, so a running agent cannot remain asleep after resume/state sync.
    if (initialized && !animate && !changedState) return clip;
    const pose = samplePose(desk, clip, previewTime ?? clock);
    if (role === "product" && clip.input === "keyboard") {
      const amount = actionEnvelope(clip.time, clip.duration);
      pose.lean.z = THREE.MathUtils.lerp(pose.lean.z, 1.28, amount); pose.body.x = THREE.MathUtils.lerp(pose.body.x, -.22, amount);
    }
    if (transitionPose) {
      transitionTime += delta;
      const amount = easeAction(transitionTime / .9);
      for (const key of ["lean", "left", "right", "cup", "phone", "pen", "mouse"] as const) pose[key].lerpVectors(transitionPose[key], pose[key], amount);
      for (const key of ["cupRotation", "penRotation"] as const) pose[key].slerpQuaternions(transitionPose[key], pose[key], amount);
      for (const key of ["body", "head"] as const) pose[key].setFromQuaternion(new THREE.Quaternion().setFromEuler(transitionPose[key]).slerp(new THREE.Quaternion().setFromEuler(pose[key]), amount));
      for (const key of ["headSide", "grip", "stretch", "phoneAmount", "penScale"] as const) pose[key] = THREE.MathUtils.lerp(transitionPose[key], pose[key], amount);
      if (transitionTime >= .9) transitionPose = undefined;
    }
    lastPose = pose;
    const blend = !initialized || !animate ? 1 : 1 - Math.exp(-delta * 12);
    stretchAmount = THREE.MathUtils.lerp(stretchAmount, pose.stretch, blend);
    body.position.lerp(pose.lean, blend);
    body.quaternion.slerp(new THREE.Quaternion().setFromEuler(pose.body), blend);
    head.quaternion.slerp(new THREE.Quaternion().setFromEuler(pose.head), blend);
    head.position.x = THREE.MathUtils.lerp(head.position.x, pose.headSide, blend);
    left.target.lerp(pose.left, blend); right.target.lerp(pose.right, blend);
    desk.interaction.cup.position.lerp(pose.cup, blend);
    workingCupRotation.slerp(pose.cupRotation, blend); desk.interaction.cup.quaternion.copy(workingCupRotation);
    const fixedGrip = clip.action === "drinking" && clip.time >= 1.1 && clip.time <= 5.95;
    if (fixedGrip) right.target.copy(vector(0, .105, .15).applyQuaternion(workingCupRotation).add(desk.interaction.cup.position));
    const shoulder = (side: number) => vector(side * .32, .77, 0).applyQuaternion(body.quaternion).add(body.position);
    const holdingCup = clip.action === "drinking" && clip.time > .7 && clip.time < 6.6;
    const cupWrist = workingCupRotation.clone().multiply(gripRotation);
    const wristDirection = holdingCup ? vector(0, 0, -1).applyQuaternion(cupWrist) : undefined;
    const maxBendAngle = initialized && animate ? delta * 4 : Infinity;
    solveArm(left, shoulder(-1), left.target, -1, stretchAmount, undefined, maxBendAngle);
    solveArm(right, shoulder(1), right.target, 1, stretchAmount, wristDirection, maxBendAngle);
    for (const [index, arm] of [left, right].entries()) {
      const gripping = index === 1 && holdingCup;
      const handRotation = new THREE.Quaternion();
      if (gripping) handRotation.copy(cupWrist);
      else if (stretchAmount > 0) {
        // Ease from the resting palm to forearm alignment and back.
        const forearm = arm.hand.position.clone().sub(arm.elbow.position).normalize();
        handRotation.slerp(new THREE.Quaternion().setFromUnitVectors(vector(0, 0, -1), forearm), smooth(0, .65, stretchAmount));
      } else handRotation.setFromEuler(new THREE.Euler(clip.action === "phone" ? -.3 * pose.phoneAmount : 0, 0, 0));
      if (index === 1 && fixedGrip) arm.hand.quaternion.copy(handRotation);
      else arm.hand.quaternion.slerp(handRotation, blend);
      if (index === 1) {
        const forward = vector(0, 0, -1).applyQuaternion(arm.hand.quaternion);
        const forearm = arm.hand.position.clone().sub(arm.elbow.position).normalize();
        group.userData.wristAngle = THREE.MathUtils.radToDeg(Math.acos(THREE.MathUtils.clamp(forward.dot(forearm), -1, 1)));
      }
      arm.fingers.forEach((finger, i) => {
        finger.rotation.x = (clip.input === "keyboard" || (clip.input === "keyboard-mouse" && index === 0)) ? Math.max(0, Math.sin(clock * 19 + i * 1.5 + index)) * .3
          : clip.input === "keyboard-mouse" && index === 1 && i < 2 ? Math.max(0, Math.sin(clock * 11 + i)) * .12
          : gripping && clip.time > 1.1 && clip.time < 5.95 ? .72 : 0;
        if (clip.action === "gaming") finger.rotation.x *= actionEnvelope(clip.time, clip.duration);
      });
    }
    phone.visible = pose.phoneAmount > .025;
    phone.position.lerp(pose.phone, blend);
    if (desk.interaction.mouse) {
      desk.interaction.mouse.position.lerp(pose.mouse, blend);
    }
    if (desk.interaction.pen) {
      desk.interaction.pen.position.lerp(pose.pen, blend); desk.interaction.pen.quaternion.slerp(pose.penRotation, blend);
      desk.interaction.pen.scale.y = THREE.MathUtils.lerp(desk.interaction.pen.scale.y, pose.penScale, blend);
    }
    group.userData.reachError = Math.max(left.hand.position.distanceTo(left.target), right.hand.position.distanceTo(right.target));
    group.userData.action = clip.action; group.userData.input = clip.input;
    initialized = true;
    return clip;
  }
  return { group, update };
}
