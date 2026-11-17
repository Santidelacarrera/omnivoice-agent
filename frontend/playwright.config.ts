import { defineConfig } from "@playwright/test";

// Pruebas de interfaz contra un backend real en memoria (con el token de desarrollo) y el frontend compilado.
const API_PORT = 8001;
const WEB_PORT = 3011;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false, // comparten el backend en memoria
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    {
      command: `python -m uvicorn app.main:app --port ${API_PORT}`,
      cwd: "../backend",
      url: `http://localhost:${API_PORT}/healthz`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: {
        ENVIRONMENT: "development", PERSISTENCE_BACKEND: "memory", STATE_BACKEND: "memory",
        CORS_ORIGINS: JSON.stringify([`http://localhost:${WEB_PORT}`]),
        OUTBOUND_SCHEDULER_ENABLED: "false", WEBHOOK_WORKER_ENABLED: "false",
        TELEPHONY_PROVIDER: "twilio", TWILIO_ACCOUNT_SID: "ACe2e", TWILIO_AUTH_TOKEN: "e2e-token", TELEPHONY_PUBLIC_URL: "https://voz.example.com",
        WEBHOOKS_ALLOW_PRIVATE: "true", // solo para estas pruebas: permite http://localhost sin depender de DNS externo
      },
    },
    {
      command: `npm run build && npx next start -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      env: { NEXT_PUBLIC_API_URL: `http://localhost:${API_PORT}` },
    },
  ],
});
