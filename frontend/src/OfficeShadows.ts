import * as THREE from "three";
import { FullScreenQuad } from "three/addons/postprocessing/Pass.js";

/** Bake an area-light floor shadow once. Sampling the light position preserves
 * crisp contact at chair wheels while softening the higher desk silhouettes.
 * This work never repeats when a screen animates or an agent changes status. */
export function bakeFloorShadow(renderer: THREE.WebGLRenderer, scene: THREE.Scene, light: THREE.DirectionalLight) {
  const sample = new THREE.WebGLRenderTarget(768, 1024, { type: THREE.HalfFloatType });
  const accumulation = sample.clone();
  const camera = new THREE.OrthographicCamera(-8, 8, 10.5, -10.5, .1, 40);
  camera.position.set(0, 20, -.25); camera.up.set(0, 0, -1); camera.lookAt(0, 0, -.25);
  const receiverMaterial = new THREE.ShadowMaterial({ color: 0x000000, opacity: 1 });
  receiverMaterial.allowOverride = false;
  const receiver = new THREE.Mesh(new THREE.PlaneGeometry(16, 21), receiverMaterial);
  receiver.rotation.x = -Math.PI / 2; receiver.position.set(0, -.035, -.25); receiver.receiveShadow = true;
  const discard = new THREE.ShaderMaterial({
    vertexShader: "void main() { gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.); }",
    fragmentShader: "void main() { discard; }",
  });
  const samples = 32;
  const blend = new THREE.ShaderMaterial({
    uniforms: { source: { value: sample.texture }, weight: { value: 1 / samples } },
    vertexShader: "varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0., 1.); }",
    fragmentShader: "uniform sampler2D source; uniform float weight; varying vec2 vUv; void main() { gl_FragColor = vec4(texture2D(source, vUv).rgb * weight, 1.); }",
    blending: THREE.AdditiveBlending, depthTest: false, depthWrite: false,
  });
  const quad = new FullScreenQuad(blend);
  const originalPosition = light.position.clone();
  const originalClear = renderer.getClearColor(new THREE.Color());
  const originalAlpha = renderer.getClearAlpha();
  const originalTarget = renderer.getRenderTarget();
  const originalAutoClear = renderer.autoClear;
  const originalOverride = scene.overrideMaterial;
  const originalToneMapping = renderer.toneMapping;
  scene.add(receiver);
  scene.overrideMaterial = discard;
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.setRenderTarget(accumulation); renderer.setClearColor(0x000000, 1); renderer.clear();
  try {
    for (let i = 0; i < samples; i++) {
      const angle = i * Math.PI * (3 - Math.sqrt(5));
      const radius = 4.8 * Math.sqrt((i + .5) / samples);
      light.position.copy(originalPosition).add(new THREE.Vector3(Math.cos(angle) * radius, 0, Math.sin(angle) * radius));
      renderer.shadowMap.needsUpdate = true;
      renderer.autoClear = true;
      renderer.setClearColor(0xffffff, 1); renderer.setRenderTarget(sample);
      renderer.render(scene, camera);
      renderer.autoClear = false; renderer.setRenderTarget(accumulation); quad.render(renderer);
    }
  } finally {
    scene.remove(receiver); scene.overrideMaterial = originalOverride;
    light.position.copy(originalPosition); renderer.shadowMap.needsUpdate = true;
    renderer.setClearColor(originalClear, originalAlpha); renderer.setRenderTarget(originalTarget);
    renderer.autoClear = originalAutoClear; renderer.toneMapping = originalToneMapping;
    receiver.geometry.dispose(); receiverMaterial.dispose(); discard.dispose();
    sample.dispose(); blend.dispose(); quad.dispose();
  }
  const shadowMaterial = new THREE.ShaderMaterial({
    uniforms: { shadowMap: { value: accumulation.texture } },
    vertexShader: "varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.); }",
    fragmentShader: "uniform sampler2D shadowMap; varying vec2 vUv; void main() { float shadow = 1. - texture2D(shadowMap, vUv).r; vec2 edge = smoothstep(vec2(0.), vec2(.065), vUv) * smoothstep(vec2(0.), vec2(.065), 1. - vUv); gl_FragColor = vec4(vec3(.18, .20, .19), shadow * .66 * edge.x * edge.y); }",
    transparent: true, depthWrite: false,
  });
  const shadow = new THREE.Mesh(new THREE.PlaneGeometry(16, 21), shadowMaterial);
  shadow.name = "baked-area-shadow";
  shadow.rotation.x = -Math.PI / 2; shadow.position.set(0, -.036, -.25);
  scene.add(shadow);
  return () => { accumulation.dispose(); };
}
