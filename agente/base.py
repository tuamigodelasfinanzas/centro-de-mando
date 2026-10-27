"""Piezas comunes del centro de mando: configuración, estado y clientes de Make, GitHub, Telegram y Gemini.

Solo usa la librería estándar de Python, para que el workflow no tenga que instalar nada.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = json.loads((RAIZ / "config" / "proyectos.json").read_text(encoding="utf-8"))
ESTADO = Path(os.environ.get("CENTRO_ESTADO", RAIZ / "estado"))
MADRID = ZoneInfo("Europe/Madrid")
OWNER_REPO = os.environ.get("GITHUB_REPOSITORY", "tuamigodelasfinanzas/centro-de-mando")


def ahora():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leer_fecha(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hora_madrid(dt):
    return dt.astimezone(MADRID).strftime("%d/%m %H:%M")


# ---------- estado (vive en la rama "estado" del repo) ----------

def cargar(nombre, defecto):
    p = ESTADO / nombre
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def guardar(nombre, datos):
    ESTADO.mkdir(parents=True, exist_ok=True)
    (ESTADO / nombre).write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")


def anotar(evento, **datos):
    """Historial de todo lo que el agente propone y hace (se guardan las 500 últimas líneas)."""
    ESTADO.mkdir(parents=True, exist_ok=True)
    p = ESTADO / "historial.jsonl"
    lineas = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    lineas.append(json.dumps({"t": iso(ahora()), "evento": evento, **datos}, ensure_ascii=False))
    p.write_text("\n".join(lineas[-500:]) + "\n", encoding="utf-8")


# ---------- HTTP ----------

SECRETOS = [v for k, v in os.environ.items() if k.endswith(("TOKEN", "_KEY", "_PAT")) and v and len(v) > 8]


def limpiar(texto):
    """Quita cualquier secreto de un texto antes de enviarlo a Telegram, al panel o a Gemini."""
    texto = str(texto)
    for s in SECRETOS:
        texto = texto.replace(s, "***")
    return re.sub(r"(github_pat_|ghp_|AIza)[A-Za-z0-9_\-]{10,}", "***", texto)


class ErrorHTTP(Exception):
    pass


def http(metodo, url, cabeceras=None, cuerpo=None, intentos=3, crudo=False):
    datos = None
    cab = {"User-Agent": "centro-de-mando"}
    cab.update(cabeceras or {})
    if cuerpo is not None:
        datos = json.dumps(cuerpo).encode()
        cab.setdefault("Content-Type", "application/json")
    for n in range(intentos):
        req = urllib.request.Request(url, data=datos, headers=cab, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
                if crudo:
                    return b
                return json.loads(b) if b else {}
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")[:500]
            if e.code in (429, 500, 502, 503, 504) and n < intentos - 1:
                time.sleep(2 * (n + 1))
                continue
            raise ErrorHTTP(f"{e.code} {limpiar(txt)}") from None
        except urllib.error.URLError as e:
            if n < intentos - 1:
                time.sleep(2 * (n + 1))
                continue
            raise ErrorHTTP(f"sin conexión: {e.reason}") from None


# ---------- Make ----------

class Make:
    def __init__(self):
        self.token = os.environ.get("MAKE_API_TOKEN", "").strip()
        self.api = f"https://{CONFIG['make']['zona']}.make.com/api/v2"
        self.equipo = CONFIG["make"]["equipo"]

    def _cab(self):
        if not self.token:
            raise ErrorHTTP("falta el secret MAKE_API_TOKEN")
        return {"Authorization": f"Token {self.token}"}

    def get(self, ruta, **params):
        q = urllib.parse.urlencode(params, doseq=True)
        return http("GET", f"{self.api}{ruta}{'?' + q if q else ''}", self._cab())

    def escenarios(self):
        return self.get("/scenarios", teamId=self.equipo, **{"pg[limit]": 500}).get("scenarios", [])

    def webhooks(self):
        return self.get("/hooks", teamId=self.equipo).get("hooks", [])

    def ejecuciones(self, escenario_id, desde):
        r = self.get(f"/scenarios/{escenario_id}/logs", **{"from": int(desde.timestamp() * 1000), "pg[limit]": 50})
        logs = r.get("scenarioLogs", r if isinstance(r, list) else [])
        # el registro mezcla ejecuciones con cambios (modify, start, stop...): nos quedamos con las ejecuciones
        return [x for x in logs if x.get("eventType") == "EXECUTION_END"]

    def organizacion(self):
        return self.get(f"/organizations/{CONFIG['make']['organizacion']}").get("organization", {})

    # acciones (solo se llaman tras tu aprobación en Telegram)
    def ejecutar(self, escenario_id):
        return http("POST", f"{self.api}/scenarios/{escenario_id}/run", self._cab(), {"responsive": False})

    def activar(self, escenario_id):
        return http("POST", f"{self.api}/scenarios/{escenario_id}/start", self._cab(), {})

    def pausar(self, escenario_id):
        return http("POST", f"{self.api}/scenarios/{escenario_id}/stop", self._cab(), {})

    def mover(self, escenario_id, carpeta):
        return http("PATCH", f"{self.api}/scenarios/{escenario_id}?confirmed=true", self._cab(), {"folderId": carpeta})


def url_make_escenario(escenario_id):
    return f"https://{CONFIG['make']['zona']}.make.com/{CONFIG['make']['organizacion']}/scenarios/{escenario_id}/edit"


# ---------- GitHub ----------

class GitHub:
    def __init__(self):
        # GH_PAT puede relanzar workflows en tus otros repos; sin él solo se puede leer.
        self.token = os.environ.get("GH_PAT", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()
        self.puede_actuar = bool(os.environ.get("GH_PAT", "").strip())

    def _cab(self):
        c = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            c["Authorization"] = f"Bearer {self.token}"
        return c

    def get(self, ruta, **params):
        q = urllib.parse.urlencode(params)
        return http("GET", f"https://api.github.com{ruta}{'?' + q if q else ''}", self._cab())

    def ejecuciones(self, repo, desde):
        r = self.get(f"/repos/{repo}/actions/runs", per_page=100, created=f">={iso(desde)}")
        return r.get("workflow_runs", [])

    def final_del_log(self, repo, run_id, lineas=40):
        """Últimas líneas del primer job fallido, para que Gemini explique el error."""
        try:
            jobs = self.get(f"/repos/{repo}/actions/runs/{run_id}/jobs").get("jobs", [])
            job = next((j for j in jobs if j.get("conclusion") == "failure"), None)
            if not job:
                return ""
            paso = next((s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"), "")
            log = http("GET", f"https://api.github.com/repos/{repo}/actions/jobs/{job['id']}/logs", self._cab(), crudo=True)
            texto = log.decode(errors="replace").splitlines()[-lineas:]
            texto = [re.sub(r"^\S+Z ", "", l) for l in texto]
            return limpiar(f"Job «{job['name']}», paso «{paso}»:\n" + "\n".join(texto))
        except ErrorHTTP:
            return ""

    def relanzar_fallidos(self, repo, run_id):
        if not self.puede_actuar:
            raise ErrorHTTP("falta el secret GH_PAT para relanzar workflows de otros repos")
        return http("POST", f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs", self._cab(), {})


# ---------- Telegram ----------

class Telegram:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.ultimo_error = ""

    @property
    def listo(self):
        return bool(self.token and self.chat)

    def llamar(self, metodo, **datos):
        if not self.token:
            print(f"[telegram sin configurar] {metodo}: {json.dumps(datos, ensure_ascii=False)[:600]}")
            return {}
        try:
            return http("POST", f"https://api.telegram.org/bot{self.token}/{metodo}", cuerpo=datos, intentos=2)
        except ErrorHTTP as e:
            print(f"::warning::Telegram {metodo}: {e}")
            self.ultimo_error = f"{metodo}: {e}"
            return {}

    def enviar(self, texto, botones=None):
        d = {"chat_id": self.chat, "text": texto[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if botones:
            d["reply_markup"] = {"inline_keyboard": botones}
        return self.llamar("sendMessage", **d)

    def editar(self, message_id, texto):
        return self.llamar("editMessageText", chat_id=self.chat, message_id=message_id, text=texto[:4000],
                           parse_mode="HTML", disable_web_page_preview=True)


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Gemini (opcional: explica errores y responde preguntas) ----------

def gemini(prompt, maximo=600):
    clave = os.environ.get("GEMINI_API_KEY", "").strip()
    if not clave:
        return ""
    modelo = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
    cuerpo = {
        "contents": [{"parts": [{"text": limpiar(prompt)}]}],
        "generationConfig": {"maxOutputTokens": maximo, "temperature": 0.3},
    }
    try:
        r = http("POST", url, {"x-goog-api-key": clave}, cuerpo, intentos=2)
        partes = r["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in partes).strip()
    except (ErrorHTTP, KeyError, IndexError) as e:
        print(f"::warning::Gemini no respondió: {e}")
        return ""


# ---------- proyectos ----------

def proyectos():
    return CONFIG["proyectos"]


def proyecto_por_carpeta(carpeta_id):
    return next((p for p in proyectos() if p["carpeta_make"] == carpeta_id), None)


def proyecto_por_nombre_escenario(nombre):
    """El proyecto al que pertenece un escenario por su nombre (el prefijo más largo que encaje)."""
    mejor, largo = None, 0
    for p in proyectos():
        for pre in p["prefijos"]:
            if nombre.lower().startswith(pre.lower()) and len(pre) > largo:
                mejor, largo = p, len(pre)
    return mejor


def proyecto_por_texto(texto):
    import unicodedata
    t = "".join(c for c in unicodedata.normalize("NFD", texto.lower().strip()) if unicodedata.category(c) != "Mn")
    for p in proyectos():
        if t == p["id"] or t in p["alias"] or t in p["nombre"].lower():
            return p
    return None
