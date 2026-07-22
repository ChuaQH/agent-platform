import type { Metadata } from "next";
import { AuthProvider } from "@/lib/auth";
import "./globals.css";

export const metadata: Metadata = {
  title: "Assistant Agent",
  description: "AI Assistant powered by DeepSeek",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <AuthProvider>
      <html lang="en" className="h-dvh dark">
        <body className="h-dvh bg-background font-sans text-foreground antialiased">
          {children}
        </body>
      </html>
    </AuthProvider>
  );
}
