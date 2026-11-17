# omnivoice-sdk (Python)

Cliente mínimo de la API de OmniVoice Agent. Un solo archivo (`omnivoice_sdk.py`), depende solo de `httpx`.

```python
from omnivoice_sdk import OmniVoice, OmniVoiceError, verify_webhook

with OmniVoice("https://api.tu-dominio.com", api_key="ovk_...") as ov:
    agent = ov.agents.create(name="Recepción", instructions="Atiende llamadas con amabilidad.", tools=["check_inventory"])
    sim = ov.agents.simulate(agent["id"], ["Hola, ¿tienen chaquetas negras talla M?"])   # prueba antes de publicar
    ov.agents.publish(agent["id"])
    job = ov.calls.create(to="+56987654321", consent_basis="existing_customer")          # llamada saliente
    print(ov.analytics(days=7)["rates"])
```

Verificar un webhook (usa el cuerpo **crudo** de la petición):

```python
ok = verify_webhook(secret, request.headers["X-OmniVoice-Signature"], raw_body)
```

Notas: una clave de API no puede crear claves ni webhooks (usa un JWT de una persona para eso). El secreto de un webhook se muestra una sola vez al crearlo. Las llamadas salientes exigen una base de consentimiento y respetan horarios y lista de exclusión (ver `docs/RUNBOOK.md`).
