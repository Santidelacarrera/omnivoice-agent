import "./globals.css";
import Link from "next/link";
import type { ReactNode } from "react";

export const metadata = { title: "OmniVoice Agent", description: "Agentes de voz en tiempo real" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="es">
      <body className="bg-white text-slate-900">
        <nav className="flex gap-4 border-b p-4 text-sm">
          <Link href="/conversation">Conversación</Link>
          <Link href="/dashboard">Panel</Link>
        </nav>
        {children}
      </body>
    </html>
  );
}
