import type { DeskRole } from "./DeskScene";

// Shared by the office characters and their UI portraits.
export const characterAppearance: Record<DeskRole, { shirt: number; hair: number; skin: number; style: "swept" | "bob" | "bun" | "curly" | "headphones" | "ponytail" }> = {
  leader: { shirt: 0x6f8177, hair: 0x514a43, skin: 0xdac0a9, style: "swept" },
  product: { shirt: 0xb6a389, hair: 0x685448, skin: 0xe2c5ad, style: "bob" },
  designer: { shirt: 0xa194b4, hair: 0x574843, skin: 0xd6b69a, style: "bun" },
  frontend: { shirt: 0x7d9c93, hair: 0x594e43, skin: 0xe0c0a4, style: "curly" },
  backend: { shirt: 0x8194ae, hair: 0x363f48, skin: 0xc9ad97, style: "headphones" },
  testing: { shirt: 0xc4b895, hair: 0x56534b, skin: 0xe2c7b2, style: "ponytail" },
};
