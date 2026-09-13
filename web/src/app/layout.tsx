import type { Metadata, Viewport } from "next";
import "./globals.css";

// No webfont is loaded on purpose. This terminal's whole type voice is the
// platform's own mono at tabular-nums, which is already installed, renders
// identically on every reload, and costs zero network on a screen that is
// open all day. A downloaded face would buy nothing and add a flash.

export const metadata: Metadata = {
  title: "EdgeAfterCosts",
  description:
    "Quantitative market research and execution-simulation terminal — paper only, no real order is ever submitted.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#080a0e",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
