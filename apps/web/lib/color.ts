// sRGB hex -> CIELAB (D65, 2 degree observer), per contracts/filaments.py. Distances are CIEDE2000.

export type Lab = [number, number, number];

const HEX_RE = /^#?([0-9a-f]{6})$/i;

export function isHex(hex: string): boolean {
  return HEX_RE.test(hex.trim());
}

export function normalizeHex(hex: string): string {
  const m = HEX_RE.exec(hex.trim());
  if (!m) throw new Error(`Bad hex: ${hex}`);
  return `#${m[1].toUpperCase()}`;
}

export function hexToRgb(hex: string): [number, number, number] {
  const h = normalizeHex(hex).slice(1);
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as [number, number, number];
}

function srgbToLinear(c8: number): number {
  const c = c8 / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

// D65 reference white, Y normalized to 1 (IEC 61966-2-1 matrix).
const XN = 0.95047;
const YN = 1.0;
const ZN = 1.08883;

function f(t: number): number {
  const d = 6 / 29;
  return t > d ** 3 ? Math.cbrt(t) : t / (3 * d * d) + 4 / 29;
}

export function hexToLab(hex: string): Lab {
  const [r, g, b] = hexToRgb(hex).map(srgbToLinear);
  const x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b;
  const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b;
  const z = 0.0193339 * r + 0.119192 * g + 0.9503041 * b;
  const fx = f(x / XN);
  const fy = f(y / YN);
  const fz = f(z / ZN);
  const L = 116 * fy - 16;
  const a = 500 * (fx - fy);
  const bb = 200 * (fy - fz);
  return [round2(L), round2(a), round2(bb)];
}

function round2(v: number): number {
  const r = Math.round(v * 100) / 100;
  return Object.is(r, -0) ? 0 : r;
}

export function formatLab(lab: Lab): string {
  return lab.map((v) => v.toFixed(1)).join(" ");
}

// CIEDE2000 (Sharma, Wu, Dalal 2005).
export function deltaE2000(lab1: Lab, lab2: Lab): number {
  const [L1, a1, b1] = lab1;
  const [L2, a2, b2] = lab2;
  const rad = Math.PI / 180;
  const C1 = Math.hypot(a1, b1);
  const C2 = Math.hypot(a2, b2);
  const Cbar = (C1 + C2) / 2;
  const G = 0.5 * (1 - Math.sqrt(Cbar ** 7 / (Cbar ** 7 + 25 ** 7)));
  const a1p = (1 + G) * a1;
  const a2p = (1 + G) * a2;
  const C1p = Math.hypot(a1p, b1);
  const C2p = Math.hypot(a2p, b2);
  const hp = (b: number, ap: number) => {
    if (b === 0 && ap === 0) return 0;
    const h = Math.atan2(b, ap) / rad;
    return h >= 0 ? h : h + 360;
  };
  const h1p = hp(b1, a1p);
  const h2p = hp(b2, a2p);
  const dLp = L2 - L1;
  const dCp = C2p - C1p;
  let dhp = 0;
  if (C1p * C2p !== 0) {
    dhp = h2p - h1p;
    if (dhp > 180) dhp -= 360;
    else if (dhp < -180) dhp += 360;
  }
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin((dhp / 2) * rad);
  const Lbarp = (L1 + L2) / 2;
  const Cbarp = (C1p + C2p) / 2;
  let hbarp = h1p + h2p;
  if (C1p * C2p !== 0) {
    if (Math.abs(h1p - h2p) <= 180) hbarp = (h1p + h2p) / 2;
    else if (h1p + h2p < 360) hbarp = (h1p + h2p + 360) / 2;
    else hbarp = (h1p + h2p - 360) / 2;
  }
  const T =
    1 -
    0.17 * Math.cos((hbarp - 30) * rad) +
    0.24 * Math.cos(2 * hbarp * rad) +
    0.32 * Math.cos((3 * hbarp + 6) * rad) -
    0.2 * Math.cos((4 * hbarp - 63) * rad);
  const dTheta = 30 * Math.exp(-(((hbarp - 275) / 25) ** 2));
  const Rc = 2 * Math.sqrt(Cbarp ** 7 / (Cbarp ** 7 + 25 ** 7));
  const Sl = 1 + (0.015 * (Lbarp - 50) ** 2) / Math.sqrt(20 + (Lbarp - 50) ** 2);
  const Sc = 1 + 0.045 * Cbarp;
  const Sh = 1 + 0.015 * Cbarp * T;
  const Rt = -Math.sin(2 * dTheta * rad) * Rc;
  return Math.sqrt(
    (dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh),
  );
}

/** Relative luminance, for picking readable text on a swatch. */
export function isLight(hex: string): boolean {
  return hexToLab(hex)[0] > 60;
}

// Filament id slug: <brand>-<material>-<line>-<color>, e.g. bambu-pla-basic-black.
// Brand keeps its first word; the material token is dropped from the line.
export function filamentSlug(brand: string, material: string, line: string, name: string): string {
  const words = (s: string) =>
    s
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[^a-z0-9+]+/g, " ")
      .replace(/\+/g, " plus ")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
  const mat = words(material);
  const brandWords = words(brand).slice(0, 1);
  const lineWords = words(line).filter((w) => !mat.includes(w));
  return [...brandWords, ...mat, ...lineWords, ...words(name)].join("-");
}
