import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, join } from "node:path";

const cargoHome = process.env.CARGO_HOME || join(homedir(), ".cargo");
const cargoBin = join(cargoHome, "bin");
const cargoName = process.platform === "win32" ? "cargo.exe" : "cargo";

if (!existsSync(join(cargoBin, cargoName))) {
  console.error(`Cargo was not found at ${cargoBin}. Install Rust with rustup or set CARGO_HOME.`);
  process.exit(1);
}

const child = spawn("tauri", ["dev"], {
  stdio: "inherit",
  shell: process.platform === "win32",
  env: {
    ...process.env,
    PATH: `${cargoBin}${delimiter}${process.env.PATH || ""}`,
  },
});

child.on("error", (error) => {
  console.error(`Could not start Tauri: ${error.message}`);
  process.exitCode = 1;
});

child.on("exit", (code) => {
  process.exitCode = code ?? 1;
});
