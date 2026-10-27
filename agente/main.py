"""Punto de entrada. Cada vuelta del workflow:
  1. atiende tus mensajes y botones de Telegram (aprobaciones, comandos, preguntas)
  2. si toca (una vez por hora, o si lo pides), vigila Make y GitHub, propone arreglos y actualiza el panel

Uso: python agente/main.py [ciclo|vigilar|resumen|bot]
"""
import sys

from base import ahora, cargar, guardar, iso, leer_fecha
from bot import atender
from vigilar import vigilar

CADA = 55 * 60  # segundos entre vigilancias completas


def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else "ciclo"
    if modo == "bot":
        atender()
        return
    if modo in ("vigilar", "resumen"):
        atender()
        vigilar(forzar_resumen=modo == "resumen")
        _marcar()
        return
    # ciclo normal (cada 10 min)
    atender()
    ultima = leer_fecha(cargar("control.json", {}).get("vigilancia"))
    if not ultima or (ahora() - ultima).total_seconds() >= CADA:
        vigilar()
        _marcar()
    else:
        print("Vigilancia reciente: esta vuelta solo atiende Telegram.")
    atender()  # por si llegó algo mientras vigilaba


def _marcar():
    c = cargar("control.json", {})
    c["vigilancia"] = iso(ahora())
    guardar("control.json", c)


if __name__ == "__main__":
    main()
