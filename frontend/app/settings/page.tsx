"use client";
import { useEffect, useState } from "react";
import { api, MetricsResponse } from "@/lib/api";

export default function Settings() {
  const [m, setM] = useState<MetricsResponse | null>(null);
  useEffect(() => { api<MetricsResponse>("/api/v1/metrics", "admin").then(setM).catch(() => setM(null)); }, []);

  return (
    <main className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Configuración</h1>
      <section className="rounded border p-4">
        <h2 className="font-medium">Consumo</h2>
        <p className="text-sm text-slate-600">
          {m ? `${Math.round(m.usage.audio_seconds / 60)} min de audio · coste estimado $${m.usage.estimated_cost_usd.toFixed(2)}` : "Cargando…"}
        </p>
      </section>
      <section className="rounded border p-4 text-sm text-slate-600">
        <h2 className="font-medium text-slate-900">Privacidad y retención</h2>
        <p>Las transcripciones y el audio solo se almacenan con consentimiento explícito del usuario y según el periodo de retención de la organización (30 días por defecto). El audio no se persiste; solo se guardan transcripciones, métricas y auditoría.</p>
      </section>
      <section className="rounded border p-4 text-sm text-slate-600">
        <h2 className="font-medium text-slate-900">Límites</h2>
        <p>Los límites de sesiones concurrentes, peticiones por minuto y duración máxima de sesión se configuran en el backend mediante variables de entorno (ver <code>.env.example</code>).</p>
      </section>
    </main>
  );
}
