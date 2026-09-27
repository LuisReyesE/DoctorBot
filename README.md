# Doctor Bot — Backend + Panel de gestión

Teleorientación médica primaria por **mensajería** (Telegram, WhatsApp o mock).  
Paciente y médico operan por el canal configurado; el **panel web** (`/panel`) es solo para el equipo: ver consultas, médicos, pagos y PDFs.

Documentación de visión y UML: **[PROJECT.md](./PROJECT.md)**.

Proyecto **Crea y Emprende 2026**.

## Respuesta rápida: ¿hay IU en el backend?

| Canal | ¿UI? | Quién la usa |
|-------|------|----------------|
| Paciente | No (chat del canal activo) | Usuario final |
| Médico atención | No (chat + comandos) | Médico de turno |
| **Gestión centralizada** | **Sí — panel web** | Admin / operaciones / demo |

## Stack

- Python 3.11+, FastAPI, SQLite, SQLAlchemy
- Jinja2 + CSS para el panel
- ReportLab para PDF de orientación
- Mensajería: **mock** · **Telegram Bot API** · **Meta WhatsApp Cloud API**
- Un solo canal activo a la vez (`MESSAGING_PROVIDER`)

## Instalación

```powershell
cd C:\Users\Luis\Documents\PROYECTOS-PROGRAMACION\saludfacil
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Si SQLite falla por esquema viejo:

```powershell
Remove-Item .\doctorbot.db -ErrorAction SilentlyContinue
Remove-Item .\saludfacil.db -ErrorAction SilentlyContinue
```

## Ejecutar

```powershell
uvicorn app.main:app --reload --port 8000
```

| URL | Descripción |
|-----|-------------|
| http://127.0.0.1:8000/panel | Panel de gestión (token: `admin`) |
| http://127.0.0.1:8000/docs | OpenAPI |
| http://127.0.0.1:8000/health | Healthcheck |
| http://127.0.0.1:8000/dev/simulate | Simular mensajes (dev + mock) |
| `/webhooks/telegram` | Webhook Telegram |
| `/webhooks/whatsapp` | Webhook Meta WhatsApp |

## Canales

| `MESSAGING_PROVIDER` | Uso |
|----------------------|-----|
| `mock` | Demo local + tests |
| `telegram` | Prueba real (BotFather + ngrok) |
| `meta` | WhatsApp Cloud API |

Con Telegram: arranca **ngrok http 8000** y luego **uvicorn**; el webhook se registra solo si `TELEGRAM_AUTO_WEBHOOK=true`.

## Tests

```powershell
pytest -q
```

## Aviso médico / legal

Servicio de **teleorientación primaria**. No reemplaza emergencias ni consulta presencial.  
Emergencia: **106 SAMU**. El PDF no es receta oficial del MINSA.
