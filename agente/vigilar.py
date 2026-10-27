"""Vigilancia: lee Make y GitHub, calcula el semáforo de cada proyecto, detecta problemas y propone arreglos.

Nunca cambia nada por su cuenta: cada arreglo se te propone por Telegram con un botón y solo se hace
cuando lo apruebas (eso lo ejecuta bot.py).
"""
import hashlib
import time
from collections import defaultdict
from datetime import timedelta

from base import (CONFIG, ErrorHTTP, GitHub, Make, Telegram, ahora, anotar, cargar, esc, gemini, guardar,
                  hora_madrid, iso, leer_fecha, limpiar, proyecto_por_carpeta, proyecto_por_nombre_escenario,
                  proyectos, url_make_escenario)

VENTANA = timedelta(hours=36)          # cuánto hacia atrás se miran los fallos
CADUCIDAD = timedelta(hours=48)        # una propuesta sin respuesta caduca
MAX_DIAGNOSTICOS = 5                   # llamadas a Gemini por vuelta, para no gastar cuota
TEMPORALES = ("timeout", "timed out", "429", "rate", "quota", "502", "503", "504", "ECONNRESET", "temporarily")

TIPOS_PROGRAMADOS = {"weekly", "daily", "monthly", "indefinitely", "on-demand", "once"}


def pid(clave):
    return hashlib.sha1(clave.encode()).hexdigest()[:8]


def recoger():
    """Foto de Make y GitHub. Si una fuente falla, se anota y se sigue con la otra."""
    make, gh = Make(), GitHub()
    datos = {"escenarios": [], "webhooks": [], "logs": {}, "org": None, "runs": {}, "errores": []}
    desde = ahora() - VENTANA
    try:
        datos["escenarios"] = make.escenarios()
        datos["webhooks"] = make.webhooks()
        for s in datos["escenarios"]:
            if s.get("isActive") or s.get("isinvalid"):
                try:
                    datos["logs"][s["id"]] = make.ejecuciones(s["id"], desde)
                except ErrorHTTP as e:
                    datos["errores"].append(f"Make, ejecuciones de «{s['name']}»: {e}")
                time.sleep(0.3)
        try:
            datos["org"] = make.organizacion()
            if datos["org"].get("_error_uso"):
                datos["errores"].append(f"Make, consumo de créditos: {datos['org']['_error_uso']}")
        except ErrorHTTP as e:
            datos["errores"].append(f"Make, consumo de créditos: {e}")
    except ErrorHTTP as e:
        datos["errores"].append(f"Make: {e}")
    for p in proyectos():
        try:
            datos["runs"][p["repo"]] = gh.ejecuciones(p["repo"], desde)
        except ErrorHTTP as e:
            datos["errores"].append(f"GitHub {p['repo']}: {e}")
    return datos


def creditos(org):
    """Consumo de Make del mes. La API ha cambiado de 'operaciones' a 'créditos': se aceptan ambos formatos."""
    if not org:
        return None
    lic = org.get("license") or {}
    limite = lic.get("credits") or lic.get("operations") or CONFIG["make"]["creditos_mes"]
    usados = None
    for k in ("credits", "operations", "consumedCredits", "consumedOperations"):
        v = org.get(k)
        if isinstance(v, (int, float)):
            usados = v
            break
    if isinstance(org.get("centicredits"), (int, float)):
        usados = org["centicredits"] / 100
    if isinstance(org.get("_usados"), (int, float)):
        usados = org["_usados"]
    if usados is None or not limite:
        return None
    return {"usados": round(usados), "limite": int(limite), "pct": round(100 * usados / limite)}


def ultima(logs):
    logs = sorted(logs, key=lambda x: x.get("timestamp", ""))
    return logs[-1] if logs else None


def analizar(datos):
    """Devuelve (estado por proyecto, lista de incidencias). Cada incidencia puede llevar una acción propuesta."""
    por_proyecto = {p["id"]: {"escenarios": [], "incid": [], "runs": []} for p in proyectos()}
    incidencias = []

    def incid(proy, nivel, texto, clave, accion=None, error=None, enlace=None):
        i = {"proyecto": proy["id"] if proy else None, "nivel": nivel, "texto": texto, "clave": clave,
             "accion": accion, "error": limpiar(error) if error else None, "enlace": enlace}
        incidencias.append(i)
        if proy:
            por_proyecto[proy["id"]]["incid"].append(i)

    hooks_por_escenario = defaultdict(list)
    for h in datos["webhooks"]:
        if h.get("scenarioId"):
            hooks_por_escenario[h["scenarioId"]].append(h)

    for s in datos["escenarios"]:
        # un escenario es del proyecto que dice su nombre; si el nombre no lo dice, del de su carpeta
        proy = proyecto_por_nombre_escenario(s["name"]) or proyecto_por_carpeta(s.get("folderId"))
        logs = datos["logs"].get(s["id"], [])
        u = ultima(logs)
        fallos = [x for x in logs if x.get("status") == 3]
        exitos = sorted(x.get("timestamp", "") for x in logs if x.get("status") == 1)
        recuperados = [f for f in fallos if exitos and exitos[-1] > f.get("timestamp", "")]
        tipo = (s.get("scheduling") or {}).get("type")
        fila = {
            "id": s["id"], "nombre": s["name"], "activo": bool(s.get("isActive")), "tipo": tipo,
            "proxima": s.get("nextExec"), "enlace": url_make_escenario(s["id"]),
            "ejecuciones": len(logs), "fallos": len(fallos), "recuperados": len(recuperados),
            "ultimo": {"estado": {1: "ok", 2: "aviso", 3: "error"}.get(u.get("status"), "?"), "cuando": u.get("timestamp"),
                       "error": limpiar((u.get("error") or {}).get("message", ""))[:200]} if u else None,
        }
        if proy:
            por_proyecto[proy["id"]]["escenarios"].append(fila)

        # 1. Make lo ha apagado por errores
        if s.get("isinvalid"):
            incid(proy, "rojo", f"Make ha apagado «{s['name']}» por errores", f"make:invalido:{s['id']}:{s.get('lastEdit','')}",
                  {"tipo": "activar_escenario", "escenario": s["id"], "nombre": s["name"]},
                  (u or {}).get("error", {}).get("message") if u else None, url_make_escenario(s["id"]))
            continue

        # 2. su última ejecución falló (si después salió bien, se dio por recuperado y no se avisa)
        if s.get("isActive") and u and u.get("status") == 3:
            err = u.get("error") or {}
            modulo = (err.get("causeModule") or {}).get("appName", "")
            texto = f"Falló «{s['name']}»" + (f" en el módulo {modulo}" if modulo else "")
            accion = None
            if tipo in TIPOS_PROGRAMADOS:
                accion = {"tipo": "ejecutar_escenario", "escenario": s["id"], "nombre": s["name"]}
            else:
                texto += " (lo dispara un webhook: se relanza desde el workflow de GitHub que lo llama)"
            incid(proy, "rojo", texto, f"make:fallo:{s['id']}:{u.get('id')}", accion,
                  f"{err.get('name','')}: {err.get('message','')}", url_make_escenario(s["id"]))
        elif s.get("isActive") and u and u.get("status") == 2:
            incid(proy, "ambar", f"«{s['name']}» terminó con avisos", f"make:aviso:{s['id']}:{u.get('id')}",
                  enlace=url_make_escenario(s["id"]))

        # 3. webhooks atascados: Make dice "Accepted" pero solo encola
        for h in hooks_por_escenario.get(s["id"], []):
            if h.get("queueCount", 0) > 0 and not s.get("isActive"):
                incid(proy, "rojo", f"El webhook de «{s['name']}» tiene {h['queueCount']} publicaciones en cola y el escenario está apagado",
                      f"make:cola:{s['id']}:{h['queueCount']}",
                      {"tipo": "activar_escenario", "escenario": s["id"], "nombre": s["name"]}, enlace=url_make_escenario(s["id"]))

        # 4. escenario en la carpeta de otro proyecto
        dueño = proyecto_por_nombre_escenario(s["name"])
        carpeta = proyecto_por_carpeta(s.get("folderId"))
        if dueño and carpeta != dueño:
            donde = f"en la carpeta de {carpeta['nombre']}" if carpeta else "sin carpeta"
            incid(dueño, "ambar", f"«{s['name']}» está {donde}; debería ir en {dueño['nombre']}",
                  f"make:carpeta:{s['id']}:{dueño['carpeta_make']}",
                  {"tipo": "mover_escenario", "escenario": s["id"], "nombre": s["name"], "carpeta": dueño["carpeta_make"],
                   "carpeta_nombre": dueño["nombre"]}, enlace=url_make_escenario(s["id"]))

    # GitHub: por cada workflow, su última ejecución terminada
    for p in proyectos():
        runs = datos["runs"].get(p["repo"], [])
        por_wf = defaultdict(list)
        for r in runs:
            if r.get("status") == "completed":
                por_wf[r["name"]].append(r)
        resumen = []
        for wf, lista in por_wf.items():
            lista.sort(key=lambda r: r.get("updated_at", ""))
            u = lista[-1]
            fallidas = [r for r in lista if r.get("conclusion") == "failure"]
            resumen.append({"workflow": wf, "ultimo": u.get("conclusion"), "cuando": u.get("updated_at"),
                            "url": u.get("html_url"), "ejecuciones": len(lista), "fallidas": len(fallidas)})
            if u.get("conclusion") == "failure":
                accion = None
                if u.get("run_attempt", 1) < 3:
                    accion = {"tipo": "relanzar_workflow", "repo": p["repo"], "run": u["id"], "nombre": wf}
                incid(p, "rojo", f"Falló el workflow «{wf}» de GitHub", f"gh:{p['repo']}:{u['id']}:{u.get('run_attempt',1)}",
                      accion, None, u.get("html_url"))
        resumen.sort(key=lambda x: (x["ultimo"] != "failure", x["workflow"]))
        por_proyecto[p["id"]]["runs"] = resumen

    return por_proyecto, incidencias


def semaforo(incid):
    if any(i["nivel"] == "rojo" for i in incid):
        return "rojo"
    if incid:
        return "ambar"
    return "verde"


def construir_estado(datos, por_proyecto, cred, propuestas):
    ahora_ = ahora()
    abiertas = {p["clave"]: p for p in propuestas.values() if p["estado"] == "abierta"}
    salida = []
    for p in proyectos():
        d = por_proyecto[p["id"]]
        esc_ = sorted(d["escenarios"], key=lambda e: (not e["activo"], e["proxima"] or "9999", e["nombre"]))
        prox = [e for e in esc_ if e["activo"] and e["proxima"] and leer_fecha(e["proxima"]) > ahora_]
        prox.sort(key=lambda e: e["proxima"])
        runs = d["runs"]
        incid = [{**i, "propuesta": abiertas.get(i["clave"], {}).get("id")} for i in d["incid"]]
        gh_agenda, err = agenda_github(p)
        if err:
            datos["errores"].append(err)
        prox = sorted(prox + [{"nombre": g["nombre"], "proxima": g["cuando"]} for g in gh_agenda if g["activo"]],
                      key=lambda e: e["proxima"])
        salida.append({
            "id": p["id"], "nombre": p["nombre"], "emoji": p["emoji"], "instagram": p["instagram"], "repo": p["repo"],
            "carpeta_make": p["carpeta_make"], "semaforo": semaforo(d["incid"]),
            "make": {"total": len(esc_), "activos": sum(e["activo"] for e in esc_),
                     "ejecuciones_36h": sum(e["ejecuciones"] for e in esc_), "fallos_36h": sum(e["fallos"] for e in esc_),
                     "recuperados_36h": sum(e["recuperados"] for e in esc_)},
            "github": {"workflows": len(runs), "ejecuciones_36h": sum(r["ejecuciones"] for r in runs),
                       "fallidas_36h": sum(r["fallidas"] for r in runs)},
            "proximas": [{"nombre": e["nombre"], "cuando": e["proxima"]} for e in prox[:5]],
            "incidencias": incid, "escenarios": esc_, "workflows": runs, "programadas_github": gh_agenda,
        })
    return {"actualizado": iso(ahora_), "creditos": cred, "proyectos": salida,
            "propuestas_abiertas": len(abiertas), "errores_recogida": datos["errores"]}


def diagnosticar(i):
    pista = "Suele ser algo temporal: reintentar suele bastar." if i["error"] and any(
        t.lower() in i["error"].lower() for t in TEMPORALES) else ""
    log = ""
    if i["accion"] and i["accion"]["tipo"] == "relanzar_workflow":
        log = GitHub().final_del_log(i["accion"]["repo"], i["accion"]["run"])
    if not (i["error"] or log):
        return pista
    texto = gemini(
        "Eres el técnico de un creador de contenido que no es programador. Su sistema publica en Instagram con Make "
        "y GitHub Actions. Explica en 2 frases cortas, en español de España y sin tecnicismos, qué ha fallado y si "
        "basta con reintentar o hay que cambiar algo (y qué). No uses markdown.\n\n"
        f"Problema: {i['texto']}\nError: {i['error'] or ''}\n{log}")
    return texto or pista


def proponer(incidencias, propuestas, tg):
    """Convierte las incidencias nuevas en propuestas y las manda por Telegram (cada una una sola vez)."""
    vistas = {p["clave"] for p in propuestas.values()}
    nombres = {p["id"]: f"{p['emoji']} {p['nombre']}" for p in proyectos()}
    diagnosticos = 0
    nuevas = 0
    for i in incidencias:
        if i["clave"] in vistas:
            continue
        diag = ""
        if i["nivel"] == "rojo" and diagnosticos < MAX_DIAGNOSTICOS:
            diag = diagnosticar(i)
            diagnosticos += 1
        p = {"id": pid(i["clave"]), "clave": i["clave"], "proyecto": i["proyecto"], "nivel": i["nivel"],
             "texto": i["texto"], "error": i["error"], "diagnostico": diag, "enlace": i["enlace"], "accion": i["accion"],
             "estado": "abierta" if i["accion"] else "aviso", "creada": iso(ahora())}
        icono = "🔴" if i["nivel"] == "rojo" else "🟠"
        msg = f"{icono} <b>{esc(nombres.get(i['proyecto'], 'Sin proyecto'))}</b>\n{esc(i['texto'])}"
        if diag:
            msg += f"\n\n🩺 {esc(diag)}"
        elif i["error"]:
            msg += f"\n\n<code>{esc(i['error'][:300])}</code>"
        if i["enlace"]:
            msg += f"\n<a href=\"{i['enlace']}\">Abrir</a>"
        botones = None
        if i["accion"]:
            msg += f"\n\n👉 Propuesta: <b>{esc(texto_accion(i['accion']))}</b>"
            botones = [[{"text": "✅ Hazlo", "callback_data": f"ok:{p['id']}"},
                        {"text": "🙈 Ignorar", "callback_data": f"no:{p['id']}"}]]
        # los avisos ámbar sin acción no se mandan uno a uno: salen en el resumen de la mañana
        if i["accion"] or i["nivel"] == "rojo":
            r = tg.enviar(msg, botones)
            p["message_id"] = (r.get("result") or {}).get("message_id")
        propuestas[p["id"]] = p
        anotar("propuesta" if i["accion"] else "aviso", id=p["id"], texto=i["texto"])
        nuevas += 1
    return nuevas


def texto_accion(a):
    return {
        "ejecutar_escenario": f"ejecutar ahora «{a.get('nombre')}» en Make",
        "activar_escenario": f"volver a encender «{a.get('nombre')}» en Make",
        "pausar_escenario": f"pausar «{a.get('nombre')}» en Make",
        "mover_escenario": f"mover «{a.get('nombre')}» a la carpeta {a.get('carpeta_nombre')}",
        "relanzar_workflow": f"relanzar los pasos fallidos de «{a.get('nombre')}» en GitHub",
    }.get(a["tipo"], a["tipo"])


def caducar(propuestas, tg):
    limite = ahora() - CADUCIDAD
    for p in propuestas.values():
        if p["estado"] == "abierta" and leer_fecha(p["creada"]) < limite:
            p["estado"] = "caducada"
            if p.get("message_id"):
                tg.editar(p["message_id"], f"⌛ <s>{esc(p['texto'])}</s>\nPropuesta caducada sin respuesta.")
    # se olvidan las propuestas de hace más de 15 días
    viejas = [k for k, p in propuestas.items() if leer_fecha(p["creada"]) < ahora() - timedelta(days=15)]
    for k in viejas:
        del propuestas[k]


def resumen_texto(estado):
    color = {"verde": "🟢", "ambar": "🟠", "rojo": "🔴"}
    lineas = [f"🛰 <b>Centro de mando</b> · {hora_madrid(ahora())}"]
    for p in estado["proyectos"]:
        m, g = p["make"], p["github"]
        lineas.append(f"\n{color[p['semaforo']]} <b>{esc(p['emoji'] + ' ' + p['nombre'])}</b>")
        rec = f" ({m['recuperados_36h']} se arreglaron solos al reintentar)" if m["recuperados_36h"] else ""
        lineas.append(f"   Make: {m['activos']}/{m['total']} activos · {m['ejecuciones_36h']} ejecuciones, {m['fallos_36h']} fallos{rec} (36 h)")
        lineas.append(f"   GitHub: {g['ejecuciones_36h']} ejecuciones, {g['fallidas_36h']} fallidas (36 h)")
        for i in p["incidencias"][:4]:
            lineas.append(f"   • {esc(i['texto'])}")
        if p["proximas"]:
            n = p["proximas"][0]
            lineas.append(f"   ⏭ {esc(n['nombre'])} · {hora_madrid(leer_fecha(n['cuando']))}")
    c = estado.get("creditos")
    if c:
        lineas.append(f"\n💳 Créditos Make: {c['usados']:,}/{c['limite']:,} ({c['pct']} %)".replace(",", "."))
    if estado["propuestas_abiertas"]:
        lineas.append(f"\n👉 Tienes {estado['propuestas_abiertas']} propuesta(s) esperando: /pendientes")
    if estado["errores_recogida"]:
        lineas.append("\n⚠️ No pude leer: " + esc("; ".join(estado["errores_recogida"])[:500]))
    return "\n".join(lineas)


def vigilar(forzar_resumen=False):
    tg = Telegram()
    propuestas = cargar("propuestas.json", {})
    datos = recoger()
    por_proyecto, incidencias = analizar(datos)
    cred = creditos(datos["org"])
    if cred and cred["pct"] >= 80:
        nivel = "rojo" if cred["pct"] >= 95 else "ambar"
        incidencias.append({"proyecto": None, "nivel": nivel, "texto": f"Llevas el {cred['pct']} % de los créditos de Make del mes",
                            "clave": f"make:creditos:{ahora().strftime('%Y-%m')}:{cred['pct'] // 5}", "accion": None,
                            "error": None, "enlace": None})
    caducar(propuestas, tg)
    nuevas = proponer(incidencias, propuestas, tg)
    estado = construir_estado(datos, por_proyecto, cred, propuestas)
    guardar("status.json", estado)
    guardar("propuestas.json", propuestas)

    # resumen de la mañana (una vez al día, a partir de las 08:00 de Madrid)
    control = cargar("control.json", {})
    from base import MADRID
    local = ahora().astimezone(MADRID)
    hoy = local.strftime("%Y-%m-%d")
    if forzar_resumen or (local.hour >= 8 and control.get("resumen") != hoy):
        tg.enviar(resumen_texto(estado))
        control["resumen"] = hoy
        guardar("control.json", control)
    print(f"Vigilancia: {len(incidencias)} incidencias, {nuevas} nuevas, errores de lectura: {len(datos['errores'])}")
    return estado


def agenda_github(p, dias=3):
    """Publicaciones que programa GitHub (no Make): franjas de content/calendario.json y el guion que toca en cada una."""
    cfg = p.get("calendario_github")
    if not cfg:
        return [], None
    from datetime import datetime as _dt
    from base import MADRID
    import json as _json
    gh = GitHub()
    try:
        cal = _json.loads(gh.archivo(p["repo"], cfg["archivo"]))
        rama, ruta = cfg["publicados"].split(":", 1)
        try:
            publicados = _json.loads(gh.archivo(p["repo"], ruta, rama))
        except ErrorHTTP:
            publicados = {}
    except (ErrorHTTP, ValueError) as e:
        return [], f"GitHub {p['repo']}, calendario: {e}"
    activo = str(cal.get("publicar", "")).lower() == "si" and bool(cal.get("inicio"))
    pendientes = [g for g in cal.get("orden", []) if g not in publicados]
    hoy = ahora().astimezone(MADRID).date()
    inicio = _dt.strptime(cal["inicio"], "%Y-%m-%d").date() if cal.get("inicio") else hoy
    huecos = []
    for d in range(dias):
        dia = hoy + timedelta(days=d)
        if dia < inicio:
            continue
        for franja, hora in sorted(cal.get("franjas", {}).items(), key=lambda x: x[1]):
            h, m = map(int, hora.split(":"))
            cuando = _dt(dia.year, dia.month, dia.day, h, m, tzinfo=MADRID)
            if cuando > ahora():
                huecos.append((franja, cuando))
    salida = []
    for (franja, cuando), gid in zip(huecos, pendientes + [None] * len(huecos)):
        titulo = ""
        if gid and len(salida) < 8:
            try:
                titulo = _json.loads(gh.archivo(p["repo"], cfg["guion"].format(id=gid))).get("titulo", "")
            except (ErrorHTTP, ValueError):
                pass
        nombre = f"{cfg.get('tipo', 'Publicación')} {franja} · " + (titulo or gid or "sin guion pendiente")
        salida.append({"nombre": nombre, "cuando": iso(cuando), "origen": "github", "activo": activo,
                       "enlace": f"https://github.com/{p['repo']}/actions/workflows/{cfg['workflow']}"})
    return salida, None
