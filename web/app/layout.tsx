import type { Metadata } from "next";

/* Self-hosted variable fonts (no external CDN needed at build time). */
import "@fontsource-variable/inter";
import "@fontsource-variable/newsreader/standard.css";
import "@fontsource-variable/newsreader/standard-italic.css";
import "@fontsource-variable/jetbrains-mono";

import "../styles/tokens.css";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Nimna — Agent Workspace",
    template: "%s · Nimna",
  },
  description:
    "A Manus × Claude inspired workspace for the Nimna reusable-skills agent.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
