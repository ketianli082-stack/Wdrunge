/** Compact decimal units; promote values that round across a unit boundary. */
export function formatTokens(value: number): string {
  const count = Math.max(0, Number.isFinite(value) ? value : 0);
  if (count < 999.95) return Math.floor(count).toString();
  const units = ["k", "m", "b", "t"];
  let scaled = count / 1000;
  let unit = 0;
  while (Math.round(scaled * 10) / 10 >= 1000 && unit < units.length - 1) {
    scaled /= 1000;
    unit += 1;
  }
  return `${scaled.toFixed(1)}${units[unit]}`;
}
