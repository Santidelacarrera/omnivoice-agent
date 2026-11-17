# SDK de JavaScript

Un archivo, sin dependencias, Node >= 18 (usa `fetch`). Tipos en `omnivoice-sdk.d.ts`.

```js
import { OmniVoice, verifyWebhook } from "./omnivoice-sdk.mjs";

const ov = new OmniVoice("https://api.tu-dominio.com", { apiKey: process.env.OMNIVOICE_API_KEY });
const agent = await ov.agents.create({ name: "Recepción", instructions: "Atiende llamadas con amabilidad." });
await ov.agents.publish(agent.id);
await ov.calls.create({ to: "+56987654321", consentBasis: "existing_customer" });

// Receptor de webhooks: usa el cuerpo CRUDO, no el JSON reparseado.
verifyWebhook(secret, req.headers["x-omnivoice-signature"], rawBody); // true | false
```

- Los errores de la API lanzan `OmniVoiceError` con `status` y `detail`.
- Una clave de API no gestiona claves, webhooks ni equipo: para eso usa `{ token }` con el JWT de una persona.
- No uses una clave de API en código que corra en el navegador de tus clientes.
- Pruebas: `npm test` (con `fetch` simulado; la firma se verificó además contra el firmador real del backend).
- No está publicado en npm.
