# Doctor Bot

Teleorientación médica primaria por chat. El paciente, el médico y la caja usan el mismo bot. El equipo de operación usa un panel web.

Canal activo, uno solo a la vez: **mock** (local y tests), **Telegram** o **WhatsApp** (Meta Cloud API). La identidad de cada persona es el `external_id` del canal: chat de Telegram o teléfono de WhatsApp. Si ese id está registrado como médico, el mensaje se trata como médico. Si está registrado como caja, como caja. En cualquier otro caso, como paciente. Un mismo número no puede cumplir dos roles.

Visión, UML y reglas de asignación con más detalle: **[PROJECT.md](./PROJECT.md)**.  
Proyecto **Crea y Emprende 2026**. Versión de la API: `0.2.0`.  
Repositorio: [github.com/LuisReyesE/DoctorBot](https://github.com/LuisReyesE/DoctorBot).

> Orientación primaria. No reemplaza una consulta presencial ni una emergencia. Ante signos de alarma el bot indica llamar al **106 (SAMU)**. El PDF no es receta del MINSA.

## Quién usa qué

| Quién | Dónde | Qué hace |
|-------|--------|----------|
| Paciente | Chat | Menú, suscripción, triaje, mensajes al médico |
| Médico | El mismo chat, con comandos | Turno, ficha, orientación, videollamada, cierre y PDF |
| Caja | El mismo chat, con comandos | Confirma o rechaza el Yape/Plin. No atiende consultas |
| Admin | [http://127.0.0.1:8000/panel](http://127.0.0.1:8000/panel) | Consultas, médicos, turnos, cajas, pagos, PDF |

El médico no ve el voucher. La caja no ve el chat clínico.

## Cómo corre una consulta

1. La primera vez que alguien escribe, se crea el usuario. Si `TRIAL_DAYS` es mayor que 0 y nunca tuvo plan, arranca una prueba (3 días por defecto).
2. Con suscripción vigente elige **1 Nueva consulta**. El triaje pide, en orden: nombre, edad (0–120), síntoma, tiempo de evolución y una foto o la palabra *omitir*.
3. Si el texto coincide con signos de alarma (paro, no respira, dolor de pecho, convulsión, hemorragia, etc.), la consulta pasa a `emergency` y se indica el 106. No se asigna médico. Si eso ocurre **ya en el chat con el médico**, el paciente recibe el mismo aviso y la consulta sigue abierta.
4. Al completar el triaje la consulta entra a una cola FIFO. Después se busca médico, así un caso nuevo no se adelanta a quien ya esperaba.
5. El médico recibe la ficha. Lo que escribe, y las fotos (por ejemplo una receta manuscrita), se reenvían al paciente. Puede abrir una sala Jitsi con `/video`. Solo el médico inicia la videollamada. La sala es estable por consulta y este sistema no la graba.
6. `/cerrar` arma el PDF de orientación, se lo envía al paciente y, si el médico sigue en horario, toma el siguiente de la cola.
7. Unas 48 horas después, un proceso en segundo plano manda un seguimiento breve. También se puede disparar a mano desde el panel.

Un paciente solo puede tener una consulta abierta (`triaging`, `queued`, `assigned` o `active`). Un médico atiende un solo caso a la vez.

### Estados de la consulta

| Estado | Significado |
|--------|-------------|
| `triaging` | Formulario a medias. Aún no llega a un médico |
| `queued` | Triaje listo, esperando médico libre y en horario |
| `assigned` | Hay médico. Todavía no escribió |
| `active` | El médico ya respondió |
| `closed` | Cerrada. Hay PDF si se pudo generar |
| `emergency` | Derivada al 106 durante el triaje |

### Estados del médico

| Comando | Efecto |
|---------|--------|
| `/disponible` | Entra al sorteo. Si tiene un caso abierto, el bot se lo recuerda y no cambia nada |
| `/ocupado` | No recibe casos nuevos. Conserva el activo |
| `/fuera` | Sale de servicio. Solo si no tiene caso activo |
| `/cerrar` | Cierra el caso, genera el PDF y puede tomar el siguiente de la cola |

Los turnos se cargan en el panel (día y hora de Perú). Sin franjas, el médico no tiene restricción de horario. Dentro del horario automático solo entra quien está en su franja. `/disponible` fuera de horario cuenta como hora extra y sí puede tomar la cola.

### A quién se le asigna

Entre los médicos `available`, sin caso activo y en horario:

1. Si el triaje sugiere una especialidad y hay alguien de esa pista, se prefiere. Menor de 12 años apunta a pediatría. Palabras del síntoma pueden apuntar a dermatología, ginecología, traumatología o farmacia.
2. Si no hay especialista, cae en medicina general o en cualquier disponible. Nunca se deja al paciente esperando a un especialista.
3. A igualdad, gana quien tiene menos consultas asignadas hoy (día civil Perú) y, si empatan, quien lleva más tiempo sin un caso.

## Menú del paciente

Botones o el número:

| Opción | Acción |
|--------|--------|
| 1 Nueva consulta | Triaje, si la suscripción está vigente |
| 2 Estado | Dónde va su consulta, incluida la posición en la cola |
| 3 Suscripción | Plan mensual o trimestral |
| 4 Ayuda | Resumen del servicio |
| Continuar | Retoma un triaje incompleto |
| Cancelar consulta | Abandona el triaje que no llegó al médico |
| menu | Vuelve al inicio en cualquier momento |

Durante un chat activo, el texto libre se reenvía al médico. *Menú* y *2 Estado* siguen disponibles.

## Comandos del médico

| Comando | Acción |
|---------|--------|
| `/disponible` `/ocupado` `/fuera` | Turno |
| `/casos` | Consultas asignadas, cola global y PDF recientes |
| `/horario` | Franjas configuradas en el panel |
| `/video` | Enlace Jitsi de la consulta activa, al paciente y al médico |
| `/cerrar` | Cierra, PDF al paciente y siguiente de la cola |
| `/pdf 8` | Reenvía el PDF de una consulta **suya** ya cerrada |
| `/ayuda` | Lista de comandos |

Cualquier otro texto, o una foto, se reenvía al paciente de la consulta activa.

## Suscripción y caja

Precios por defecto: mensual **S/ 8** (30 días) y trimestral **S/ 22** (90 días), en `PRICE_MONTHLY` y `PRICE_QUARTERLY`. El cobro no pasa por una pasarela. El paciente paga al Yape o Plin del proyecto (`YAPE_PHONE`, `PLIN_PHONE`) y escribe en el mensaje el código `SALUD-XXXX`.

Orden del alta:

1. Elige plan.
2. Indica DNI (8 dígitos) o carné de extranjería. Se pide una vez, en la suscripción, no en cada triaje.
3. Acepta el consentimiento. Si dice que no, no se crea el pago y la prueba no se toca.
4. Nombre y celular tal como figuran en Yape.
5. Envía la foto del voucher.

Todas las cajas registradas reciben la ficha. La primera que confirma gana. El paciente recibe el aviso en el chat y la suscripción queda activa.

| Comando de caja | Acción |
|-----------------|--------|
| `/casos` | Pagos pendientes |
| `/buscar K7P2` | Ficha por código, `SALUD-K7P2` o id |
| `/confirmar K7P2` | Activa la suscripción |
| `/rechazar K7P2 motivo` | Avisa al paciente |
| `/otrafoto K7P2` | Pide otra captura, misma ficha |
| `/ayuda` | Lista de comandos |

Las cajas se crean en **Panel → Cajas** con el chat id o teléfono del canal. No van en el `.env`.

## Panel web

Login con `PANEL_TOKEN` (en desarrollo el ejemplo es `admin`). Cookie de sesión. Antes de exponer el servidor, cambia ese token.

| Página | Para qué |
|--------|----------|
| `/panel` | Conteos: abiertas, cola, emergencias, médicos libres, pagos pendientes |
| `/panel/consultations` | Lista y ficha: triaje, chat, foto, PDF |
| `/panel/doctors` | Alta, especialidad, carga del día, turnos, pasar a fuera de servicio |
| `/panel/cajas` | Alta y baja de quien confirma pagos |
| `/panel/users` | Pacientes y estado de suscripción |
| `/panel/payments` | Voucher, confirmar o rechazar igual que la caja |

En el detalle de una consulta el equipo puede **reasignar** a otro médico o **forzar el cierre**. El dashboard se refresca solo. Las horas del panel se muestran en hora Perú. En la base quedan en UTC.

Otras URLs con el servidor en marcha:

| URL | Descripción |
|-----|-------------|
| http://127.0.0.1:8000/docs | OpenAPI |
| http://127.0.0.1:8000/health | Healthcheck |
| http://127.0.0.1:8000/dev/simulate | Simular un mensaje. Solo si `APP_ENV` es development y el canal es `mock` |
| `POST /webhooks/telegram` | Webhook de Telegram |
| `GET/POST /webhooks/whatsapp` | Verificación y mensajes de Meta |

## Stack y carpetas

- Python 3.11+, FastAPI, SQLite (WAL), SQLAlchemy
- Jinja2 y CSS para el panel
- ReportLab para el PDF
- httpx hacia Telegram o Meta

```text
app/api/          webhooks, panel, health, simulador
app/providers/    mock, telegram, meta
app/services/     triaje, cola, puente de mensajes, PDF, caja, recordatorios
app/models/       tablas SQLAlchemy
app/templates/    HTML del panel
scripts/          re-registro manual del webhook de Telegram
storage/pdfs/     orientaciones generadas (no se suben a Git)
storage/media/    fotos de triaje y vouchers (no se suben a Git)
tests/            pytest
```

Al arrancar, el proceso crea las tablas, agrega columnas nuevas si faltan (SQLite, sin Alembic) y siembra el médico de `SEED_DOCTOR_ID`. Si `REMINDER_AUTO=true`, revisa seguimientos cada `REMINDER_POLL_SECONDS` (900 s por defecto).

## Instalación

```powershell
cd C:\Users\Luis\Documents\PROYECTOS-PROGRAMACION\DoctorBot
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edita `.env` con el token del bot y el `PANEL_TOKEN`. El archivo `.env` no se commitea.

```powershell
uvicorn app.main:app --reload --port 8000
```

Si SQLite se queja por un esquema viejo e incompatible, apaga el servidor y borra la base local. Se pierde el historial de esa máquina:

```powershell
Remove-Item .\doctorbot.db, .\doctorbot.db-wal, .\doctorbot.db-shm -ErrorAction SilentlyContinue
```

## Canales

| `MESSAGING_PROVIDER` | Cuándo |
|----------------------|--------|
| `mock` | Demo en la misma máquina y `pytest`. No sale ningún mensaje real |
| `telegram` | Prueba con pacientes y médicos de verdad |
| `meta` | WhatsApp Cloud API. Hoy envía texto. Fotos, PDF y botones del menú están completos en Telegram, no en este cliente |

### Telegram

1. Crea el bot con [@BotFather](https://t.me/BotFather) y pon el token en `TELEGRAM_BOT_TOKEN`.
2. En otra terminal: `ngrok http 8000`.
3. Arranca uvicorn **después** de ngrok. Con `TELEGRAM_AUTO_WEBHOOK=true` el proceso lee el túnel en `http://127.0.0.1:4040` y registra `https://<tunel>/webhooks/telegram`.
4. Si prefieres fijar la URL, llena `TELEGRAM_WEBHOOK_URL` (sin el path `/webhooks/...`).
5. Opcional: `TELEGRAM_WEBHOOK_SECRET`, un texto corto. No pongas ahí la URL de ngrok.
6. El chat id del médico demo va en `SEED_DOCTOR_ID`. Los demás médicos se agregan en el panel.
7. Para volver a registrar el webhook sin reiniciar: `.\scripts\set_telegram_webhook.ps1`.

El paciente abre el bot, escribe `/start` y usa el menú. El médico, desde su Telegram, escribe `/disponible`.

### WhatsApp (Meta)

Hacen falta `META_WHATSAPP_TOKEN`, `META_PHONE_NUMBER_ID` y `META_VERIFY_TOKEN`. Meta verifica el webhook con `GET /webhooks/whatsapp`. `META_APP_SECRET` está en la configuración. El endpoint todavía no valida la firma `X-Hub-Signature`.

### Simular sin Telegram

Con `MESSAGING_PROVIDER=mock` y el servidor en development:

```powershell
curl -X POST http://127.0.0.1:8000/dev/simulate -H "Content-Type: application/json" -d "{\"from_phone\":\"51911111111\",\"body\":\"/start\"}"
```

`from_phone` es el id del canal. Sirve también para el médico demo (`SEED_DOCTOR_ID`).

## Variables de entorno

Copia de trabajo: `.env.example`.

| Variable | Rol |
|----------|-----|
| `APP_ENV` | `development` abre `/dev/simulate` |
| `DATABASE_URL` | Por defecto `sqlite:///./doctorbot.db` |
| `MESSAGING_PROVIDER` | `mock`, `telegram` o `meta`. También se acepta el nombre viejo `WHATSAPP_PROVIDER` |
| `TELEGRAM_BOT_TOKEN` | Token de BotFather |
| `TELEGRAM_WEBHOOK_URL` | URL pública del túnel, sin path. Vacío = leer ngrok en el puerto 4040 |
| `TELEGRAM_WEBHOOK_SECRET` | Secreto opcional del webhook |
| `TELEGRAM_AUTO_WEBHOOK` | Registrar el webhook al arrancar |
| `SEED_DOCTOR_ID` | Chat id o teléfono del médico demo. Alias antiguo: `SEED_DOCTOR_PHONE` |
| `SEED_DOCTOR_NAME` | Nombre de ese médico |
| `PANEL_TOKEN` | Clave del panel |
| `PANEL_ENABLED` | `false` oculta el panel |
| `REQUIRE_SUBSCRIPTION` | `false` deja pasar consultas sin plan (así corren los tests) |
| `TRIAL_DAYS` | Días de prueba al primer mensaje. `0` la desactiva |
| `PRICE_MONTHLY` / `PRICE_QUARTERLY` | Montos en soles |
| `YAPE_PHONE` / `YAPE_NAME` | Destino del cobro |
| `PLIN_PHONE` / `PLIN_NAME` | Destino alternativo |
| `PAYMENT_QR_PATH` | QR opcional. Si no, se busca `storage/pay/yape-qr.png` |
| `REMINDER_HOURS` | Horas hasta el seguimiento (48) |
| `REMINDER_AUTO` | Bucle de recordatorios al arrancar |
| `REMINDER_POLL_SECONDS` | Cada cuánto revisa |
| `JITSI_BASE_URL` | Base de la sala. Por defecto `https://meet.jit.si` |

## Tests

```powershell
pytest -q
```

Cubren triaje, emergencia, cola, carga del día, especialidad blanda, turnos, PDF, video, consentimiento, caja y el panel. Usan SQLite en memoria y el canal `mock`. No hace falta Telegram.

## Qué no se sube a Git

`.env`, `doctorbot.db` (y los `-wal` / `-shm`), `storage/pdfs/`, `storage/media/` y `storage/pay/`. Ahí viven tokens, consultas, fotos clínicas y capturas de pago. En otra máquina se clona el repo, se copia `.env.example` a `.env` y se completan los tokens.

## Aviso

Doctor Bot ofrece **orientación primaria**. No sustituye una cita presencial ni una emergencia. El PDF es un resumen de la orientación. **No** es una receta oficial del MINSA. Emergencias: **106**.
