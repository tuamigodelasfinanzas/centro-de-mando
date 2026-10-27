# Centro de mando

Vigila y gestiona desde un solo sitio tus cuatro cuentas automatizadas:

| Proyecto | Make (carpeta) | GitHub |
|---|---|---|
| 💶 Tu Amigo de las Finanzas | Tu Amigo de las Finanzas | resumen-bolsa-reel |
| 🌍 Planet in Numbers | Planet in Numbers | planet-in-numbers |
| 🧒 Cosas de Mayores | Cosas de Mayores | No-Entiendo-a-los-Mayores |
| 🤖 Zero Understands | Zero Understands | zero-understands |

## Qué hace

- **Cada 10 minutos** lee tu Telegram: aprobaciones, comandos y preguntas.
- **Cada hora** revisa todos los escenarios de Make y todos los workflows de GitHub de los cuatro repos.
- Si algo falla, **te escribe por Telegram** con la explicación (si hay clave de Gemini, en lenguaje claro) y una propuesta con dos botones: **✅ Hazlo** o **🙈 Ignorar**. No cambia nada sin tu ✅.
- Los fallos que se arreglan solos al reintentar (por ejemplo, Gemini tardando demasiado) no te molestan: salen como «recuperados».
- **A partir de las 08:00** te manda un resumen de los cuatro proyectos (una vez al día).
- **Panel web** con el semáforo de todo: `https://tuamigodelasfinanzas.github.io/centro-de-mando/`

Lo que puede proponer: ejecutar un escenario de Make que falló, volver a encender uno que Make apagó, mover un escenario a la carpeta de su proyecto y relanzar los pasos fallidos de un workflow de GitHub.

## Comandos de Telegram

| Comando | Qué hace |
|---|---|
| `/estado` | Semáforo de todos los proyectos |
| `/proyecto finanzas` (o `pin`, `mayores`, `zero`) | Detalle de un proyecto |
| `/pendientes` | Propuestas esperando tu OK |
| `/lanzar palabra del dia` | Ejecutar ya un escenario (te pide confirmación) |
| `/pausar zero` | Pausar un escenario o un proyecto entero (te pide confirmación) |
| `/activar zero` | Encender un escenario o un proyecto entero (te pide confirmación) |
| `/creditos` | Consumo de créditos de Make este mes |
| `/historial` | Lo último que ha hecho el agente |

También puedes escribir con tus palabras («¿qué ha fallado hoy?», «¿qué se publica mañana?») y te responde con los datos reales.

## Puesta en marcha (una sola vez, unos 15 minutos)

Todo se guarda en **Settings → Secrets and variables → Actions → New repository secret** de este repo.

**1. Bot de Telegram nuevo**
1. En Telegram, abre **@BotFather**, escribe `/newbot` y ponle de nombre «Centro de mando».
2. Copia el token que te da y guárdalo como secret **`TELEGRAM_BOT_TOKEN`**.
3. Abre tu bot nuevo y escríbele `/start`.
4. En este repo: **Actions → Centro de mando → Run workflow**. El bot te contesta con tu número de chat.
5. Guarda ese número como secret **`TELEGRAM_CHAT_ID`**.

**2. Token de Make** → secret **`MAKE_API_TOKEN`**
Puedes usar el mismo valor que ya tienes en el repo resumen-bolsa-reel. Si creas uno nuevo (Make → tu perfil → API access → Add token), marca: `scenarios:read`, `scenarios:write`, `scenarios:run`, `hooks:read`, `organizations:read`.

**3. Permiso para relanzar workflows** → secret **`GH_PAT`**
GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token. En *Repository access* elige tus 4 repos de proyectos; en *Permissions → Actions* elige **Read and write**. Sin este secret el agente avisa de los fallos de GitHub pero no puede relanzarlos.

**4. (Opcional) Explicaciones claras** → secret **`GEMINI_API_KEY`**
La misma clave de Google AI Studio que ya usas en resumen-bolsa-reel.

**5. Panel web**
Settings → Pages → *Deploy from a branch* → `main` / `(root)` → Save.

**6. Arrancar**
Actions → Centro de mando → Run workflow → modo `resumen`. En un minuto te llega el primer resumen.

## Cómo está hecho

- `config/proyectos.json`: los proyectos, sus carpetas de Make, sus repos y cómo se reconocen los escenarios por el nombre. Para añadir un proyecto nuevo, añade un bloque aquí.
- `agente/vigilar.py`: lee Make y GitHub, calcula el semáforo y propone arreglos.
- `agente/bot.py`: atiende Telegram y ejecuta solo las acciones de su lista cerrada.
- `.github/workflows/centro.yml`: lo pone en marcha cada 10 minutos.
- El estado (panel, propuestas, historial) se guarda en la rama `estado`, así `main` no se llena de cambios.

Gasto: el repo es público, así que GitHub Actions no tiene límite de minutos. En Make no gasta créditos (solo lee por la API).
