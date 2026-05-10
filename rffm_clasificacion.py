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
"""

import sys
import json
import time
import argparse
from itertools import product
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN POR DEFECTO
# ══════════════════════════════════════════════════════════════════════════════

BASE_URL = "https://www.rffm.es"

COMPETICION_DEFAULT = "24037708"   # Preferente Alevín Fútbol 11
TEMPORADA_DEFAULT   = "21"
TIPO_JUEGO          = "1"          # Fútbol 11
GRUPO_7_ID          = "24037715"   # Grupo 7

# Reglas de descenso
DESCENSO_DIRECTO_N    = 3  # últimas N posiciones de grupos de 14 → descenso directo
DESCENSO_COEF_N       = 4  # de entre todos los 11ºs, los N con peor coef. descienden
GRUPO_SIZE_STANDARD   = 14

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://www.rffm.es/",
}

RESULT_PTS = {"G": 3, "E": 1, "D": 0}


# ══════════════════════════════════════════════════════════════════════════════
# HTTP / SESIÓN
# ══════════════════════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_soup(
    session: requests.Session,
    path: str,
    params: dict,
    retries: int = 3,
) -> Optional[BeautifulSoup]:
    url = BASE_URL + "/" + path.lstrip("/")
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=20)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except requests.RequestException as exc:
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
            else:
                print(f"  ⚠ {url}: {exc}", file=sys.stderr)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# DESCUBRIMIENTO: GRUPOS Y JORNADA
# ══════════════════════════════════════════════════════════════════════════════

def discover_groups(
    session: requests.Session,
    temporada: str,
    competicion: str,
) -> list[dict]:
    """
    Descarga la página de clasificaciones sin grupo específico y extrae
    todos los IDs de grupo disponibles (de atributos href/value).
    """
    soup = fetch_soup(session, "/competicion/clasificaciones", {
        "temporada": temporada,
        "competicion": competicion,
        "tipojuego": TIPO_JUEGO,
    })
    if not soup:
        return []

    seen: set[str] = set()
    groups: list[dict] = []

    # Busca en <select> con name="grupo" y en <a href="...grupo=X...">
    for tag in soup.find_all(["option", "a"]):
        href  = tag.get("href", "") or ""
        value = tag.get("value", "") or ""
        label = tag.get_text(strip=True)

        for src in [href, value]:
            if "grupo=" not in src:
                continue
            gid = src.split("grupo=")[1].split("&")[0].split("#")[0].strip()
            if gid and gid not in seen:
                seen.add(gid)
                groups.append({"id": gid, "nombre": label or gid})

    return groups


def discover_latest_jornada(
    session: requests.Session,
    temporada: str,
    competicion: str,
    grupo: str,
) -> int:
    """
    Intenta leer la última jornada disponible desde el selector de jornadas.
    Si no lo encuentra, hace una búsqueda lineal hacia arriba.
    """
    soup = fetch_soup(session, "/competicion/clasificaciones", {
        "temporada": temporada,
        "competicion": competicion,
        "grupo": grupo,
        "tipojuego": TIPO_JUEGO,
    })

    if soup:
        # Selector <select name="jornada"> o <select id="jornada">
        for sel in soup.find_all("select"):
            name = (sel.get("name", "") or sel.get("id", "") or "").lower()
            if "jornada" not in name:
                continue
            nums = [
                int(o["value"])
                for o in sel.find_all("option")
                if o.get("value", "").lstrip("-").isdigit()
            ]
            if nums:
                return max(nums)

        # Links con jornada=N
        jornadas = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "jornada=" in href:
                try:
                    jornadas.append(int(href.split("jornada=")[1].split("&")[0]))
                except ValueError:
                    pass
        if jornadas:
            return max(jornadas)

    # Fallback: escaneo lineal
    last_valid = 1
    for j in range(1, 35):
        soup2 = fetch_soup(session, "/competicion/clasificaciones", {
            "temporada": temporada,
            "competicion": competicion,
            "grupo": grupo,
            "jornada": str(j),
            "tipojuego": TIPO_JUEGO,
        })
        teams = parse_standings(soup2) if soup2 else []
        if not teams:
            break
        # Comprobamos que algún equipo ha jugado esta jornada
        if any(t["pj"] >= j for t in teams):
            last_valid = j
        else:
            break
        time.sleep(0.4)

    return last_valid


# ══════════════════════════════════════════════════════════════════════════════
# PARSEO DE CLASIFICACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def _int(text: str) -> int:
    """Convierte texto a int ignorando signos '+' y espacios."""
    return int(text.replace("+", "").replace("\xa0", "").strip())


def parse_standings(soup: Optional[BeautifulSoup]) -> list[dict]:
    """
    Extrae la tabla de clasificación del HTML.
    Columnas esperadas: Pos, Equipo, PJ, PG, PE, PP, GF, GC, DG, Pts
    (acepta columna extra de escudo entre Pos y Equipo).
    """
    if not soup:
        return []

    # Intenta encontrar la tabla por clase, si no coge la primera tabla
    table = None
    for candidate in soup.find_all("table"):
        classes = " ".join(candidate.get("class", []))
        if "clasificacion" in classes.lower() or "tabla" in classes.lower():
            table = candidate
            break
    if not table:
        tables = soup.find_all("table")
        table = tables[0] if tables else None
    if not table:
        return []

    teams: list[dict] = []
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        texts = [c.get_text(strip=True) for c in cells]

        if len(texts) < 9:
            continue
        # La primera celda debe ser la posición numérica
        if not texts[0].isdigit():
            continue

        pos = int(texts[0])

        # Determina el offset (existe columna de escudo/imagen entre pos y nombre)
        parsed = None
        for offset in (0, 1):
            try:
                nombre = texts[1]
                pj = _int(texts[2 + offset])
                pg = _int(texts[3 + offset])
                pe = _int(texts[4 + offset])
                pp = _int(texts[5 + offset])
                gf = _int(texts[6 + offset])
                gc = _int(texts[7 + offset])
                dg = _int(texts[8 + offset])
                pts = _int(texts[-1])
                # Sanity check: pj == pg + pe + pp
                if pg + pe + pp != pj:
                    continue
                parsed = {
                    "pos": pos, "nombre": nombre,
                    "pj": pj, "pg": pg, "pe": pe, "pp": pp,
                    "gf": gf, "gc": gc, "dg": dg, "pts": pts,
                }
                break
            except (ValueError, IndexError):
                continue

        if parsed:
            teams.append(parsed)

    teams.sort(key=lambda t: t["pos"])
    return teams


# ══════════════════════════════════════════════════════════════════════════════
# CALENDARIO (OPCIONAL — para mostrar rivales en jornadas restantes)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_remaining_fixtures(
    session: requests.Session,
    temporada: str,
    competicion: str,
    grupo: str,
    desde_jornada: int,
) -> dict[int, list[dict]]:
    """
    Intenta obtener los partidos pendientes desde el calendario online.
    Devuelve {jornada: [{local, visitante}, ...]}
    """
    soup = fetch_soup(session, "/competicion/calendario", {
        "temporada": temporada,
        "competicion": competicion,
        "grupo": grupo,
        "tipojuego": TIPO_JUEGO,
    })
    if not soup:
        return {}

    fixtures: dict[int, list[dict]] = {}
    current_jornada = None

    for row in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if not cells:
            continue

        # Busca encabezados de jornada (Jornada N / Jornada  N)
        joined = " ".join(cells).lower()
        if "jornada" in joined:
            for cell in cells:
                lower = cell.lower()
                if "jornada" in lower:
                    num_str = lower.replace("jornada", "").strip()
                    if num_str.isdigit():
                        current_jornada = int(num_str)
            continue

        if current_jornada is None or current_jornada <= desde_jornada:
            continue

        # Fila de partido: buscamos "local - visitante" o columnas separadas
        # Formato típico RFFM: [equipo_local, resultado_o_vs, equipo_visitante, ...]
        if len(cells) >= 3:
            local = cells[0]
            visitante = cells[-1] if len(cells) > 2 else cells[2]
            if local and visitante and local != visitante:
                fixtures.setdefault(current_jornada, []).append(
                    {"local": local, "visitante": visitante}
                )

    return fixtures


# ══════════════════════════════════════════════════════════════════════════════
# COEFICIENTE
# ══════════════════════════════════════════════════════════════════════════════

def coef(pts: int, pj: int) -> float:
    return pts / pj if pj > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS DE DESCENSO
# ══════════════════════════════════════════════════════════════════════════════

def build_relegation_analysis(all_standings: dict[str, list[dict]]) -> dict:
    """
    Calcula:
      - Equipos en descenso directo (3 últimos de grupos de 14)
      - Lista de undécimos de cada grupo, ordenados por coeficiente
    """
    direct: list[dict] = []
    eleventh: list[dict] = []

    for gid, teams in all_standings.items():
        n = len(teams)
        safe_threshold = n - DESCENSO_DIRECTO_N  # pos 11 para grupos de 14

        # Descenso directo: últimas DESCENSO_DIRECTO_N posiciones
        for t in teams[safe_threshold:]:
            direct.append({**t, "grupo": gid})

        # 11º (índice 10)
        if n >= 11:
            t11 = teams[10]
            eleventh.append({
                **t11,
                "grupo": gid,
                "coef": coef(t11["pts"], t11["pj"]),
            })

    eleventh.sort(key=lambda x: x["coef"])
    return {
        "direct": direct,
        "eleventh_sorted": eleventh,    # peor coef primero
        "relegated_by_coef": eleventh[:DESCENSO_COEF_N],
        "safe_by_coef": eleventh[DESCENSO_COEF_N:],
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
    """
    Calcula la posición mínima y máxima posible para el equipo objetivo.

    best_pos: asumiendo que todos los demás obtienen 0 pts en jornadas restantes.
    worst_pos: asumiendo que todos los demás obtienen 3 pts por jornada restante.
    """
    others = [t for t in teams if t["nombre"] != target_nombre]

    best_pos = sum(1 for t in others if t["pts"] > new_pts) + 1
    worst_pos = sum(1 for t in others if t["pts"] + remaining * 3 >= new_pts) + 1

    return best_pos, worst_pos


def _scenario_status(
    best_pos: int,
    worst_pos: int,
    n_teams: int,
) -> tuple[str, str]:
    """
    Devuelve (estado_garantizado, estado_posible).
    Para grupos de n_teams:
      - posiciones 1..(n-3)    → zona segura
      - posición  (n-2) = 11   → zona coeficiente (puede descender o no)
      - posiciones (n-1)..n    → descenso directo
    """
    safe_max = n_teams - DESCENSO_DIRECTO_N      # 11 para grupos de 14
    coef_pos  = safe_max                          # posición 11

    # ¿Qué puede pasar en el mejor caso?
    if best_pos <= safe_max - 1:                  # ≤ 10
        best_label = "✅ SEGURO"
    elif best_pos == coef_pos:                    # == 11
        best_label = "⚠ ZONA COEF"
    else:
        best_label = "❌ DESCENSO"

    # ¿Qué puede pasar en el peor caso?
    if worst_pos <= safe_max - 1:                 # ≤ 10 incluso en el peor caso
        worst_label = "✅ GARANTIZADO"
    elif worst_pos == coef_pos:                   # peor caso = posición 11
        worst_label = "⚠ ZONA COEF"
    else:
        worst_label = "❌ DESCENSO POSIBLE"

    return worst_label, best_label


def simulate(
    teams: list[dict],
    target_nombre: str,
    remaining: int,
    all_standings: dict[str, list[dict]],
    grupo_id: str,
    fixtures: Optional[dict[int, list[dict]]] = None,
    latest_jornada: int = 0,
) -> None:
    target = next(
        (t for t in teams if target_nombre.lower() in t["nombre"].lower()), None
    )
    if not target:
        nombres = ", ".join(t["nombre"] for t in teams)
        print(f"\n  ❌ Equipo '{target_nombre}' no encontrado.")
        print(f"     Equipos en el grupo: {nombres}")
        return

    n = len(teams)
    pos_actual = teams.index(target) + 1
    coef_actual = coef(target["pts"], target["pj"])
    safe_max = n - DESCENSO_DIRECTO_N  # posición 11 para grupo de 14

    print(f"\n{'═'*72}")
    print(f"  SIMULACIÓN — {target['nombre'].upper()}")
    print(f"{'═'*72}")
    print(f"  Posición: {pos_actual}/{n}  |  Pts: {target['pts']}  |  PJ: {target['pj']}  |  Coef: {coef_actual:.3f}")
    print(f"  Jornadas restantes: {remaining}")

    # Muestra rivales pendientes si los tenemos
    if fixtures:
        team_lower = target["nombre"].lower()
        for jornada in sorted(fixtures):
            for m in fixtures[jornada]:
                if team_lower in m["local"].lower() or team_lower in m["visitante"].lower():
                    rival = m["visitante"] if team_lower in m["local"].lower() else m["local"]
                    cond  = "LOCAL" if team_lower in m["local"].lower() else "VISITANTE"
                    print(f"  J{jornada}: {cond} vs {rival}")

    # Coeficientes actuales de los 11ºs de otros grupos (para zona coeficiente)
    others_11th = []
    for gid, gteams in all_standings.items():
        if gid == grupo_id or len(gteams) < 11:
            continue
        t11 = gteams[10]
        others_11th.append({
            "grupo": gid,
            "nombre": t11["nombre"],
            "pts": t11["pts"],
            "pj": t11["pj"],
            "coef_now": coef(t11["pts"], t11["pj"]),
        })

    # ── Enumera todos los escenarios ────────────────────────────────────────
    scenarios = list(product(RESULT_PTS.keys(), repeat=remaining))

    results = []
    for scenario in scenarios:
        extra   = sum(RESULT_PTS[r] for r in scenario)
        new_pts = target["pts"] + extra
        new_pj  = target["pj"] + remaining
        new_coef = coef(new_pts, new_pj)
        label   = "".join(scenario)

        best_pos, worst_pos = _position_range(teams, target["nombre"], new_pts, remaining)
        worst_label, best_label = _scenario_status(best_pos, worst_pos, n)

        # Para zona coef: ¿cómo quedaría vs otros grupos?
        coef_rank = None
        if others_11th:
            # Peor caso para el equipo objetivo: otros 11ºs también suman al máximo
            coefs_worst = sorted(
                [o["coef_now"] + (3 * remaining / (o["pj"] + remaining))
                 if o["pj"] + remaining > 0 else 0
                 for o in others_11th]
            )
            # ¿Cuántos tienen mejor coef que el equipo objetivo en el peor caso?
            above_coef = sum(1 for c in coefs_worst if c > new_coef)
            coef_rank = above_coef + 1  # ranking entre los undécimos

        results.append({
            "label": label,
            "extra": extra,
            "new_pts": new_pts,
            "new_pj": new_pj,
            "new_coef": new_coef,
            "best_pos": best_pos,
            "worst_pos": worst_pos,
            "worst_label": worst_label,
            "best_label": best_label,
            "coef_rank": coef_rank,
        })

    results.sort(key=lambda r: (-r["new_pts"], r["label"]))

    # ── Agrupa y muestra ─────────────────────────────────────────────────────
    guaranteed  = [r for r in results if r["worst_label"] == "✅ GARANTIZADO"]
    coef_zone   = [r for r in results if "COEF" in r["worst_label"] or
                   ("COEF" in r["best_label"] and "GARANTIZADO" not in r["worst_label"])]
    conditional = [r for r in results if "DESCENSO" in r["worst_label"] and
                   r["best_label"] == "✅ SEGURO"]
    relegated   = [r for r in results if r["best_label"] == "❌ DESCENSO"]

    def fmt_row(r: dict) -> str:
        gs = r["label"].count("G")
        es = r["label"].count("E")
        ds = r["label"].count("D")
        coef_info = f" | Rank undécimos: {r['coef_rank']}" if r["coef_rank"] else ""
        return (
            f"     {r['label']}  ({gs}V {es}E {ds}D)  → "
            f"{r['new_pts']} pts | Coef: {r['new_coef']:.3f}"
            f" | Pos: {r['best_pos']}–{r['worst_pos']}{coef_info}"
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

    # Resumen: mínimo de puntos para garantizar permanencia
    min_pts_guaranteed = min(
        (r["new_pts"] for r in guaranteed), default=None
    )
    if min_pts_guaranteed is not None:
        needed = min_pts_guaranteed - target["pts"]
        print(f"\n  → Mínimo para GARANTIZAR permanencia: {needed} pts más "
              f"(total {min_pts_guaranteed} pts)")
    else:
        print(f"\n  → No existe resultado que garantice permanencia matemáticamente "
              f"(depende del resto de grupos o de otros equipos del grupo).")


# ══════════════════════════════════════════════════════════════════════════════
# SALIDA POR PANTALLA
# ══════════════════════════════════════════════════════════════════════════════

def print_standings_table(
    teams: list[dict],
    titulo: str,
    highlight: str = "",
) -> None:
    n = len(teams)
    safe_max = n - DESCENSO_DIRECTO_N  # 11 para grupos de 14

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
        pos = i + 1
        c = coef(t["pts"], t["pj"])

        if pos > safe_max:
            zone = " ↓"   # descenso directo
        elif pos == safe_max:
            zone = " ?"   # zona coeficiente
        else:
            zone = "  "

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
        print(f"\n  📉 DESCENSO DIRECTO (últimas {DESCENSO_DIRECTO_N} posiciones de cada grupo):")
        for t in analysis["direct"]:
            c = coef(t["pts"], t["pj"])
            print(
                f"     [{t['grupo'][-6:]}] {t['nombre']:<32} "
                f"Pts:{t['pts']:>3}  PJ:{t['pj']:>2}  Coef:{c:.3f}"
            )

    if analysis["eleventh_sorted"]:
        print(
            f"\n  ⚠  ZONA COEFICIENTE — undécimos de cada grupo "
            f"(descienden los {DESCENSO_COEF_N} con peor coef):"
        )
        for i, t in enumerate(analysis["eleventh_sorted"]):
            if i < DESCENSO_COEF_N:
                estado = "↓ DESCIENDE"
            else:
                estado = "✅ PERMANECE"
            print(
                f"     {estado}  [{t['grupo'][-6:]}] {t['nombre']:<28} "
                f"Coef:{t['coef']:.3f}  (Pts:{t['pts']:>3}  PJ:{t['pj']:>2})"
            )
    elif n_grupos == 1:
        print(
            f"\n  ℹ  Para el análisis completo de coeficiente entre grupos, "
            f"usa --todos-grupos"
        )


# ══════════════════════════════════════════════════════════════════════════════
# CARGA / GUARDADO DE CACHÉ
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
            "Ejemplo:\n"
            "  python rffm_clasificacion.py -e 'Atlético Madrid B' --todos-grupos\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--equipo", "-e", default="",
        help="Nombre (parcial) del equipo a simular.",
    )
    p.add_argument(
        "--grupo", "-g", default=GRUPO_7_ID,
        help=f"ID del grupo objetivo (default: {GRUPO_7_ID} = Grupo 7).",
    )
    p.add_argument(
        "--competicion", "-c", default=COMPETICION_DEFAULT,
        help=f"ID de la competición (default: {COMPETICION_DEFAULT}).",
    )
    p.add_argument(
        "--temporada", "-t", default=TEMPORADA_DEFAULT,
        help=f"Temporada (default: {TEMPORADA_DEFAULT}).",
    )
    p.add_argument(
        "--total-jornadas", "-j", type=int, default=0,
        help="Total de jornadas de la competición (0 = autodetectar).",
    )
    p.add_argument(
        "--todos-grupos", action="store_true",
        help="Descarga clasificaciones de todos los grupos (necesario para coef. cruzado).",
    )
    p.add_argument(
        "--cache", action="store_true",
        help="Usa datos guardados en clasificacion_cache.json si existen.",
    )
    p.add_argument(
        "--borrar-cache", action="store_true",
        help="Elimina la caché antes de descargar.",
    )
    p.add_argument(
        "--con-calendario", action="store_true",
        help="Intenta descargar el calendario para mostrar rivales pendientes.",
    )
    return p.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = parse_args()
    cache_file = Path("clasificacion_cache.json")

    if args.borrar_cache and cache_file.exists():
        cache_file.unlink()
        print("🗑  Caché eliminada.")

    print("═" * 75)
    print("  RFFM · Preferente Alevín Fútbol 11 · Análisis de Descenso")
    print("═" * 75)

    session = make_session()
    all_standings: dict[str, list[dict]] = {}
    latest_jornada: int = 0

    # ── Intentar usar caché ──────────────────────────────────────────────────
    if args.cache:
        cached = load_cache(cache_file)
        if cached:
            all_standings   = cached.get("standings", {})
            latest_jornada  = cached.get("jornada", 0)
            print(f"\n📂 Datos cargados desde caché (Jornada {latest_jornada})")

    # ── Descarga online ──────────────────────────────────────────────────────
    if not all_standings:
        # Grupos a descargar
        if args.todos_grupos:
            print(f"\n🔍 Buscando grupos de la competición...")
            groups = discover_groups(session, args.temporada, args.competicion)
            if not groups:
                print("  ⚠ No se encontraron grupos. Usando solo el grupo especificado.")
                groups = [{"id": args.grupo, "nombre": "Grupo 7"}]
            # Asegura que el grupo objetivo está incluido
            if not any(g["id"] == args.grupo for g in groups):
                groups.append({"id": args.grupo, "nombre": "Grupo 7"})
            print(f"  Grupos encontrados: {len(groups)}")
        else:
            groups = [{"id": args.grupo, "nombre": "Grupo objetivo"}]

        # Última jornada
        print(f"\n📅 Buscando última jornada disputada...")
        latest_jornada = discover_latest_jornada(
            session, args.temporada, args.competicion, args.grupo
        )
        print(f"  Última jornada: {latest_jornada}")

        # Descarga de clasificaciones
        print(f"\n📊 Descargando clasificaciones...")
        for g in groups:
            gid = g["id"]
            print(f"  · {g['nombre']} ({gid}) ...", end=" ", flush=True)
            soup = fetch_soup(session, "/competicion/clasificaciones", {
                "temporada": args.temporada,
                "competicion": args.competicion,
                "grupo": gid,
                "jornada": str(latest_jornada),
                "tipojuego": TIPO_JUEGO,
            })
            teams = parse_standings(soup) if soup else []
            if teams:
                all_standings[gid] = teams
                print(f"✓ {len(teams)} equipos")
            else:
                print("✗ sin datos")
            time.sleep(0.5)

        save_cache(cache_file, {"jornada": latest_jornada, "standings": all_standings})
        print(f"\n💾 Datos guardados en {cache_file}")

    if not all_standings:
        print("\n❌ No se pudieron obtener datos. Comprueba la conexión y los parámetros.")
        sys.exit(1)

    # Total de jornadas y jornadas restantes
    if args.total_jornadas > 0:
        total_jornadas = args.total_jornadas
    else:
        # Para grupos de 14: (14-1)*2 = 26 jornadas
        grupo_teams = all_standings.get(args.grupo, [])
        n = len(grupo_teams) if grupo_teams else GRUPO_SIZE_STANDARD
        total_jornadas = (n - 1) * 2
        print(f"\n  ℹ Total jornadas estimado: {total_jornadas} (grupos de {n} equipos)")

    remaining = max(0, total_jornadas - latest_jornada)

    # ── Clasificación del grupo objetivo ────────────────────────────────────
    grupo_teams = all_standings.get(args.grupo, [])
    if grupo_teams:
        print_standings_table(
            grupo_teams,
            f"CLASIFICACIÓN GRUPO 7 — PREFERENTE ALEVÍN — Jornada {latest_jornada} "
            f"({remaining} jornada(s) restante(s))",
            highlight=args.equipo,
        )
    else:
        print(f"\n  ⚠ No hay datos para el grupo {args.grupo}")

    # ── Análisis de descenso ─────────────────────────────────────────────────
    analysis = build_relegation_analysis(all_standings)
    print_relegation_analysis(analysis, len(all_standings))

    # ── Calendario (opcional) ─────────────────────────────────────────────────
    fixtures: dict[int, list[dict]] = {}
    if args.con_calendario and remaining > 0:
        print(f"\n📆 Descargando calendario pendiente...")
        fixtures = fetch_remaining_fixtures(
            session, args.temporada, args.competicion, args.grupo, latest_jornada
        )
        if fixtures:
            print(f"  ✓ {sum(len(v) for v in fixtures.values())} partidos pendientes encontrados")
        else:
            print("  ⚠ No se pudo obtener el calendario")

    # ── Simulación para equipo concreto ──────────────────────────────────────
    if args.equipo and grupo_teams and remaining > 0:
        simulate(
            grupo_teams,
            args.equipo,
            remaining,
            all_standings,
            args.grupo,
            fixtures=fixtures if fixtures else None,
            latest_jornada=latest_jornada,
        )
    elif args.equipo and remaining == 0:
        print(f"\n  ℹ La competición ha finalizado. No quedan jornadas para simular.")
    elif not args.equipo:
        print(
            f"\n  ℹ Usa --equipo \"Nombre\" para ver la simulación de escenarios "
            f"del equipo deseado."
        )


if __name__ == "__main__":
    main()
