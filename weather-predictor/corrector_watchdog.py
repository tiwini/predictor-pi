"""Corrector watchdog — vigilancia diaria del corrector de nivel.

Corre a diario por cron en el Pi. Mismo patrón que `brier_watchdog.py`: query
SQL, informe en disco, y push ntfy **sólo si hay algo que decir**. Cero coste,
cero LLM.

El criterio NO vive aquí: se importa de `investigacion/seguimiento_corrector.py`,
que es donde quedó escrito antes de que hubiera resultados. Duplicarlo sería
dejar que el umbral que alerta y el que se mira a mano diverjan.

## Cuándo empuja

- 🔴 **REVERTIR** en cualquier estación → push siempre, cada día que dure.
- Cualquier **cambio de estado** respecto al último run (verde→amarillo,
  n_bajo→verde…) → push una vez.
- Todo lo demás → sólo escribe el informe, sin ruido.

El estado previo se guarda en `corrector_watchdog/estado.json`. Si el fichero no
existe (primer run) no se empuja por "cambio": no tiene sentido alertar de un
cambio contra la nada.

Salida: `~/predictor-pi/corrector_watchdog/corrector_YYYY-MM-DD.md`
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR.parent / "investigacion"))

import level_corrector as lc          # noqa: E402
import seguimiento_corrector as sg    # noqa: E402

OUT_DIR = Path.home() / "predictor-pi" / "corrector_watchdog"
STATE_FILE = OUT_DIR / "estado.json"
NTFY_ENV = Path.home() / ".config" / "ntfy.env"


def _load_ntfy_topic() -> str:
    if not NTFY_ENV.exists():
        return ""
    for ln in NTFY_ENV.read_text().splitlines():
        if ln.startswith("NTFY_TOPIC="):
            return ln.split("=", 1)[1].strip()
    return ""


def _push_ntfy(title: str, msg: str) -> None:
    topic = _load_ntfy_topic()
    if not topic:
        print("[corrector_watchdog] sin NTFY_TOPIC, no se empuja")
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=msg.encode("utf-8"),
            headers={"Title": title, "Priority": "high",
                     "Tags": "warning,thermometer"},
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[corrector_watchdog] ntfy pushed: {title}")
    except urllib.error.URLError as e:
        print(f"[corrector_watchdog] ntfy push failed: {e}", file=sys.stderr)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    an = sqlite3.connect(f"file:{PROJECT_DIR / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect(f"file:{PROJECT_DIR / 'calibration.db'}?mode=ro",
                          uri=True)

    estados = {st: sg.estado_de(an, cal, st)
               for st in sorted(lc.ENABLED_STATIONS)}

    hoy = date.today().isoformat()
    cuerpo = [f"# Corrector de nivel — {hoy}", ""]
    for e in estados.values():
        cuerpo.append("```")
        cuerpo.append(sg.render(e))
        cuerpo.append("```")
    informe = OUT_DIR / f"corrector_{hoy}.md"
    informe.write_text("\n".join(cuerpo))
    print(f"[corrector_watchdog] escrito {informe}")

    previo = {}
    if STATE_FILE.exists():
        try:
            previo = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            previo = {}

    rojos = [st for st, e in estados.items() if e["estado"] == "rojo"]
    cambios = [f"{st}: {previo[st]} → {e['estado']}"
               for st, e in estados.items()
               if st in previo and previo[st] != e["estado"]]

    if rojos:
        _push_ntfy(
            f"🔴 Corrector: revertir {', '.join(rojos)}",
            "\n".join(estados[st]["veredicto"] for st in rojos)
            + "\n\nSobre-corrige y ya no compensa. Ver "
              f"corrector_watchdog/corrector_{hoy}.md")
    elif cambios:
        _push_ntfy("Corrector de nivel: cambio de estado", "\n".join(cambios))
    else:
        print("[corrector_watchdog] sin cambios ni rojos, no se empuja")

    STATE_FILE.write_text(json.dumps(
        {st: e["estado"] for st, e in estados.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
