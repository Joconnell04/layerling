import type { Metadata, Viewport } from "next";
import { GeistMono } from "geist/font/mono";
import "@fontsource-variable/fraunces/full.css";
import "@fontsource-variable/fraunces/full-italic.css";
import "@fontsource-variable/instrument-sans/index.css";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "layerling", template: "%s | layerling" },
  description: "Your pet, printed in color: keychains, nameplates, figures, ornaments and magnets from a photo.",
};

export const viewport: Viewport = {
  themeColor: "#f3eee4",
  colorScheme: "light",
};

// Set before paint so motion can start hidden only when JavaScript runs. If the site's motion
// script has not claimed it within 4 s (a failed bundle), the class comes off and everything shows.
const JS_FLAG =
  "document.documentElement.classList.add('js');setTimeout(function(){if(!window.__llMotion)document.documentElement.classList.remove('js')},4000)";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={GeistMono.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: JS_FLAG }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
