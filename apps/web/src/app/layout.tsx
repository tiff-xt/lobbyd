import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Header } from "@/components/Header";
import { PointerLight } from "@/components/PointerLight";
// Fonts ship with the app (open-licensed files from npm), so the build needs
// no network access and pages make no requests to a font service.
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@fontsource/chakra-petch/600.css";
import "@fontsource/chakra-petch/700.css";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: { default: "lobbyd: tournament tickets", template: "%s · lobbyd" },
  description:
    "Demo client for lobbyd, a ticketing backend for gaming tournaments and LAN events.",
};

export const viewport: Viewport = { themeColor: "#09090a" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="antialiased">
      <body>
        <Providers>
          <PointerLight />
          <Header />
          <main className="relative z-[2] mx-auto w-full max-w-6xl px-4 pb-24 pt-8 sm:px-6">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
