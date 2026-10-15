import { ImageResponse } from "next/og";

export const size = { width: 180, height: 180 };
export const contentType = "image/png";

// Apple touch icon: brick square with the cream layered mark (the "app" variant on the canvas).
const lines = Array.from({ length: 10 }, (_, i) => `<rect x="0" y="${20 + i * 8}" width="120" height="5"/>`).join("");
const mark = `<svg xmlns="http://www.w3.org/2000/svg" width="132" height="132" viewBox="0 0 120 120"><defs><clipPath id="h"><circle cx="60" cy="54" r="31"/><ellipse cx="28" cy="62" rx="12" ry="25" transform="rotate(14 28 62)"/><ellipse cx="92" cy="62" rx="12" ry="25" transform="rotate(-14 92 62)"/><ellipse cx="60" cy="76" rx="21" ry="17"/></clipPath></defs><g clip-path="url(#h)" fill="#FFF8EE">${lines}</g><circle cx="48" cy="53" r="5" fill="#B83C24"/><circle cx="72" cy="53" r="5" fill="#B83C24"/><ellipse cx="60" cy="71" rx="8" ry="6" fill="#B83C24"/></svg>`;

export default function AppleIcon() {
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center", background: "#B83C24" }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={`data:image/svg+xml;base64,${Buffer.from(mark).toString("base64")}`} width={132} height={132} alt="" />
      </div>
    ),
    size,
  );
}
