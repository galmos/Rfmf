#!/usr/bin/env python3
"""
Clasificaciones RFFM — Preferente Alevín Fútbol 11
Descarga la clasificación, analiza el descenso por coeficiente y simula
escenarios de permanencia/ascenso para un equipo concreto.

Normativa de descenso (Bases de Ascensos y Descensos RFFM F11):
  - Grupos de 14 equipos: descienden los 3 últimos (puestos 12, 13, 14).
  - Puesto 11 de cada grupo: se comparan por coeficiente (Pts / PJ).
    Los 4 equipos undécimos con peor coeficiente también descienden.

API RFFM:
  - Grupos:    GET /api/groups?competicion={id}
  - Jornadas:  GET /api/group-rounds?idGroup={id}&fetchBy=standings
  - Clasif.:   GET /api/standings?idGroup={id}&round={n}
  - Partidos:  GET /api/results?idGroup={id}&round={n}
"""

import sys
import json
import time
import argparse
from copy import deepcopy
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
ASCENSO_N          = 2   # top N posiciones → ascenso

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://www.rffm.es/",
}


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
# DESCUBRIMIENTO
# ══════════════════════════════════════════════════════════════════════════════

def discover_groups(session: requests.Session, competicion: str) -> list[dict]:
    data = fetch_json(session, "/api/groups", {"competicion": competicion})
    if not data or not isinstance(data, list):
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


def discover_current_round(session: requests.Session, grupo_id: str) -> int:
    data = fetch_json(session, "/api/group-rounds", {
        "idGroup": grupo_id, "fetchBy": "standings",
    })
    if data and isinstance(data, dict):
        if data.get("currentRound"):
            return int(data["currentRound"])
        jornadas = data.get("jornadas", [])
        if jornadas:
            return max(int(j["codjornada"]) for j in jornadas)
    return 1


def fetch_round_fixtures(session: requests.Session, grupo_id: str, round_num: int) -> list[dict]:
    """Devuelve los partidos NO jugados de una jornada concreta."""
    data = fetch_json(session, "/api/results", {"idGroup": grupo_id, "round": round_num})
    if not data or "partidos" not in data:
        return []
    return [
        {
            "local":     p["Nombre_equipo_local"],
            "visitante": p["Nombre_equipo_visitante"],
            "fecha":     p.get("fecha", ""),
            "hora":      p.get("hora", ""),
        }
        for p in data["partidos"]
        if p.get("Goles_casa", "") == "" and p.get("Retirado_local", "0") == "0"
    ]


def get_remaining_fixtures(
    session: requests.Session,
    grupo_id: str,
    current_round: int,
    total_rounds: int,
) -> dict[int, list[dict]]:
    """Retorna {jornada: [partidos]} para todas las jornadas pendientes."""
    remaining: dict[int, list[dict]] = {}
    for r in range(current_round + 1, total_rounds + 1):
        fixtures = fetch_round_fixtures(session, grupo_id, r)
        if fixtures:
            remaining[r] = fixtures
        time.sleep(0.2)
    return remaining


# ══════════════════════════════════════════════════════════════════════════════
# PARSEO DE CLASIFICACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def parse_standings(data: Optional[dict]) -> list[dict]:
    if not data or not isinstance(data, dict):
        return []
    clasificacion = data.get("clasificacion", [])
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
# MOTOR DE SIMULACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def coef(pts: int, pj: int) -> float:
    return pts / pj if pj > 0 else 0.0


def apply_result(d: dict, local: str, visitante: str, result: str) -> None:
    """Aplica G/E/D (perspectiva local) sobre el dict de clasificación."""
    l, v = d.get(local), d.get(visitante)
    if not l or not v:
        return
    l["pj"] += 1; v["pj"] += 1
    if result == "G":
        l["pts"] += 3; l["pg"] += 1; v["pp"] += 1
    elif result == "E":
        l["pts"] += 1; l["pe"] += 1; v["pts"] += 1; v["pe"] += 1
    else:
        v["pts"] += 3; v["pg"] += 1; l["pp"] += 1


def sort_final(d: dict) -> list[dict]:
    """Ordena el dict de clasificación: pts desc, coef desc, dg desc, gf desc, nombre asc."""
    teams = sorted(
        d.values(),
        key=lambda t: (-t["pts"], -coef(t["pts"], t["pj"]), -t["dg"], -t["gf"], t["nombre"])
    )
    for i, t in enumerate(teams):
        t["pos"] = i + 1
    return teams


def team_data(standings: list[dict], nombre: str) -> Optional[dict]:
    """Busca un equipo por nombre parcial en la clasificación final."""
    n = nombre.lower()
    for t in standings:
        if n in t["nombre"].lower() or t["nombre"].lower() in n:
            return t
    return None


def flatten_fixtures(fixtures_by_round: dict[int, list[dict]]) -> list[tuple]:
    """Convierte {jornada: [partidos]} a lista de (local, visitante, jornada)."""
    return [
        (m["local"], m["visitante"], rnd)
        for rnd, matches in sorted(fixtures_by_round.items())
        for m in matches
    ]


def run_scenarios(
    teams: list[dict],
    flat_matches: list[tuple],
) -> list[tuple]:
    """
    Enumera todos los 3^n escenarios.
    Retorna lista de (combo_tuple, final_standings_list).
    """
    n = len(flat_matches)
    results = []
    for combo in product(("G", "E", "D"), repeat=n):
        d = {t["nombre"]: dict(t) for t in teams}
        for i, (local, vis, _) in enumerate(flat_matches):
            apply_result(d, local, vis, combo[i])
        results.append((combo, sort_final(d)))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS DE SUPERVIVENCIA
# ══════════════════════════════════════════════════════════════════════════════

def find_target_match(flat_matches: list[tuple], target_nombre: str) -> Optional[tuple]:
    """Localiza el partido del equipo objetivo."""
    n = target_nombre.lower()
    for m in flat_matches:
        local, vis, _ = m
        if n in local.lower() or n in vis.lower():
            return m
    return None


def find_minimum_conditions(
    scenarios: list[dict],
    matches: list[tuple],
) -> dict[tuple, set]:
    """
    Retorna las condiciones mínimas necesarias en todos los escenarios de supervivencia.
    Solo incluye partidos donde no aparecen los 3 resultados posibles.
    """
    result: dict[tuple, set] = {}
    for local, vis, _ in matches:
        key = (local, vis)
        seen: set[str] = set()
        for s in scenarios:
            if key in s.get("match_outcomes", {}):
                seen.add(s["match_outcomes"][key])
        if 0 < len(seen) < 3:
            result[key] = seen
    return result


def analyze_survival(
    target_nombre: str,
    target_teams: list[dict],
    target_fixtures: dict[int, list[dict]],
    other_groups: dict[str, dict],  # {gid: {nombre, teams, fixtures}}
) -> dict:
    """
    Análisis completo de supervivencia/ascenso para el equipo objetivo.

    Retorna:
    {
      n_teams, safe_pos, ascenso_pos,
      target_match,             # partido del equipo objetivo
      by_result: {              # análisis por resultado del equipo
        "G": { safe_by_pos, safe_by_coef, relegated, ascenso },
        "E": { ... },
        "D": { ... },
      },
      other_groups_coef: {      # análisis coeficiente otros grupos
        gid: { nombre, scenarios_11th, min_coef, max_coef }
      }
    }
    """
    n_teams  = len(target_teams)
    safe_pos = n_teams - DESCENSO_DIRECTO_N   # 11 para grupo de 14

    flat = flatten_fixtures(target_fixtures)
    target_match = find_target_match(flat, target_nombre)

    # ── Enumerar escenarios de otros grupos (para coeficiente) ──────────────
    other_coef: dict[str, dict] = {}
    for gid, gdata in other_groups.items():
        g_flat = flatten_fixtures(gdata["fixtures"])
        g_scen = run_scenarios(gdata["teams"], g_flat)
        items = []
        for combo, final in g_scen:
            if len(final) >= safe_pos:
                t11 = final[safe_pos - 1]
                items.append({
                    "combo":  combo,
                    "coef":   coef(t11["pts"], t11["pj"]),
                    "pts":    t11["pts"],
                    "pj":     t11["pj"],
                    "nombre": t11["nombre"],
                    "flat_matches": g_flat,
                })
        if items:
            other_coef[gid] = {
                "nombre":    gdata["nombre"],
                "items":     items,
                "min_coef":  min(x["coef"] for x in items),
                "max_coef":  max(x["coef"] for x in items),
                "flat_matches": g_flat,
                "teams":     gdata["teams"],
            }

    # ── Analizar por resultado del equipo objetivo ──────────────────────────
    results_map = {"G": "gana", "E": "empata", "D": "pierde"}
    by_result: dict[str, dict] = {}

    for target_result in ("G", "E", "D"):
        # Construir dict inicial con el resultado del equipo ya aplicado
        def make_base(tr=target_result):
            d = {t["nombre"]: dict(t) for t in target_teams}
            if target_match:
                local, vis, _ = target_match
                # Convertir resultado al punto de vista del local
                if target_nombre.lower() in local.lower():
                    r = tr
                else:
                    r = {"G": "D", "D": "G", "E": "E"}[tr]
                apply_result(d, local, vis, r)
            return d

        # Partidos restantes SIN el del equipo objetivo
        other_matches = [m for m in flat if m != target_match]
        n_other = len(other_matches)

        safe_pos_scenarios: list[dict] = []
        coef_zone_scenarios: list[dict] = []
        relegation_scenarios: list[dict] = []
        ascenso_scenarios: list[dict] = []

        for combo in product(("G", "E", "D"), repeat=n_other):
            d = make_base()
            match_outcomes: dict[tuple, str] = {}
            for i, (local, vis, rnd) in enumerate(other_matches):
                apply_result(d, local, vis, combo[i])
                match_outcomes[(local, vis)] = combo[i]

            final = sort_final(d)
            target = team_data(final, target_nombre)
            if not target:
                continue
            pos = target["pos"]

            if pos <= ASCENSO_N:
                ascenso_scenarios.append({
                    "match_outcomes": match_outcomes,
                    "final": final,
                    "target": target,
                    "position": pos,
                })

            if pos < safe_pos:
                safe_pos_scenarios.append({
                    "match_outcomes": match_outcomes,
                    "final": final,
                    "target": target,
                    "position": pos,
                })
            elif pos == safe_pos:
                my_c = coef(target["pts"], target["pj"])
                # ¿Cuántos grupos pueden tener su undécimo con peor coef?
                worse_info: dict[str, dict] = {}
                for gid, gc_data in other_coef.items():
                    worse_items = [x for x in gc_data["items"] if x["coef"] < my_c]
                    worse_info[gid] = {
                        "nombre":       gc_data["nombre"],
                        "possible":     len(worse_items) > 0,
                        "always_worse": gc_data["max_coef"] < my_c,
                        "min_coef":     gc_data["min_coef"],
                        "max_coef":     gc_data["max_coef"],
                        "best_for_target": min(worse_items, key=lambda x: x["coef"])
                                           if worse_items else None,
                    }
                n_possible_worse = sum(1 for v in worse_info.values() if v["possible"])

                if n_possible_worse >= DESCENSO_COEF_N:
                    coef_zone_scenarios.append({
                        "match_outcomes": match_outcomes,
                        "final": final,
                        "target": target,
                        "my_coef": my_c,
                        "worse_info": worse_info,
                        "n_possible_worse": n_possible_worse,
                    })
                else:
                    relegation_scenarios.append({
                        "match_outcomes": match_outcomes,
                        "final": final,
                        "target": target,
                        "position": pos,
                        "reason": "coef_imposible",
                    })
            else:
                relegation_scenarios.append({
                    "match_outcomes": match_outcomes,
                    "final": final,
                    "target": target,
                    "position": pos,
                    "reason": "descenso_directo",
                })

        by_result[target_result] = {
            "label":      results_map[target_result],
            "safe_pos":   safe_pos_scenarios,
            "coef_zone":  coef_zone_scenarios,
            "relegated":  relegation_scenarios,
            "ascenso":    ascenso_scenarios,
            "can_survive": len(safe_pos_scenarios) > 0 or len(coef_zone_scenarios) > 0,
        }

    return {
        "n_teams":      n_teams,
        "safe_pos":     safe_pos,
        "target_match": target_match,
        "flat_matches": flat,
        "by_result":    by_result,
        "other_coef":   other_coef,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SALIDA
# ══════════════════════════════════════════════════════════════════════════════

def print_standings_table(
    teams: list[dict],
    titulo: str,
    highlight: str = "",
    n_teams: Optional[int] = None,
) -> None:
    n        = n_teams or len(teams)
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
    print(f"  ↓ Descenso directo  ? Zona coeficiente")


def print_relegation_analysis(analysis: dict, n_grupos: int) -> None:
    direct    = analysis["direct"]
    eleventh  = analysis["eleventh_sorted"]
    print(f"\n{'═'*75}")
    print(f"  ANÁLISIS DE DESCENSO — {n_grupos} grupo(s)")
    print(f"{'═'*75}")
    if direct:
        print(f"\n  📉 DESCENSO DIRECTO (últimas {DESCENSO_DIRECTO_N} posiciones):")
        for t in direct:
            print(
                f"     [{t['grupo'][-6:]}] {t['nombre']:<32} "
                f"Pts:{t['pts']:>3}  PJ:{t['pj']:>2}  Coef:{coef(t['pts'], t['pj']):.3f}"
            )
    if eleventh:
        print(f"\n  ⚠  ZONA COEFICIENTE — undécimos (descienden los {DESCENSO_COEF_N} peores):")
        for i, t in enumerate(eleventh):
            estado = "↓ DESCIENDE" if i < DESCENSO_COEF_N else "✅ PERMANECE"
            print(
                f"     {estado}  [{t['grupo'][-6:]}] {t['nombre']:<28} "
                f"Coef:{t['coef']:.3f}  (Pts:{t['pts']:>3}  PJ:{t['pj']:>2})"
            )
    elif n_grupos == 1:
        print(f"\n  ℹ  Para análisis de coeficiente usa --todos-grupos")


def _fmt_match_outcome(local: str, vis: str, result: str) -> str:
    short_local = local.split()[0] if len(local) > 20 else local
    short_vis   = vis.split()[0]   if len(vis) > 20   else vis
    lbl = {"G": f"gana {short_local}", "E": f"empatan", "D": f"gana {short_vis}"}
    return lbl[result]


def _fmt_condition(local: str, vis: str, allowed: set) -> str:
    """Formatea una condición mínima como texto legible."""
    sl = local.split()[0] if len(local) > 20 else local
    sv = vis.split()[0] if len(vis) > 20 else vis
    if allowed == {"G"}:
        return f"gana {sl}"
    if allowed == {"D"}:
        return f"gana {sv}"
    if allowed == {"E"}:
        return f"empate {sl}–{sv}"
    if allowed == {"G", "E"}:
        return f"{sv} no gana (gana o empata {sl})"
    if allowed == {"E", "D"}:
        return f"{sl} no gana (gana o empata {sv})"
    if allowed == {"G", "D"}:
        return f"no empate ({sl} o {sv} ganan)"
    return "cualquier resultado"


def print_survival_report(
    target_nombre: str,
    analysis: dict,
    groups_meta: dict,
    all_standings: dict,
) -> None:
    """Imprime el análisis de permanencia/ascenso."""
    safe_pos   = analysis["safe_pos"]
    by_result  = analysis["by_result"]
    other_coef = analysis["other_coef"]
    flat       = analysis["flat_matches"]
    n_teams    = analysis["n_teams"]

    target_match = analysis["target_match"]

    print(f"\n{'═'*75}")
    print(f"  ANÁLISIS DE PERMANENCIA — {target_nombre.upper()}")
    print(f"{'═'*75}")

    if not flat:
        print("  ℹ  No quedan jornadas por disputar.")
        return

    # Partidos restantes del grupo
    print(f"\n  Partidos pendientes:")
    for local, vis, rnd in flat:
        is_target = (
            target_nombre.lower() in local.lower() or
            target_nombre.lower() in vis.lower()
        )
        mark = " ◄ (PARTIDO DEL EQUIPO)" if is_target else ""
        print(f"    J{rnd}: {local} vs {vis}{mark}")

    # ── Tabla de coeficientes undécimos (todos los grupos) ──────────────────
    if other_coef:
        print(f"\n  {'─'*73}")
        print(f"  UNDÉCIMOS ACTUALES (zona coeficiente — {DESCENSO_COEF_N} peores descienden):")
        print(f"  {'─'*73}")
        print(f"  {'Grupo':<10} {'Equipo':<36} {'Pts':>3} {'PJ':>3} {'Coef':>6}  {'Rango posible':>20}")
        print(f"  {'─'*73}")
        for gid, gc_data in sorted(other_coef.items()):
            t_curr = all_standings.get(gid, [])
            t11 = t_curr[safe_pos - 1] if len(t_curr) >= safe_pos else None
            if not t11:
                continue
            c_curr = coef(t11["pts"], t11["pj"])
            c_min  = gc_data["min_coef"]
            c_max  = gc_data["max_coef"]
            print(
                f"  {gc_data['nombre']:<10} {t11['nombre']:<36} "
                f"{t11['pts']:>3} {t11['pj']:>3} {c_curr:>6.3f}  "
                f"[{c_min:.3f} – {c_max:.3f}]"
            )
        # Equipo objetivo (su grupo no está en other_coef)
        print(f"  {'─'*73}")

    # ── Análisis por resultado del equipo ───────────────────────────────────
    results_label = {"G": "GANA", "E": "EMPATA", "D": "PIERDE"}
    results_emoji = {"G": "⚽", "E": "🤝", "D": "❌"}

    can_survive_at_all = any(r["can_survive"] for r in by_result.values())

    for tr in ("G", "E", "D"):
        rdata = by_result[tr]
        n_safe_pos  = len(rdata["safe_pos"])
        n_coef      = len(rdata["coef_zone"])
        n_rel       = len(rdata["relegated"])
        n_asc       = len(rdata["ascenso"])
        total       = n_safe_pos + n_coef + n_rel

        print(f"\n  {'─'*73}")
        print(f"  {results_emoji[tr]} SI {target_nombre.split()[0].upper()} "
              f"{results_label[tr]}:")

        if not rdata["can_survive"]:
            print(f"     → DESCENSO DIRECTO en todos los escenarios ({total} combinaciones) ❌")
            continue

        if n_asc:
            print(f"     ✅ Ascenso posible en {n_asc}/{total} escenarios")

        if n_safe_pos:
            print(f"     ✅ Permanencia por posición (≤{safe_pos-1}º) en "
                  f"{n_safe_pos}/{total} escenarios")
            # Mostrar el mejor escenario (el que requiere menos condiciones favorables)
            best = min(rdata["safe_pos"], key=lambda s: s["position"])
            pos_final = best["position"]
            print(f"        Mejor caso → posición {pos_final}ª")
            # Mostrar qué resultados del grupo se necesitan
            conds = []
            for (local, vis), result in best["match_outcomes"].items():
                conds.append(f"{_fmt_match_outcome(local, vis, result)}")
            if conds:
                print(f"        Condiciones en el grupo:")
                for c_txt in conds:
                    print(f"          • {c_txt}")

        if n_coef:
            print(f"     ⚠  Permanencia posible por coeficiente en "
                  f"{n_coef}/{total} escenarios")
            best_coef = max(rdata["coef_zone"], key=lambda s: s["n_possible_worse"])
            my_c = best_coef["my_coef"]
            wi   = best_coef["worse_info"]

            print(f"        Coeficiente de {target_nombre.split()[0]}: {my_c:.3f}")
            other_matches_in_group = [m for m in flat if m != target_match]
            min_conds = find_minimum_conditions(rdata["coef_zone"], other_matches_in_group)
            if min_conds:
                print(f"        Condición(es) necesaria(s) en el grupo:")
                for (local, vis), allowed in min_conds.items():
                    print(f"          • {_fmt_condition(local, vis, allowed)}")
            else:
                print(f"        Sin condiciones adicionales en el grupo.")

            always_worse = [v for v in wi.values() if v["always_worse"]]
            possibly_worse = [v for v in wi.values() if v["possible"] and not v["always_worse"]]
            always_better  = [v for v in wi.values() if not v["possible"]]

            if always_worse:
                names = ", ".join(f"{v['nombre']}({v['min_coef']:.3f})"
                                  for v in always_worse)
                print(f"        ✅ Grupos con undécimo SIEMPRE peor (sin condiciones): {names}")

            if possibly_worse:
                print(f"        🎯 Grupos que necesitan tener su undécimo peor que {my_c:.3f}:")
                for v in possibly_worse:
                    s = v["best_for_target"]
                    if s:
                        # Describir los resultados necesarios en ese grupo
                        cond_parts = []
                        for i, (local, vis, _) in enumerate(s["flat_matches"]):
                            cond_parts.append(_fmt_match_outcome(local, vis, s["combo"][i]))
                        cond_str = " | ".join(cond_parts)
                        print(
                            f"          • {v['nombre']}: undécimo debe quedar "
                            f"≤ {s['coef']:.3f} pts ({s['pts']} pts / {s['pj']} PJ)"
                        )
                        if cond_str:
                            print(f"            → {cond_str}")

            if always_better:
                names = ", ".join(f"{v['nombre']}({v['min_coef']:.3f})"
                                  for v in always_better)
                print(f"        ❌ Grupos con undécimo SIEMPRE mejor (desfavorables): {names}")

            n_against = len(always_better)
            n_needed  = DESCENSO_COEF_N - len(always_worse) - len(possibly_worse)
            if n_against > (7 - DESCENSO_COEF_N):
                print(f"        ⛔ Demasiados grupos con coef superior → coef muy difícil")
            else:
                print(f"        → Con las condiciones anteriores: ✅ PERMANENCIA POR COEF")

    # ── Resumen narrativo ───────────────────────────────────────────────────
    print(f"\n{'═'*75}")
    print(f"  💬 RESUMEN")
    print(f"{'═'*75}")

    if not can_survive_at_all:
        print(f"\n  ❌ NO existe ningún escenario de permanencia.")
        print(f"     El descenso de {target_nombre} es matemáticamente seguro.")
        return

    # Encontrar el resultado mínimo para sobrevivir
    min_result_needed = None
    for tr in ("D", "E", "G"):  # orden: del peor al mejor resultado
        if by_result[tr]["can_survive"]:
            min_result_needed = tr

    result_texts = {"G": "GANA", "E": "EMPATA", "D": "PIERDE"}
    print(f"\n  🟡 EXISTE UN CAMINO A LA PERMANENCIA.")

    if target_match:
        local, vis, rnd = target_match
        is_local = target_nombre.lower() in local.lower()
        rival = vis if is_local else local
        cond_loc = "LOCAL" if is_local else "VISITANTE"
        print(f"\n  Partido clave: J{rnd} — {local} vs {vis}")

    rdata = by_result[min_result_needed]

    # ¿Puede sobrevivir por posición?
    if rdata["safe_pos"]:
        best = min(rdata["safe_pos"], key=lambda s: s["position"])
        print(f"\n  ✅ MEJOR CAMINO — Permanencia POR POSICIÓN:")
        print(f"     1. {target_nombre.split()[0]} debe {result_texts[min_result_needed].lower()} "
              f"su partido")
        if best["match_outcomes"]:
            print(f"     2. En el resto del grupo:")
            for (local, vis), result in best["match_outcomes"].items():
                print(f"        • {_fmt_match_outcome(local, vis, result)}")
        print(f"     → {target_nombre.split()[0]} terminaría en posición "
              f"{best['position']}ª ✅")

    # ¿Puede sobrevivir por coeficiente?
    if rdata["coef_zone"]:
        best_coef = max(rdata["coef_zone"], key=lambda s: s["n_possible_worse"])
        my_c = best_coef["my_coef"]
        wi   = best_coef["worse_info"]
        always_w = [v for v in wi.values() if v["always_worse"]]
        possibly_w = [v for v in wi.values() if v["possible"] and not v["always_worse"]]
        always_b   = [v for v in wi.values() if not v["possible"]]

        print(f"\n  ⚠️  CAMINO ALTERNATIVO — Permanencia POR COEFICIENTE:")
        print(f"     1. {target_nombre.split()[0]} debe {result_texts[min_result_needed].lower()} "
              f"→ coef: {my_c:.3f}")
        if best_coef["match_outcomes"]:
            print(f"     2. En el resto del grupo:")
            for (local, vis), result in best_coef["match_outcomes"].items():
                print(f"        • {_fmt_match_outcome(local, vis, result)}")

        if always_w:
            print(f"     3. Grupos ya asegurados (undécimo siempre peor):")
            for v in always_w:
                print(f"        • {v['nombre']} ({v['min_coef']:.3f}–{v['max_coef']:.3f}) ✅")

        if possibly_w:
            print(f"     4. Condiciones necesarias en otros grupos:")
            for v in possibly_w:
                s = v["best_for_target"]
                if s:
                    cond_parts = []
                    for i, (l2, v2, _) in enumerate(s["flat_matches"]):
                        cond_parts.append(_fmt_match_outcome(l2, v2, s["combo"][i]))
                    print(f"        • {v['nombre']}: {' | '.join(cond_parts)}")
                    print(f"          → undécimo quedaría con coef {s['coef']:.3f} < {my_c:.3f}")

        if always_b:
            print(f"     ❌ Grupos siempre mejores (inamovibles):")
            for v in always_b:
                print(f"        • {v['nombre']} (min coef {v['min_coef']:.3f})")

        n_needed = DESCENSO_COEF_N - len(always_w) - len(possibly_w)
        if n_needed > 0:
            print(f"     ⚠️  Faltan {n_needed} grupo(s) adicionales para completar los "
                  f"{DESCENSO_COEF_N} peores → coef muy difícil")
        else:
            print(f"     → Con las condiciones anteriores: ✅ PERMANENCIA POR COEF POSIBLE")


# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS DE DESCENSO (clasificación actual)
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
            eleventh.append({**t11, "grupo": gid,
                              "coef": coef(t11["pts"], t11["pj"])})
    eleventh.sort(key=lambda x: x["coef"])
    return {
        "direct":            direct,
        "eleventh_sorted":   eleventh,
        "relegated_by_coef": eleventh[:DESCENSO_COEF_N],
        "safe_by_coef":      eleventh[DESCENSO_COEF_N:],
    }


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
            "Clasificaciones RFFM — Preferente Alevín F11\n\n"
            "Ejemplos:\n"
            "  python rffm_clasificacion.py -e Ivero\n"
            "  python rffm_clasificacion.py -e Ivero --cache\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--equipo", "-e", default="",
        help="Nombre (parcial) del equipo a analizar.")
    p.add_argument("--grupo", "-g", default=GRUPO_7_ID,
        help=f"ID del grupo principal (default: {GRUPO_7_ID} = Grupo 7).")
    p.add_argument("--competicion", "-c", default=COMPETICION_DEFAULT,
        help=f"ID de la competición (default: {COMPETICION_DEFAULT}).")
    p.add_argument("--total-jornadas", "-j", type=int, default=0,
        help="Total de jornadas (0 = usar el valor de la API).")
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
    print("  RFFM · Preferente Alevín Fútbol 11 · Análisis de Clasificación")
    print("═" * 75)

    session      = make_session()
    all_standings: dict[str, list[dict]] = {}
    groups_meta:   dict[str, dict]       = {}
    all_fixtures:  dict[str, dict]       = {}   # gid → {jornada: [partidos]}

    # ── Caché ────────────────────────────────────────────────────────────────
    if args.cache:
        cached = load_cache(cache_file)
        if cached:
            all_standings = cached.get("standings", {})
            groups_meta   = cached.get("groups_meta", {})
            all_fixtures  = cached.get("fixtures", {})
            j_cache = groups_meta.get(args.grupo, {}).get("jornada_actual", "?")
            print(f"\n📂 Datos cargados desde caché (Jornada {j_cache})")

    # ── Descarga online ───────────────────────────────────────────────────────
    if not all_standings:
        print(f"\n🔍 Obteniendo grupos...")
        groups = discover_groups(session, args.competicion)
        if not groups:
            print("❌ No se pudieron obtener los grupos.")
            sys.exit(1)
        if not any(g["id"] == args.grupo for g in groups):
            groups.append({"id": args.grupo, "nombre": "Grupo 7",
                           "total_jornadas": 26, "total_equipos": 14})

        print(f"  Grupos: {len(groups)}")
        print(f"\n📊 Descargando clasificaciones y partidos pendientes...")

        for g in groups:
            gid = g["id"]
            print(f"  · {g['nombre']} ({gid}) ...", end=" ", flush=True)

            jornada_actual = discover_current_round(session, gid)
            data  = fetch_json(session, "/api/standings", {
                "idGroup": gid, "round": jornada_actual,
            })
            teams = parse_standings(data)

            total_j = (args.total_jornadas if args.total_jornadas and gid == args.grupo
                       else g["total_jornadas"])

            fixtures = get_remaining_fixtures(session, gid, jornada_actual, total_j)

            if teams:
                all_standings[gid] = teams
                groups_meta[gid]   = {
                    "nombre":         g["nombre"],
                    "total_jornadas": total_j,
                    "total_equipos":  g["total_equipos"],
                    "jornada_actual": jornada_actual,
                }
                all_fixtures[gid] = {str(k): v for k, v in fixtures.items()}
                jornadas_pending = sorted(fixtures.keys())
                if not jornadas_pending:
                    j_info = "competición finalizada"
                elif len(jornadas_pending) == 1:
                    j_info = f"1 jornada restante: J{jornadas_pending[0]}"
                else:
                    j_info = (f"{len(jornadas_pending)} jornadas restantes: "
                              f"J{jornadas_pending[0]}–J{jornadas_pending[-1]}")
                print(f"✓ {len(teams)} eq (J{jornada_actual}, {j_info})")
            else:
                print("✗ sin datos")

        save_cache(cache_file, {
            "standings": all_standings,
            "groups_meta": groups_meta,
            "fixtures": all_fixtures,
        })
        print(f"\n💾 Datos guardados en {cache_file}")

    if not all_standings:
        print("\n❌ No se pudieron obtener datos.")
        sys.exit(1)

    # ── Localizar equipo ─────────────────────────────────────────────────────
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

    # ── Clasificación del grupo objetivo ─────────────────────────────────────
    meta          = groups_meta.get(grupo_id_equipo, {})
    total_jornadas = meta.get("total_jornadas", 26)
    jornada_actual = meta.get("jornada_actual", 1)
    remaining      = max(0, total_jornadas - jornada_actual)
    grupo_teams    = all_standings.get(grupo_id_equipo, [])
    grupo_nombre   = meta.get("nombre", f"Grupo {grupo_id_equipo}")

    if grupo_teams:
        print_standings_table(
            grupo_teams,
            f"CLASIFICACIÓN {grupo_nombre.upper()} — PREFERENTE ALEVÍN "
            f"— Jornada {jornada_actual} ({remaining} jornada(s) restante(s))",
            highlight=equipo_nombre,
        )

    # ── Análisis de descenso actual ───────────────────────────────────────────
    rel_analysis = build_relegation_analysis(all_standings)
    print_relegation_analysis(rel_analysis, len(all_standings))

    # ── Análisis de permanencia/ascenso ──────────────────────────────────────
    if equipo_nombre and remaining > 0:
        target_fixtures_raw = all_fixtures.get(grupo_id_equipo, {})
        target_fixtures = {int(k): v for k, v in target_fixtures_raw.items()}

        other_groups: dict[str, dict] = {}
        for gid, teams in all_standings.items():
            if gid == grupo_id_equipo:
                continue
            gfix_raw = all_fixtures.get(gid, {})
            gfix     = {int(k): v for k, v in gfix_raw.items()}
            other_groups[gid] = {
                "nombre":   groups_meta.get(gid, {}).get("nombre", gid),
                "teams":    teams,
                "fixtures": gfix,
            }

        print(f"\n  ⚙  Calculando simulación ({3**sum(len(v) for v in target_fixtures.values())} "
              f"escenarios en grupo + {len(other_groups)} grupos adicionales)...")

        survival = analyze_survival(
            equipo_nombre,
            grupo_teams,
            target_fixtures,
            other_groups,
        )

        print_survival_report(
            equipo_nombre, survival, groups_meta, all_standings
        )

    elif equipo_nombre and remaining == 0:
        print(f"\n  ℹ La competición ha finalizado.")
    elif not equipo_nombre:
        print(f"\n  ℹ Usa --equipo \"Nombre\" para analizar escenarios.")


if __name__ == "__main__":
    main()
