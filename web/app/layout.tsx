import type { Metadata } from "next";

/* Self-hosted variable fonts (no external CDN needed at build time).
   Inter → Latin, Cairo → Arabic, Newsreader → display Latin. */
import "@fontsource-variable/inter";
import "@fontsource-variable/cairo";
import "@fontsource-variable/newsreader/standard.css";
import "@fontsource-variable/newsreader/standard-italic.css";
import "@fontsource-variable/jetbrains-mono";

import "../styles/tokens.css";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "نِمنا — مساحة عمل الوكيل",
    template: "%s · نِمنا",
  },
  description:
    "مساحة عمل مستوحاة من Manus × Claude لوكيل نِمنا القابل لإعادة استخدام المهارات.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ar" dir="rtl">
      <body>{children}</body>
    </html>
  );
}
