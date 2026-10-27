"""Bot de Telegram del centro de mando: lee tus mensajes y botones y ejecuta lo que apruebas.

Solo obedece a tu chat (TELEGRAM_CHAT_ID). Solo puede hacer las acciones de la lista ACCIONES; nada más.
"""
import json
import re

from base import (ErrorHTTP, GitHub, Make, Telegram, ahora, anotar, cargar, esc, gemini, guardar, hora_madrid,
                  iso, leer_fecha, proyecto_por_texto, proyectos)
from vigilar import pid, resumen_texto, texto_accion

AYUDA = """🛰 <b>Centro de mando</b>
/estado — semáforo de todos los proyectos
/proyecto finanzas|pin|mayores|zero — detalle de uno
/pendientes — propuestas esperando tu OK
/lanzar &lt;escenario&gt; — ejecutar ya un escenario de Make
/pausar &lt;escenario o proyecto&gt; — pausar
/activar &lt;escenario o proyecto&gt; — encender
/creditos — consumo de Make del mes
/historial — lo último que ha hecho el agente

También puedes preguntarme con tus palabras, p. ej. «¿qué ha fallado hoy?».
Antes de cambiar nada siempre te pido confirmación con un botón."""


# ---------- acciones permitidas ----------

def ejecutar_accion(a):
    make, gh = Make(), GitHub()
    t = a["tipo"]
    if t == "ejecutar_escenario":
        make.ejecutar(a["escenario"])
        return f"▶️ Lanzado «{a['nombre']}» en Make."
    if t == "activar_escenario":
        make.activar(a["escenario"])
        return f"🟢 Encendido «{a['nombre']}»."
    if t == "pausar_escenario":
        make.pausar(a["escenario"])
        return f"⏸ Pausado «{a['nombre']}»."
    if t == "mover_escenario":
        make.mover(a["escenario"], a["carpeta"])
        return f"📁 Movido «{a['nombre']}» a {a['carpeta_nombre']}."
    if t == "relanzar_workflow":
        gh.relanzar_fallidos(a["repo"], a["run"])
        return f"🔁 Relanzados los pasos fallidos de «{a['nombre']}» en GitHub."
    if t in ("pausar_varios", "activar_varios"):
        f = make.pausar if t == "pausar_varios" else make.activar
        hechos, fallos = 0, []
        for e in a["escenarios"]:
            try:
                f(e["id"])
                hechos += 1
            except ErrorHTTP as err:
                fallos.append(f"{e['nombre']}: {err}")
        verbo = "Pausados" if t == "pausar_varios" else "Encendidos"
        return f"{verbo} {hechos} escenarios de {a['nombre']}." + (("\nNo pude con: " + "; ".join(fallos)) if fallos else "")
    raise ErrorHTTP(f"acción desconocida: {t}")


def texto_accion_ext(a):
    if a["tipo"] == "pausar_varios":
        return f"pausar los {len(a['escenarios'])} escenarios activos de {a['nombre']}"
    if a["tipo"] == "activar_varios":
        return f"encender los {len(a['escenarios'])} escenarios apagados de {a['nombre']}"
    return texto_accion(a)


# ---------- búsqueda de escenarios en la última foto ----------

def todos_escenarios(estado):
    for p in estado.get("proyectos", []):
        for e in p["escenarios"]:
            yield p, e


def sin_tildes(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def buscar_escenario(estado, texto):
    t = sin_tildes(texto).strip()
    candidatos = list(todos_escenarios(estado))
    if t.isdigit():
        return [(p, e) for p, e in candidatos if str(e["id"]) == t]
    exactos = [(p, e) for p, e in candidatos if sin_tildes(e["nombre"]) == t]
    if exactos:
        return exactos
    palabras = [w for w in re.split(r"\s+", t) if w]
    return [(p, e) for p, e in candidatos if all(w in sin_tildes(e["nombre"]) for w in palabras)]


# ---------- respuestas ----------

def proponer_manual(propuestas, tg, accion, texto):
    clave = f"manual:{accion['tipo']}:{json.dumps(accion, sort_keys=True)}:{iso(ahora())}"
    p = {"id": pid(clave), "clave": clave, "proyecto": None, "nivel": "manual", "texto": texto, "error": None,
         "diagnostico": "", "enlace": None, "accion": accion, "estado": "abierta", "creada": iso(ahora())}
    r = tg.enviar(f"¿Confirmas? <b>{esc(texto_accion_ext(accion))}</b>",
                  [[{"text": "✅ Sí", "callback_data": f"ok:{p['id']}"}, {"text": "✖️ No", "callback_data": f"no:{p['id']}"}]])
    p["message_id"] = (r.get("result") or {}).get("message_id")
    propuestas[p["id"]] = p


def detalle_proyecto(p):
    color = {"verde": "🟢", "ambar": "🟠", "rojo": "🔴"}[p["semaforo"]]
    l = [f"{color} <b>{esc(p['emoji'] + ' ' + p['nombre'])}</b>" + (f" · @{esc(p['instagram'])}" if p["instagram"] else "")]
    l.append(f"Make: {p['make']['activos']}/{p['make']['total']} escenarios activos")
    if p["incidencias"]:
        l.append("\n<b>Incidencias</b>")
        l += [f"• {esc(i['texto'])}" for i in p["incidencias"]]
    if p["proximas"]:
        l.append("\n<b>Próximas publicaciones</b>")
        l += [f"• {hora_madrid(leer_fecha(n['cuando']))} · {esc(n['nombre'])}" for n in p["proximas"]]
    malos = [w for w in p["workflows"] if w["ultimo"] == "failure"]
    if malos:
        l.append("\n<b>Workflows con fallo</b>")
        l += [f"• <a href=\"{w['url']}\">{esc(w['workflow'])}</a>" for w in malos]
    apagados = [e for e in p["escenarios"] if not e["activo"]]
    if apagados:
        l.append("\n<b>Apagados</b>: " + esc(", ".join(e["nombre"] for e in apagados)))
    return "\n".join(l)


def preguntar_a_gemini(texto, estado):
    resumen = []
    for p in estado.get("proyectos", []):
        resumen.append({"proyecto": p["nombre"], "semaforo": p["semaforo"], "incidencias": [i["texto"] for i in p["incidencias"]],
                        "proximas": p["proximas"], "apagados": [e["nombre"] for e in p["escenarios"] if not e["activo"]],
                        "escenarios": [{"n": e["nombre"], "ult": e["ultimo"], "prox": e["proxima"]} for e in p["escenarios"] if e["activo"]][:60],
                        "workflows": [{"n": w["workflow"], "ult": w["ultimo"], "cuando": w["cuando"]} for w in p["workflows"]]})
    hist = "Últimos movimientos del agente: " + json.dumps(cargar_historial()[-15:], ensure_ascii=False)
    r = gemini(
        "Eres el asistente del centro de mando de un creador que publica en Instagram de forma automática con Make y "
        "GitHub Actions. Responde en español de España, breve (máximo 8 líneas), sin markdown, usando SOLO estos datos. "
        "Las horas están en UTC: pásalas a hora de Madrid. Si te pide cambiar algo, dile qué comando usar "
        "(/lanzar, /pausar, /activar) porque tú no puedes cambiar nada.\n\n"
        f"Datos ({estado.get('actualizado')}):\n{json.dumps(resumen, ensure_ascii=False)[:30000]}\n{hist}\n\nPregunta: {texto}",
        maximo=500)
    return esc(r) if r else "No he entendido el comando. " + AYUDA


def procesar_mensaje(texto, tg, propuestas):
    estado = cargar("status.json", {})
    cmd, _, arg = texto.strip().partition(" ")
    cmd = cmd.lower().split("@")[0]
    arg = arg.strip()

    if cmd in ("/start", "/ayuda", "/help"):
        return tg.enviar(AYUDA)
    if not estado:
        return tg.enviar("Todavía no tengo datos: la primera vigilancia aún no ha terminado. Prueba en unos minutos.")
    if cmd == "/estado":
        return tg.enviar(resumen_texto(estado))
    if cmd == "/proyecto":
        p = proyecto_por_texto(arg) if arg else None
        if not p:
            return tg.enviar("¿Cuál? " + " · ".join(f"/proyecto {x['id']}" for x in proyectos()))
        datos = next(x for x in estado["proyectos"] if x["id"] == p["id"])
        return tg.enviar(detalle_proyecto(datos))
    if cmd == "/pendientes":
        abiertas = [p for p in propuestas.values() if p["estado"] == "abierta"]
        if not abiertas:
            return tg.enviar("✅ No hay nada esperando tu aprobación.")
        for p in abiertas[:10]:
            r = tg.enviar(f"👉 {esc(p['texto'])}\n<b>{esc(texto_accion_ext(p['accion']))}</b>",
                          [[{"text": "✅ Hazlo", "callback_data": f"ok:{p['id']}"},
                            {"text": "🙈 Ignorar", "callback_data": f"no:{p['id']}"}]])
            p["message_id"] = (r.get("result") or {}).get("message_id") or p.get("message_id")
        return
    if cmd == "/creditos":
        c = estado.get("creditos")
        return tg.enviar(f"💳 Make: {c['usados']}/{c['limite']} créditos este mes ({c['pct']} %)" if c else
                         "No pude leer el consumo de Make (revisa los permisos del token: organizaciones de lectura).")
    if cmd == "/historial":
        lineas = cargar_historial()[-10:]
        return tg.enviar("🗒 <b>Últimos movimientos</b>\n" + "\n".join(
            f"• {hora_madrid(leer_fecha(x['t']))} {esc(x['evento'])}: {esc(x.get('texto') or x.get('resultado') or '')}"
            for x in lineas) if lineas else "Aún no hay movimientos.")
    if cmd in ("/lanzar", "/pausar", "/activar"):
        if not arg:
            return tg.enviar(f"Dime qué: {cmd} &lt;nombre del escenario o proyecto&gt;")
        proy = proyecto_por_texto(arg)
        if proy and cmd != "/lanzar":
            datos = next(x for x in estado["proyectos"] if x["id"] == proy["id"])
            quiero_activos = cmd == "/pausar"
            lista = [{"id": e["id"], "nombre": e["nombre"]} for e in datos["escenarios"] if e["activo"] == quiero_activos]
            if not lista:
                return tg.enviar("No hay escenarios que cambiar en ese proyecto.")
            tipo = "pausar_varios" if cmd == "/pausar" else "activar_varios"
            return proponer_manual(propuestas, tg, {"tipo": tipo, "nombre": proy["nombre"], "escenarios": lista},
                                   f"{cmd} {proy['nombre']}")
        hallados = buscar_escenario(estado, arg)
        if not hallados:
            return tg.enviar("No encuentro ese escenario. Prueba con otra parte del nombre o con su número.")
        if len(hallados) > 1:
            return tg.enviar("Hay varios, sé más concreto:\n" + "\n".join(
                f"• {e['id']} · {esc(e['nombre'])}" for _, e in hallados[:12]))
        _, e = hallados[0]
        tipo = {"/lanzar": "ejecutar_escenario", "/pausar": "pausar_escenario", "/activar": "activar_escenario"}[cmd]
        return proponer_manual(propuestas, tg, {"tipo": tipo, "escenario": e["id"], "nombre": e["nombre"]}, f"{cmd} {e['nombre']}")
    if cmd.startswith("/"):
        return tg.enviar(AYUDA)
    return tg.enviar(preguntar_a_gemini(texto, estado))


def cargar_historial():
    from base import ESTADO
    p = ESTADO / "historial.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def procesar_boton(cq, tg, propuestas):
    datos = cq.get("data", "")
    tg.llamar("answerCallbackQuery", callback_query_id=cq["id"])
    decision, _, ident = datos.partition(":")
    p = propuestas.get(ident)
    msg_id = (cq.get("message") or {}).get("message_id")
    if not p:
        return tg.editar(msg_id, "Esta propuesta ya no existe.")
    if p["estado"] != "abierta":
        return tg.editar(msg_id, f"{esc(p['texto'])}\n\nYa estaba resuelta ({p['estado']}).")
    if decision == "no":
        p["estado"] = "ignorada"
        anotar("ignorada", id=ident, texto=p["texto"])
        return tg.editar(msg_id, f"🙈 <s>{esc(p['texto'])}</s>\nIgnorada.")
    try:
        resultado = ejecutar_accion(p["accion"])
        p["estado"] = "hecha"
    except ErrorHTTP as e:
        resultado = f"❌ No he podido: {esc(e)}"
        p["estado"] = "fallida"
    p["resuelta"] = iso(ahora())
    anotar(p["estado"], id=ident, texto=p["texto"], resultado=resultado)
    tg.editar(msg_id, f"{esc(p['texto'])}\n\n{resultado}")


def atender():
    """Procesa todo lo que te haya llegado por Telegram desde la última vez."""
    tg = Telegram()
    if not tg.token:
        print("Sin TELEGRAM_BOT_TOKEN: el bot no se atiende.")
        return 0
    control = cargar("control.json", {})
    propuestas = cargar("propuestas.json", {})
    r = tg.llamar("getUpdates", offset=control.get("offset", 0), timeout=0,
                  allowed_updates=["message", "callback_query"])
    n = 0
    for u in r.get("result", []):
        control["offset"] = u["update_id"] + 1
        chat = str(((u.get("message") or {}).get("chat") or (u.get("callback_query") or {}).get("message", {}).get("chat") or {}).get("id", ""))
        if not tg.chat:
            # puesta en marcha: aún no sabemos cuál es tu chat; te lo decimos para que lo guardes como secret
            print(f"::notice::Tu TELEGRAM_CHAT_ID es {chat}")
            tg.llamar("sendMessage", chat_id=chat, text=f"Hola 👋 Tu número de chat es {chat}. Guárdalo en GitHub como secret TELEGRAM_CHAT_ID.")
            continue
        if chat != tg.chat:
            continue  # no es tu chat: se ignora
        n += 1
        if u.get("callback_query"):
            procesar_boton(u["callback_query"], tg, propuestas)
        elif (u.get("message") or {}).get("text"):
            procesar_mensaje(u["message"]["text"], tg, propuestas)
    guardar("control.json", control)
    guardar("propuestas.json", propuestas)
    print(f"Telegram: {n} mensajes atendidos")
    return n
