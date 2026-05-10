#!/usr/bin/env python3
"""
Clasificaciones RFFM — Preferente Alevín Fútbol 11
Descarga la clasificación, analiza el descenso por coeficiente y simula
escenarios de permanencia para un equipo concreto.

Normativa de descenso (Bases de Ascensos y Descensos RFFM F11):
  - Grupos de 14 equipos: descienden los 3 últimos (puestos 12, 13, 14).
  - Puesto 11 de cada grupo: se comparan por coeficiente (Pts / PJ).
    Los 4 equipos undécimos con peor coeficiente también descienden.
    Ref: https://www.rffm.es/federacion-rffm/documentacion-y-circulares/normativa-y-reglamentos

API RFFM:
  - Grupos:   GET /api/groups?competicion={id}
  - Jornadas: GET /api/group-rounds?idGroup={id}&fetchBy=standings
  - Clasif.:  GET /api/standings?idGroup={id}&round={n}
"""

import sys
import json
import time
import argparse
from itertools import product
from pathlib import Path
from typing import Optional

import requests


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

BASE_URL            = "https://www.rffm.es"
COMPETICION_DEFAULT = "24037708"   # Preferente Alevín Fútbol 11
GRUPO_7_ID          = "24037715"   # Grupo 7 (por defecto)

DESCENSO_DIRECTO_N = 3   # últimas N posiciones → descenso directo
DESCENSO_COEF_N    = 4   # de los undécimos, los N con peor coef descienden

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://www.rffm.es/",
}

RESULT_PTS = {"G": 3, "E": 1, "D": 0}


# ══════════════════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_json(
    session: requests.Session,
    path: str,
    params: dict,
    retries: int = 3,
) -> Optional[dict | list]:
    url = BASE_URL + "/" + path.lstrip("/")
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
            else:
                print(f"  ⚠ {url}: {exc}", file=sys.stderr)
        except ValueError as exc:
            print(f"  ⚠ JSON inválido en {url}: {exc}", file=sys.stderr)
            return None
    return None


# ══════════════════════════════════════════════════════════════════════════════
# DESCUBRIMIENTO: GRUPOS Y JORNADA
# ══════════════════════════════════════════════════════════════════════════════

def discover_groups(
    session: requests.Session,
    competicion: str,
) -> list[dict]:
    """
    Devuelve los grupos de la competición desde /api/groups.
    Cada elemento: {id, nombre, total_jornadas, total_equipos}
    """
    data = fetch_json(session, "/api/groups", {"competicion": competicion})
    if not data or not isinstance(data, list):
        print(f"  ⚠ No se pudieron obtener los grupos de {competicion}", file=sys.stderr)
        return []
    return [
        {
            "id":             g["codigo"],
            "nombre":         g["nombre"],
            "total_jornadas": int(g.get("total_jornadas", 26)),
            "total_equipos":  int(g.get("total_equipos", 14)),
        }
        for g in data
        if g.get("ver_clasificacion") == "1"
    ]


def discover_current_round(
    session: requests.Session,
    grupo_id: str,
) -> int:
    """
    Obtiene la jornada actual del grupo desde /api/group-rounds.
    Devuelve currentRound; si no, el máximo codjornada disponible.
    """
    data = fetch_json(session, "/api/group-rounds", {
        "idGroup": grupo_id,
        "fetchBy": "standings",
    })
    if data and isinstance(data, dict):
        if data.get("currentRound"):
            return int(data["currentRound"])
        jornadas = data.get("jornadas", [])
        if jornadas:
            return max(int(j["codjornada"]) for j in jornadas)
    print(f"  ⚠ No se pudo obtener la jornada actual del grupo {grupo_id}", file=sys.stderr)
    return 1


# ══════════════════════════════════════════════════════════════════════════════
# PARSEO DE CLASIFICACIÓN (JSON)
# ══════════════════════════════════════════════════════════════════════════════

def parse_standings(data: Optional[dict]) -> list[dict]:
    """
    Convierte la respuesta de /api/standings al formato interno:
    {pos, nombre, pj, pg, pe, pp, gf, gc, dg, pts}
    """
    if not data or not isinstance(data, dict):
        return []
    clasificacion = data.get("clasificacion", [])
    if not clasificacion:
        return []

    teams = []
    for t in clasificacion:
        try:
            gf = int(t["goles_a_favor"])
            gc = int(t["goles_en_contra"])
            teams.append({
                "pos":    int(t["posicion"]),
                "nombre": t["nombre"],
                "pj":     int(t["jugados"]),
                "pg":     int(t["ganados"]),
                "pe":     int(t["empatados"]),
                "pp":     int(t["perdidos"]),
                "gf":     gf,
                "gc":     gc,
                "dg":     gf - gc,
                "pts":    int(t["puntos"]),
            })
        except (KeyError, ValueError):
            continue

    teams.sort(key=lambda t: t["pos"])
    return teams


# ══════════════════════════════════════════════════════════════════════════════
# COEFICIENTE
# ══════════════════════════════════════════════════════════════════════════════

def coef(pts: int, pj: int) -> float:
    return pts / pj if pj > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS DE DESCENSO
# ══════════════════════════════════════════════════════════════════════════════

def build_relegation_analysis(all_standings: dict[str, list[dict]]) -> dict:
    direct: list[dict] = []
    eleventh: list[dict] = []

    for gid, teams in all_standings.items():
        n = len(teams)
        for t in teams[n - DESCENSO_DIRECTO_N:]:
            direct.append({**t, "grupo": gid})
        if n >= 11:
            t11 = teams[10]
            eleventh.append({
                **t11,
                "grupo": gid,
                "coef": coef(t11["pts"], t11["pj"]),
            })

    eleventh.sort(key=lambda x: x["coef"])
    return {
        "direct":           direct,
        "eleventh_sorted":  eleventh,
        "relegated_by_coef": eleventh[:DESCENSO_COEF_N],
        "safe_by_coef":     eleventh[DESCENSO_COEF_N:],
    }


# ══════════════════════════════════════════════════════════════════════════════
# SIMULACIÓN DE ESCENARIOS
# ══════════════════════════════════════════════════════════════════════════════

def _position_range(
    teams: list[dict],
    target_nombre: str,
    new_pts: int,
    remaining: int,
) -> tuple[int, int]:
    others    = [t for t in teams if t["nombre"] != target_nombre]
    best_pos  = sum(1 for t in others if t["pts"] > new_pts) + 1
    worst_pos = sum(1 for t in others if t["pts"] + remaining * 3 >= new_pts) + 1
    return best_pos, worst_pos


def _scenario_status(best_pos: int, worst_pos: int, n_teams: int) -> tuple[str, str]:
    safe_max = n_teams - DESCENSO_DIRECTO_N   # posición 11 para grupos de 14

    best_label  = ("✅ SEGURO"     if best_pos  <= safe_max - 1 else
                   "⚠ ZONA COEF"  if best_pos  == safe_max     else
                   "❌ DESCENSO")
    worst_label = ("✅ GARANTIZADO" if worst_pos <= safe_max - 1 else
                   "⚠ ZONA COEF"   if worst_pos == safe_max     else
                   "❌ DESCENSO POSIBLE")
    return worst_label, best_label


def simulate(
    teams: list[dict],
    target_nombre: str,
    remaining: int,
    all_standings: dict[str, list[dict]],
    grupo_id: str,
) -> None:
    target = next(
        (t for t in teams if target_nombre.lower() in t["nombre"].lower()), None
    )
    if not target:
        print(f"\n  ❌ Equipo '{target_nombre}' no encontrado.")
        print(f"     Equipos: {', '.join(t['nombre'] for t in teams)}")
        return

    n          = len(teams)
    pos_actual = teams.index(target) + 1
    safe_max   = n - DESCENSO_DIRECTO_N

    print(f"\n{'═'*72}")
    print(f"  SIMULACIÓN — {target['nombre'].upper()}")
    print(f"{'═'*72}")
    print(
        f"  Posición: {pos_actual}/{n}  |  Pts: {target['pts']}  |  "
        f"PJ: {target['pj']}  |  Coef: {coef(target['pts'], target['pj']):.3f}"
    )
    print(f"  Jornadas restantes: {remaining}")

    # Undécimos de otros grupos (para zona coeficiente)
    others_11th = [
        {
            "grupo":    gid,
            "nombre":   gteams[10]["nombre"],
            "pts":      gteams[10]["pts"],
            "pj":       gteams[10]["pj"],
            "coef_now": coef(gteams[10]["pts"], gteams[10]["pj"]),
        }
        for gid, gteams in all_standings.items()
        if gid != grupo_id and len(gteams) >= 11
    ]

    scenarios = list(product(RESULT_PTS.keys(), repeat=remaining))
    results   = []
    for scenario in scenarios:
        extra    = sum(RESULT_PTS[r] for r in scenario)
        new_pts  = target["pts"] + extra
        new_pj   = target["pj"] + remaining
        new_coef = coef(new_pts, new_pj)

        best_pos, worst_pos = _position_range(teams, target["nombre"], new_pts, remaining)
        worst_label, best_label = _scenario_status(best_pos, worst_pos, n)

        coef_rank = None
        if others_11th:
            coefs_worst = sorted(
                o["coef_now"] + (3 * remaining / (o["pj"] + remaining))
                if o["pj"] + remaining > 0 else 0.0
                for o in others_11th
            )
            coef_rank = sum(1 for c in coefs_worst if c > new_coef) + 1

        results.append({
            "label":       "".join(scenario),
            "extra":       extra,
            "new_pts":     new_pts,
            "new_pj":      new_pj,
            "new_coef":    new_coef,
            "best_pos":    best_pos,
            "worst_pos":   worst_pos,
            "worst_label": worst_label,
            "best_label":  best_label,
            "coef_rank":   coef_rank,
        })

    results.sort(key=lambda r: (-r["new_pts"], r["label"]))

    guaranteed  = [r for r in results if r["worst_label"] == "✅ GARANTIZADO"]
    coef_zone   = [r for r in results if "COEF" in r["worst_label"] or
                   ("COEF" in r["best_label"] and "GARANTIZADO" not in r["worst_label"])]
    conditional = [r for r in results if "DESCENSO" in r["worst_label"]
                   and r["best_label"] == "✅ SEGURO"]
    relegated   = [r for r in results if r["best_label"] == "❌ DESCENSO"]

    def fmt_row(r: dict) -> str:
        gs = r["label"].count("G")
        es = r["label"].count("E")
        ds = r["label"].count("D")
        ci = f" | Rank undécimos: {r['coef_rank']}" if r["coef_rank"] else ""
        return (
            f"     {r['label']}  ({gs}V {es}E {ds}D)  → "
            f"{r['new_pts']} pts | Coef: {r['new_coef']:.3f}"
            f" | Pos: {r['best_pos']}–{r['worst_pos']}{ci}"
        )

    if guaranteed:
        print(f"\n  ✅ PERMANENCIA GARANTIZADA (independiente de otros resultados):")
        for r in guaranteed:
            print(fmt_row(r))
    if conditional:
        print(f"\n  🔶 PERMANENCIA POSIBLE (depende de resultados de otros equipos):")
        for r in conditional:
            print(fmt_row(r))
    if coef_zone:
        print(f"\n  ⚠  ZONA COEFICIENTE (terminarían undécimos, compiten por coef):")
        for r in coef_zone:
            print(fmt_row(r))
    if relegated:
        print(f"\n  ❌ DESCENSO PROBABLE incluso en el mejor caso:")
        for r in relegated:
            print(fmt_row(r))

    min_pts = min((r["new_pts"] for r in guaranteed), default=None)
    if min_pts is not None:
        print(f"\n  → Mínimo para GARANTIZAR permanencia: {min_pts - target['pts']} pts más "
              f"(total {min_pts} pts)")
    else:
        print(f"\n  → No existe resultado que garantice permanencia matemáticamente.")


# ══════════════════════════════════════════════════════════════════════════════
# SALIDA POR PANTALLA
# ══════════════════════════════════════════════════════════════════════════════

def print_standings_table(
    teams: list[dict],
    titulo: str,
    highlight: str = "",
) -> None:
    n        = len(teams)
    safe_max = n - DESCENSO_DIRECTO_N

    print(f"\n{'═'*75}")
    print(f"  {titulo}")
    print(f"{'═'*75}")
    print(
        f"{'Pos':>3}  {'Equipo':<33} "
        f"{'PJ':>3} {'PG':>3} {'PE':>3} {'PP':>3} "
        f"{'GF':>4} {'GC':>4} {'DG':>5} {'Pts':>4} {'Coef':>6}"
    )
    print(f"{'─'*75}")

    for i, t in enumerate(teams):
        pos  = i + 1
        c    = coef(t["pts"], t["pj"])
        zone = " ↓" if pos > safe_max else (" ?" if pos == safe_max else "  ")
        mark = " ◄" if highlight and highlight.lower() in t["nombre"].lower() else ""
        print(
            f"{pos:>3}{zone} {t['nombre']:<33} "
            f"{t['pj']:>3} {t['pg']:>3} {t['pe']:>3} {t['pp']:>3} "
            f"{t['gf']:>4} {t['gc']:>4} {t['dg']:>+5} {t['pts']:>4} "
            f"{c:>6.3f}{mark}"
        )

    print(f"{'─'*75}")
    print(f"  ↓ Descenso directo  ? Zona coeficiente (11ºs, comparan coef entre grupos)")


def print_relegation_analysis(analysis: dict, n_grupos: int) -> None:
    print(f"\n{'═'*75}")
    print(f"  ANÁLISIS DE DESCENSO — {n_grupos} grupo(s) descargado(s)")
    print(f"{'═'*75}")

    if analysis["direct"]:
        print(f"\n  📉 DESCENSO DIRECTO (últimas {DESCENSO_DIRECTO_N} posiciones):")
        for t in analysis["direct"]:
            c = coef(t["pts"], t["pj"])
            print(
                f"     [{t['grupo'][-6:]}] {t['nombre']:<32} "
                f"Pts:{t['pts']:>3}  PJ:{t['pj']:>2}  Coef:{c:.3f}"
            )

    if analysis["eleventh_sorted"]:
        print(
            f"\n  ⚠  ZONA COEFICIENTE — undécimos "
            f"(descienden los {DESCENSO_COEF_N} con peor coef):"
        )
        for i, t in enumerate(analysis["eleventh_sorted"]):
            estado = "↓ DESCIENDE" if i < DESCENSO_COEF_N else "✅ PERMANECE"
            print(
                f"     {estado}  [{t['grupo'][-6:]}] {t['nombre']:<28} "
                f"Coef:{t['coef']:.3f}  (Pts:{t['pts']:>3}  PJ:{t['pj']:>2})"
            )
    elif n_grupos == 1:
        print(f"\n  ℹ  Para análisis completo de coeficiente usa --todos-grupos")


# ══════════════════════════════════════════════════════════════════════════════
# BÚSQUEDA DE EQUIPO
# ══════════════════════════════════════════════════════════════════════════════

def find_team_group(
    all_standings: dict[str, list[dict]],
    nombre_parcial: str,
) -> tuple[Optional[str], Optional[dict]]:
    needle  = nombre_parcial.lower()
    matches = [
        (gid, t)
        for gid, teams in all_standings.items()
        for t in teams
        if needle in t["nombre"].lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        exact = [(g, t) for g, t in matches if t["nombre"].lower() == needle]
        if exact:
            return exact[0]
        print(f"  ⚠ Varios equipos coinciden con '{nombre_parcial}':")
        for g, t in matches:
            print(f"     [{g}] {t['nombre']}")
        return matches[0]
    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# CACHÉ
# ══════════════════════════════════════════════════════════════════════════════

def load_cache(path: Path) -> Optional[dict]:
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_cache(path: Path, data: dict) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  ⚠ No se pudo guardar la caché: {e}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Clasificaciones RFFM — Preferente Alevín F11\n"
            "Descarga clasificación, analiza descenso y simula escenarios.\n\n"
            "Ejemplos:\n"
            "  python rffm_clasificacion.py --todos-grupos -e Ivero\n"
            "  python rffm_clasificacion.py --todos-grupos -e Ivero --cache\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--equipo", "-e", default="",
        help="Nombre (parcial) del equipo a simular.")
    p.add_argument("--grupo", "-g", default=GRUPO_7_ID,
        help=f"ID del grupo a mostrar (default: {GRUPO_7_ID} = Grupo 7).")
    p.add_argument("--competicion", "-c", default=COMPETICION_DEFAULT,
        help=f"ID de la competición (default: {COMPETICION_DEFAULT}).")
    p.add_argument("--total-jornadas", "-j", type=int, default=0,
        help="Total de jornadas (0 = usar el valor de la API).")
    p.add_argument("--todos-grupos", action="store_true",
        help="Descarga los 8 grupos (necesario para análisis de coef. cruzado).")
    p.add_argument("--cache", action="store_true",
        help="Usa clasificacion_cache.json si existe.")
    p.add_argument("--borrar-cache", action="store_true",
        help="Elimina la caché antes de descargar.")
    return p.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args       = parse_args()
    cache_file = Path("clasificacion_cache.json")

    if args.borrar_cache and cache_file.exists():
        cache_file.unlink()
        print("🗑  Caché eliminada.")

    print("═" * 75)
    print("  RFFM · Preferente Alevín Fútbol 11 · Análisis de Descenso")
    print("═" * 75)

    session      = make_session()
    all_standings: dict[str, list[dict]] = {}
    groups_meta:   dict[str, dict]       = {}

    # ── Caché ────────────────────────────────────────────────────────────────
    if args.cache:
        cached = load_cache(cache_file)
        if cached:
            all_standings = cached.get("standings", {})
            groups_meta   = cached.get("groups_meta", {})
            jornada_cache = groups_meta.get(args.grupo, {}).get("jornada_actual", "?")
            print(f"\n📂 Datos cargados desde caché (Jornada {jornada_cache})")

    # ── Descarga online ───────────────────────────────────────────────────────
    if not all_standings:
        if args.todos_grupos:
            print(f"\n🔍 Obteniendo grupos de la competición...")
            groups = discover_groups(session, args.competicion)
            if not groups:
                print("❌ No se pudieron obtener los grupos.")
                sys.exit(1)
            if not any(g["id"] == args.grupo for g in groups):
                groups.append({"id": args.grupo, "nombre": "Grupo 7",
                                "total_jornadas": 26, "total_equipos": 14})
            print(f"  Grupos a descargar: {len(groups)}")
        else:
            groups = [{"id": args.grupo, "nombre": "Grupo objetivo",
                       "total_jornadas": args.total_jornadas or 26,
                       "total_equipos": 14}]

        print(f"\n📊 Descargando clasificaciones...")
        for g in groups:
            gid = g["id"]
            print(f"  · {g['nombre']} ({gid}) ...", end=" ", flush=True)

            jornada_actual = discover_current_round(session, gid)
            data  = fetch_json(session, "/api/standings", {
                "idGroup": gid,
                "round":   jornada_actual,
            })
            teams = parse_standings(data)

            if teams:
                all_standings[gid] = teams
                groups_meta[gid]   = {
                    "nombre":          g["nombre"],
                    "total_jornadas":  (args.total_jornadas
                                        if args.total_jornadas and gid == args.grupo
                                        else g["total_jornadas"]),
                    "total_equipos":   g["total_equipos"],
                    "jornada_actual":  jornada_actual,
                }
                print(f"✓ {len(teams)} equipos (J{jornada_actual})")
            else:
                print("✗ sin datos")

            time.sleep(0.3)

        save_cache(cache_file, {"standings": all_standings, "groups_meta": groups_meta})
        print(f"\n💾 Datos guardados en {cache_file}")

    if not all_standings:
        print("\n❌ No se pudieron obtener datos.")
        sys.exit(1)

    # ── Localizar equipo y su grupo ───────────────────────────────────────────
    equipo_nombre   = args.equipo
    grupo_id_equipo = args.grupo

    if equipo_nombre:
        found_gid, found_team = find_team_group(all_standings, equipo_nombre)
        if found_team:
            grupo_id_equipo = found_gid
            equipo_nombre   = found_team["nombre"]
            if found_gid != args.grupo:
                print(f"\n  🔎 '{equipo_nombre}' encontrado en grupo {found_gid}")
        else:
            print(f"\n  ⚠ No se encontró '{equipo_nombre}'.")
            if not args.todos_grupos:
                print("     Prueba con --todos-grupos para buscar en los 8 grupos.")

    # ── Jornadas restantes ────────────────────────────────────────────────────
    meta           = groups_meta.get(grupo_id_equipo, {})
    total_jornadas = (args.total_jornadas if args.total_jornadas
                      else meta.get("total_jornadas", 26))
    jornada_actual = meta.get("jornada_actual", 1)
    remaining      = max(0, total_jornadas - jornada_actual)

    # ── Clasificación del grupo objetivo ─────────────────────────────────────
    grupo_teams          = all_standings.get(grupo_id_equipo, [])
    grupo_nombre_display = meta.get("nombre", f"Grupo {grupo_id_equipo}")

    if grupo_teams:
        print_standings_table(
            grupo_teams,
            f"CLASIFICACIÓN {grupo_nombre_display.upper()} — PREFERENTE ALEVÍN "
            f"— Jornada {jornada_actual} ({remaining} jornada(s) restante(s))",
            highlight=equipo_nombre,
        )
    else:
        print(f"\n  ⚠ No hay datos para el grupo {grupo_id_equipo}")

    # ── Análisis de descenso ──────────────────────────────────────────────────
    analysis = build_relegation_analysis(all_standings)
    print_relegation_analysis(analysis, len(all_standings))

    # ── Simulación ────────────────────────────────────────────────────────────
    if equipo_nombre and grupo_teams and remaining > 0:
        simulate(grupo_teams, equipo_nombre, remaining, all_standings, grupo_id_equipo)
    elif equipo_nombre and remaining == 0:
        print(f"\n  ℹ La competición ha finalizado. No quedan jornadas.")
    elif not equipo_nombre:
        print(
            f"\n  ℹ Usa --equipo \"Nombre\" para simular escenarios. "
            f"Con --todos-grupos busca en todos los grupos."
        )


if __name__ == "__main__":
    main()
