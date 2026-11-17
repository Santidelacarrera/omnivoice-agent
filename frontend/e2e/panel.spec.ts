import { expect, request as pwRequest, test, APIRequestContext } from "@playwright/test";

const API = "http://localhost:8001";
const ORG = "00000000-0000-0000-0000-000000000001";

async function admin(): Promise<APIRequestContext> {
  const anon = await pwRequest.newContext({ baseURL: API });
  const r = await anon.post("/api/v1/auth/dev-token", { data: { user_id: "e2e", org_id: ORG, role: "admin" } });
  const { token } = await r.json();
  return pwRequest.newContext({ baseURL: API, extraHTTPHeaders: { Authorization: `Bearer ${token}` } });
}

test("asistente de 9 pasos: valida cada paso, crea el borrador y publica", async ({ page }) => {
  await page.goto("/agents/new");
  await expect(page.getByRole("heading", { name: "Crear un agente" })).toBeVisible();
  const steps = page.getByRole("list", { name: "Pasos" }).getByRole("listitem");
  await expect(steps).toHaveCount(9);

  // paso 1: no avanza sin nombre ni objetivo
  await page.getByRole("button", { name: "Siguiente" }).click();
  await expect(page.locator('p[role="alert"]')).toContainText("nombre");
  await page.getByLabel("Nombre del agente").fill("Recepción e2e");
  await page.getByLabel("Objetivo del agente").fill("Agendar citas y responder preguntas frecuentes.");
  await page.getByRole("button", { name: "Siguiente" }).click();
  await expect(steps.nth(1)).toHaveAttribute("aria-current", "step");

  // pasos 2 y 3
  await page.getByRole("button", { name: "Siguiente" }).click();
  await page.getByLabel(/Tareas que puede hacer/).fill("Agendar citas\nResponder horarios");
  await page.getByLabel(/Restricciones/).fill("Dar diagnósticos.");
  await page.getByText(/Instrucciones resultantes/).click();
  await expect(page.locator("pre")).toContainText("Dar diagnósticos.");
  await page.getByRole("button", { name: "Siguiente" }).click();

  // pasos 4 (aviso honesto) y 5 (herramientas)
  await expect(page.getByText(/search_knowledge_base/).first()).toBeVisible();
  await page.getByRole("button", { name: "Siguiente" }).click();
  await expect(page.getByLabel("check_inventory")).toBeVisible();
  await page.getByLabel("check_inventory").check();
  await page.getByRole("button", { name: "Siguiente" }).click();

  // pasos 6 y 7
  await expect(page.getByText(/aún no existen/)).toBeVisible();
  await page.getByRole("button", { name: "Siguiente" }).click();
  await page.getByRole("button", { name: "Siguiente" }).click();

  // paso 8: sin clave de IA, el simulador avisa pero el borrador queda guardado
  await page.getByRole("button", { name: "Guardar borrador y probar" }).click();
  await expect(page.getByRole("status")).toContainText("simulador no está activado");
  await page.getByRole("button", { name: "Siguiente" }).click();

  // paso 9: publicar
  await expect(page.getByText(/Resumen:/)).toContainText("Recepción e2e");
  await page.getByRole("button", { name: "Publicar agente" }).click();
  await expect(page.getByRole("status")).toContainText("Agente publicado");

  const api = await admin();
  const { items } = await (await api.get("/api/v1/agents")).json();
  const mine = items.find((a: { name: string }) => a.name === "Recepción e2e");
  expect(mine.status).toBe("published");
  expect(mine.tools).toEqual(expect.arrayContaining(["check_inventory", "transfer_to_human", "end_call"]));
  expect(mine.instructions).toContain("Dar diagnósticos.");
});

test("números: valida el formato, añade, asigna agente y elimina", async ({ page }) => {
  const api = await admin();
  const a = await (await api.post("/api/v1/agents", { data: { name: "Agente líneas", instructions: "Atiende llamadas con amabilidad.", tools: [] } })).json();
  await page.goto("/phones");
  await page.getByLabel("Número", { exact: true }).fill("12345");
  await page.getByRole("button", { name: "Añadir número" }).click();
  await expect(page.locator('p[role="alert"]')).toContainText("formato internacional");

  await page.getByLabel("Número", { exact: true }).fill("+56912340001");
  await page.getByLabel("Agente que atiende").selectOption({ label: "Agente líneas" });
  await page.getByRole("button", { name: "Añadir número" }).click();
  await expect(page.getByText("+56912340001")).toBeVisible();
  await expect(page.getByText("Agente líneas · operador global")).toBeVisible();

  const { items } = await (await api.get("/api/v1/phone-numbers")).json();
  expect(items.find((p: { e164: string }) => p.e164 === "+56912340001").agent_id).toBe(a.id);

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Eliminar" }).first().click();
  await expect(page.getByText("+56912340001")).toHaveCount(0);
});

test("salientes: lista de exclusión y exigencia de consentimiento", async ({ page }) => {
  await page.goto("/outbound");
  await page.getByLabel("Número a llamar").fill("+56987650001");
  await page.getByLabel("Base de consentimiento").selectOption("explicit_opt_in");
  await page.getByRole("button", { name: "Programar" }).click();
  await expect(page.getByRole("status")).toContainText("dio su consentimiento"); // sin indicar cómo consintió no se envía

  await page.getByLabel("Número a excluir").fill("+56987650009");
  await page.getByRole("button", { name: "Excluir" }).click();
  await expect(page.getByRole("status")).toContainText("excluido");
  await expect(page.getByText(/ref\. [0-9a-f]{10}/)).toBeVisible();
  await expect(page.getByText("+56987650009")).toHaveCount(0); // el número no aparece en la interfaz

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Quitar" }).first().click();
  await expect(page.getByText("La lista está vacía.")).toBeVisible();
});

test("integraciones: el secreto de la clave y del webhook se muestra una sola vez", async ({ page }) => {
  await page.goto("/integrations");
  await page.getByLabel("Nombre de la clave").fill("CRM e2e");
  await page.getByRole("button", { name: "Crear clave" }).click();
  const secret = page.getByRole("alert").filter({ hasText: "Clave de API" });
  await expect(secret.locator("code")).toContainText(/^ovk_[0-9a-f]{8}_/);
  const key = (await secret.locator("code").textContent()) ?? "";
  await secret.getByRole("button", { name: "Ya la guardé" }).click();
  await expect(page.getByText(key)).toHaveCount(0); // desaparece y no se vuelve a ver
  await page.reload();
  await expect(page.getByText(key)).toHaveCount(0);
  await expect(page.getByText(/CRM e2e/)).toBeVisible();
  await expect(page.getByText(/ovk_[0-9a-f]{8}…/)).toBeVisible();

  await page.getByLabel("URL del webhook").fill("http://localhost:9/hook"); // acepta http solo porque este servidor de pruebas permite redes privadas
  await page.getByRole("button", { name: "Añadir webhook" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Secreto de firma" }).locator("code")).toContainText("whsec_");
  await page.getByRole("button", { name: "Ya la guardé" }).click();

  await page.getByRole("button", { name: "Probar" }).click();
  await expect(page.getByRole("status")).toContainText("prueba en cola");
  await page.getByRole("button", { name: "Entregas" }).click();
  await expect(page.getByRole("table")).toContainText("webhook.test");

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Revocar" }).first().click();
  await expect(page.getByText("revocada", { exact: true })).toBeVisible();
});

test("auditoría: refleja lo hecho y se puede filtrar", async ({ page }) => {
  await page.goto("/audit");
  await expect(page.getByRole("heading", { name: "Auditoría" })).toBeVisible();
  await expect(page.getByRole("table")).toContainText("agent.created");
  await page.getByLabel("Filtrar por acción o actor").fill("apikey");
  await expect(page.getByRole("table")).toContainText("apikey.created");
  await expect(page.getByRole("table")).not.toContainText("agent.created");
  await page.getByLabel("Filtrar por acción o actor").fill("no-existe-esta-accion");
  await expect(page.getByText(/Sin registros para ese filtro/)).toBeVisible();
});

test("panel y llamadas: cargan con datos reales y sin errores de consola", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/dashboard");
  await expect(page.getByRole("region", { name: "Llamadas de los últimos 30 días" })).toBeVisible();
  await page.goto("/calls");
  await expect(page.getByText("Sin llamadas todavía.")).toBeVisible();
  expect(errors).toEqual([]);
});

test("conocimiento: sube un documento, lo encuentra con cita, lo reemplaza y lo elimina", async ({ page }) => {
  await page.goto("/knowledge");
  await page.getByLabel("Archivo").setInputFiles({
    name: "faq-e2e.txt", mimeType: "text/plain",
    buffer: Buffer.from("El horario de atención es de lunes a sábado de 9 a 20. Las devoluciones se aceptan durante 30 días con el ticket."),
  });
  await expect(page.getByRole("status")).toContainText("Documento añadido");
  await expect(page.getByText("faq-e2e.txt")).toBeVisible();

  await page.getByLabel("Pregunta").fill("¿Cuál es el horario de atención?");
  await page.getByRole("button", { name: "Buscar" }).click();
  await expect(page.locator("li", { hasText: "faq-e2e.txt" }).filter({ hasText: "lunes a sábado" })).toBeVisible();

  await page.getByLabel("Pregunta").fill("astronautas marcianos");
  await page.getByRole("button", { name: "Buscar" }).click();
  await expect(page.getByText(/Sin resultados/)).toBeVisible();

  await page.getByRole("button", { name: "Reemplazar" }).click();
  await page.getByLabel("Archivo").setInputFiles({ name: "faq-e2e.txt", mimeType: "text/plain", buffer: Buffer.from("Ahora atendemos también los domingos de 10 a 14 horas.") });
  await expect(page.getByRole("status")).toContainText("versión 2");

  // un archivo que no es lo que dice ser se rechaza con un mensaje claro
  await page.getByLabel("Archivo").setInputFiles({ name: "malo.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 este archivo esta roto") });
  await expect(page.getByRole("status")).toContainText("No se pudo leer el PDF");

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Eliminar" }).first().click();
  await expect(page.getByText("Aún no hay documentos.")).toBeVisible();
});

test("equipo: añade un miembro, cambia su rol, un admin no puede crear owners y se puede quitar", async ({ page }) => {
  await page.goto("/team");
  await expect(page.getByRole("heading", { name: "Equipo" })).toBeVisible();
  const email = `e2e-${Date.now()}@example.com`;

  await page.getByLabel("Email del miembro").fill(email);
  await page.getByLabel("Rol", { exact: true }).selectOption("operator");
  await page.getByRole("button", { name: "Añadir o actualizar" }).click();
  await expect(page.getByRole("status")).toContainText(`${email}: rol operator`);
  await expect(page.getByLabel(`Rol de ${email}`)).toHaveValue("operator");

  await page.getByLabel(`Rol de ${email}`).selectOption("viewer");
  await expect(page.getByRole("status")).toContainText("rol viewer");

  // un admin no puede dar el rol owner: el servidor lo rechaza y el panel lo muestra
  await page.getByLabel(`Rol de ${email}`).selectOption("owner");
  await expect(page.locator("p[role=alert]")).toContainText("owner");
  await expect(page.getByLabel(`Rol de ${email}`)).toHaveValue("viewer");

  page.once("dialog", (d) => d.accept());
  await page.locator("tr", { hasText: email }).getByRole("button", { name: "Quitar" }).click();
  await expect(page.getByRole("status")).toContainText("eliminado");
  await expect(page.locator("tr", { hasText: email })).toHaveCount(0);
});
