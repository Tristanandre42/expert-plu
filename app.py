"""
CAdastre — Application PropTech d'analyse de parcelles foncieres
APIs : BAN, API Carto (Cadastre + GPU), IGN Geoplateforme, Georisques
"""

from __future__ import annotations

import io
import re
import sys
import json
import math
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

import requests
from flask import Flask, render_template, request, send_file, jsonify

# Lazy imports for PDF-only libraries (faster cold start)
PILImage = None
colors = None
TA_CENTER = None
A4 = None
ParagraphStyle = None
cm = None
HRFlowable = None
RLImage = None
Paragraph = None
SimpleDocTemplate = None
Spacer = None
Table = None
TableStyle = None

def _load_pdf_libs():
    """Load ReportLab + Pillow on first PDF request only."""
    global PILImage, colors, TA_CENTER, A4, ParagraphStyle, cm
    global HRFlowable, RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    if PILImage is not None:
        return
    from PIL import Image as _PILImage
    from reportlab.lib import colors as _colors
    from reportlab.lib.enums import TA_CENTER as _TA_CENTER
    from reportlab.lib.pagesizes import A4 as _A4
    from reportlab.lib.styles import ParagraphStyle as _ParagraphStyle
    from reportlab.lib.units import cm as _cm
    from reportlab.platypus import (
        HRFlowable as _HRFlowable, Image as _RLImage, Paragraph as _Paragraph,
        SimpleDocTemplate as _SimpleDocTemplate, Spacer as _Spacer,
        Table as _Table, TableStyle as _TableStyle,
    )
    PILImage = _PILImage
    colors = _colors
    TA_CENTER = _TA_CENTER
    A4 = _A4
    ParagraphStyle = _ParagraphStyle
    cm = _cm
    HRFlowable = _HRFlowable
    RLImage = _RLImage
    Paragraph = _Paragraph
    SimpleDocTemplate = _SimpleDocTemplate
    Spacer = _Spacer
    Table = _Table
    TableStyle = _TableStyle

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB for map image uploads
logger.info("Flask app created successfully")


@app.route("/health")
def health():
    return "ok", 200


# ─── Keep-alive: ping ourselves every 10 min to prevent Render free tier sleep
import threading, os  # noqa: E402

def _keep_alive():
    """Self-ping to prevent Render from sleeping the instance."""
    import time
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return  # Not on Render, skip
    url = url.rstrip("/") + "/health"
    while True:
        time.sleep(600)  # 10 minutes
        try:
            requests.get(url, timeout=10)
        except Exception:
            pass

_keep_alive_thread = threading.Thread(target=_keep_alive, daemon=True)
_keep_alive_thread.start()


# ─── API base URLs ────────────────────────────────────────────────────────────

BAN_API    = "https://api-adresse.data.gouv.fr"
CARTO_API  = "https://apicarto.ign.fr/api"
IGN_WMS    = "https://data.geopf.fr/wms-r/wms"
IGN_ALTI   = "https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest"
GEORISQUES = "https://www.georisques.gouv.fr/api/v1"

TIMEOUT = 15


# ═══════════════════════════════════════════════════════════════════════════════
#  API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

# ── BAN (Base Adresse Nationale) ──────────────────────────────────────────────

def ban_autocomplete(query: str, limit: int = 5) -> list:
    """Autocompletion via BAN."""
    r = requests.get(
        f"{BAN_API}/search/",
        params={"q": query, "autocomplete": 1, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("features", [])


def geocode(address: str):
    """Adresse -> (lon, lat, properties)."""
    r = requests.get(
        f"{BAN_API}/search/",
        params={"q": address, "limit": 1},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    features = r.json().get("features", [])
    if not features:
        return None, None, None
    f = features[0]
    lon, lat = f["geometry"]["coordinates"]
    return lon, lat, f["properties"]


# ── API Carto — Cadastre ─────────────────────────────────────────────────────

def parcel_by_coords(lon: float, lat: float) -> dict:
    geom = json.dumps({"type": "Point", "coordinates": [lon, lat]})
    r = requests.get(
        f"{CARTO_API}/cadastre/parcelle",
        params={"geom": geom, "_limit": 1},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _dist_point_geom_m(lon: float, lat: float, geom: dict) -> float:
    """Distance (m) entre un point et le contour d'une geometrie Polygon/MultiPolygon."""
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    best = float("inf")

    def seg_dist(ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(ax, ay)
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        return math.hypot(ax + t * dx, ay + t * dy)

    def walk(c):
        nonlocal best
        if c and isinstance(c[0][0], (int, float)):
            pts = [((p[0] - lon) * kx, (p[1] - lat) * ky) for p in c]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                best = min(best, seg_dist(ax, ay, bx, by))
        else:
            for sub in c:
                walk(sub)

    walk(geom.get("coordinates", []))
    return best


def nearest_parcel(lon: float, lat: float) -> tuple[dict | None, float]:
    """
    Parcelle la plus proche d'un point tombe hors parcelle (adresse geocodee
    sur la voirie). Cherche dans un carre de 15 m puis 40 m autour du point.
    Retourne (feature, distance_m) ou (None, 0).
    """
    for radius in (15, 40):
        dlat = radius / 110540.0
        dlon = radius / (111320.0 * math.cos(math.radians(lat)))
        poly = {"type": "Polygon", "coordinates": [[
            [lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
            [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
            [lon - dlon, lat - dlat],
        ]]}
        r = requests.get(
            f"{CARTO_API}/cadastre/parcelle",
            params={"geom": json.dumps(poly), "_limit": 30},
            timeout=TIMEOUT,
        )
        if not r.ok:
            continue
        feats = r.json().get("features", [])
        if feats:
            best = min(feats, key=lambda f: _dist_point_geom_m(lon, lat, f.get("geometry", {})))
            return best, _dist_point_geom_m(lon, lat, best.get("geometry", {}))
    return None, 0.0


def georisques_rapport_url(lon: float, lat: float, code_insee: str = "", label: str = "") -> str:
    """Lien direct vers le rapport 'Les risques pres de chez moi' de Georisques."""
    from urllib.parse import urlencode
    params = {
        "form-adresse": "true", "isCadastre": "false", "typeForm": "adresse",
        "codeInsee": code_insee or "", "lon": f"{lon:.6f}", "lat": f"{lat:.6f}",
        "go_back": "/", "adresse": label or "",
    }
    return ("https://www.georisques.gouv.fr/mes-risques/"
            "connaitre-les-risques-pres-de-chez-moi/rapport2?" + urlencode(params))


def parse_ref(ref: str):
    """
    Parse une reference cadastrale :
    '69123AB0001' -> ('69123', 'AB', '0001')
    '69 123 A 42' -> ('69123', 'A', '0042')
    """
    s = ref.strip().upper().replace(" ", "").replace("-", "")
    m = re.match(r"^(\d{5})([A-Z]{1,2})(\d{1,4})$", s)
    if m:
        return m.group(1), m.group(2), m.group(3).zfill(4)
    return None, None, None


def parcel_by_ref(code_insee: str, section: str, numero: str) -> dict:
    params = {"code_insee": code_insee, "_limit": 1}
    if section:
        params["section"] = section
    if numero:
        params["numero"] = numero
    r = requests.get(f"{CARTO_API}/cadastre/parcelle", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def commune_name(code_insee: str) -> str:
    try:
        r = requests.get(
            f"{CARTO_API}/cadastre/commune",
            params={"code_insee": code_insee},
            timeout=TIMEOUT,
        )
        if r.ok:
            feats = r.json().get("features", [])
            if feats:
                return feats[0]["properties"].get("nom", "")
    except Exception:
        pass
    return ""


# ── IGN Altimetrie ───────────────────────────────────────────────────────────

def get_elevation(lon: float, lat: float) -> dict | None:
    """Altitude via IGN Geoplateforme."""
    try:
        r = requests.get(
            f"{IGN_ALTI}/elevation.json",
            params={"lon": lon, "lat": lat, "resource": "ign_rge_alti_wld", "zonly": "false"},
            timeout=TIMEOUT,
        )
        if r.ok:
            data = r.json()
            elevations = data.get("elevations", [])
            if elevations:
                return elevations[0]
    except Exception:
        pass
    return None


# ── API Carto — GPU (Geoportail de l'Urbanisme) ─────────────────────────────

def _gpu_query(endpoint: str, lon: float, lat: float) -> dict:
    """Requete generique GPU avec un point."""
    geom = json.dumps({"type": "Point", "coordinates": [lon, lat]})
    r = requests.get(
        f"{CARTO_API}/gpu/{endpoint}",
        params={"geom": geom},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_zonage(lon: float, lat: float) -> list:
    """Zonage PLU (U, AU, A, N...) via GPU zone-urba."""
    data = _gpu_query("zone-urba", lon, lat)
    return data.get("features", [])


def get_plu_document(lon: float, lat: float) -> list:
    """Document d'urbanisme (PLU/PLUi/POS/CC) via GPU."""
    data = _gpu_query("document", lon, lat)
    return data.get("features", [])


def get_prescriptions(lon: float, lat: float) -> list:
    """Prescriptions surfaciques (inclut ABF, monuments, etc.)."""
    data = _gpu_query("prescription-surf", lon, lat)
    return data.get("features", [])


# ── Georisques ───────────────────────────────────────────────────────────────

def get_risques_commune(code_insee: str) -> list:
    """Risques reglementaires d'une commune via GASPAR."""
    try:
        r = requests.get(
            f"{GEORISQUES}/gaspar/risques",
            params={"code_insee": code_insee, "rayon": 1000},
            timeout=TIMEOUT,
        )
        if r.ok:
            return r.json().get("data", [])
    except Exception:
        pass
    return []


def get_georisques_detail(lon: float, lat: float) -> dict:
    """Informations detaillees de risques autour d'un point."""
    result = {}
    # Risques naturels
    try:
        r = requests.get(
            f"{GEORISQUES}/resultats_rapport_risque",
            params={"latlon": f"{lat},{lon}"},
            timeout=TIMEOUT,
        )
        if r.ok:
            result = r.json()
    except Exception:
        pass
    return result


def flatten_risques(risques_commune: list) -> list[dict]:
    """
    Aplatit la structure GASPAR (commune -> risques_detail) en une liste
    propre de risques par famille (codes a 2 chiffres), dedupliquee.
    Retourne [{"libelle": str, "code": str}, ...].
    """
    familles: dict[str, str] = {}
    sous_risques: dict[str, str] = {}
    for commune in risques_commune or []:
        for d in commune.get("risques_detail", []):
            lib = (d.get("libelle_risque_long")
                   or d.get("libelle_risque_jo")
                   or d.get("libelle_risque") or "").strip()
            code = str(d.get("num_risque", "")).strip()
            if not lib:
                continue
            if len(code) <= 2:
                familles.setdefault(code or lib, lib)
            else:
                sous_risques.setdefault(code, lib)
    # Si aucune famille (cas rare), on retombe sur les sous-risques
    source = familles if familles else sous_risques
    out = []
    seen = set()
    for code, lib in source.items():
        key = lib.lower()
        if key not in seen:
            seen.add(key)
            out.append({"libelle": lib, "code": code})
    return out


# ── cadastre.gouv.fr — extrait de plan officiel (PDF) ────────────────────────

CADASTRE_GOUV = "https://www.cadastre.gouv.fr/scpc"
_CAD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def wgs84_to_cc(lon: float, lat: float, zone: int) -> tuple[float, float]:
    """WGS84/RGF93 -> Lambert conique conforme 9 zones (CC42 a CC50), ellipsoide GRS80."""
    a = 6378137.0
    f = 1 / 298.257222101
    e = math.sqrt(2 * f - f * f)
    lon0 = math.radians(3.0)
    phi0, phi1, phi2 = (math.radians(zone), math.radians(zone - 0.75), math.radians(zone + 0.75))
    x0, y0 = 1_700_000.0, (zone - 41) * 1_000_000 + 200_000

    def m(p):
        return math.cos(p) / math.sqrt(1 - (e * math.sin(p)) ** 2)

    def t(p):
        return math.tan(math.pi / 4 - p / 2) / ((1 - e * math.sin(p)) / (1 + e * math.sin(p))) ** (e / 2)

    n = (math.log(m(phi1)) - math.log(m(phi2))) / (math.log(t(phi1)) - math.log(t(phi2)))
    big_f = m(phi1) / (n * t(phi1) ** n)
    r0 = a * big_f * t(phi0) ** n
    r = a * big_f * t(math.radians(lat)) ** n
    th = n * (math.radians(lon) - lon0)
    return x0 + r * math.sin(th), y0 + r0 - r * math.cos(th)


# Cadre carte d'un extrait A4 portrait, en centiemes de mm (valeurs du site cadastre.gouv.fr)
_A4_PORTRAIT_CMM = (19550, 21100)
_ECHELLES_EXTRAIT = (500, 1000, 2000, 2500, 5000)


def get_extrait_cadastral(code_insee: str, section: str, numero: str,
                          prefixe: str = "000", ville: str = "",
                          parcel_size_m: float = 0,
                          geom: dict | None = None) -> bytes | None:
    """
    Telecharge l'extrait de plan cadastral officiel (PDF DGFiP) depuis
    cadastre.gouv.fr en rejouant le parcours du site :
      1. accueil.do            -> session JSESSIONID + token CSRF
      2. rechercherParReferenceCadastrale.do -> identifiants parcelle/feuille
         (avec desambiguisation codeCommune si plusieurs communes homonymes)
      3. afficherCarteParcelle.do -> centre de la parcelle (Lambert CC)
      4. imprimerExtraitCadastral.do -> extrait normalise A4 (cartouche DGFiP,
         carroyage), cadre sur le centre de la parcelle ; a defaut, version
         non normalisee (imprimerExtraitCadastralNonNormalise.do)
    """
    s = requests.Session()
    s.headers["User-Agent"] = _CAD_UA

    # 1. Session + CSRF
    r = s.get(f"{CADASTRE_GOUV}/accueil.do", timeout=20)
    m = re.search(r"CSRF_TOKEN=([A-Z0-9-]+)", r.text)
    if not m:
        return None
    csrf = m.group(1)

    # 2. Recherche par reference cadastrale
    dep = code_insee[:3] if code_insee[:2] in ("97", "98") else "0" + code_insee[:2]
    if not ville:
        ville = commune_name(code_insee)
    if not ville:
        return None
    ville = _strip_accents(ville).upper()

    search_url = f"{CADASTRE_GOUV}/rechercherParReferenceCadastrale.do?CSRF_TOKEN={csrf}"
    data = {
        "rechercheType": "1",
        "codeDepartement": dep,
        "ville": ville,
        "codePostal": "",
        "prefixeParcelle": (prefixe or "000").zfill(3),
        "sectionLibelle": section.strip().upper(),
        "numeroParcelle": str(numero).zfill(4),
        "prefixeFeuille": "000",
        "feuilleLibelle": "",
        "nbResultatParPage": "10",
    }
    r = s.post(search_url, data=data, timeout=20)
    link_re = r"afficherCarteParcelle\.do\?[^\"']*?p=([A-Z0-9]+)&(?:amp;)?f=([A-Z0-9]+)"
    links = re.findall(link_re, r.text)

    if not links:
        # Desambiguisation : plusieurs communes homonymes -> select codeCommune
        # dont la valeur se termine par le numero de commune INSEE sur 3 chiffres
        # (ex : 42097 -> "I0097", 69382 -> "R0382", 13201 -> "81201").
        part = code_insee[-3:]
        options = re.findall(r'<option value="([0-9A-Z]{5})"', r.text)
        code_commune = next((o for o in options if o[-3:] == part), None)
        if not code_commune:
            return None
        data["codeCommune"] = code_commune
        r = s.post(search_url, data=data, timeout=20)
        links = re.findall(link_re, r.text)
        if not links:
            return None

    p_id, f_id = links[0]

    # 3. Carte de la parcelle -> coordonnees du centre (projection de la feuille)
    r = s.get(f"{CADASTRE_GOUV}/afficherCarteParcelle.do",
              params={"CSRF_TOKEN": csrf, "p": p_id, "f": f_id}, timeout=20)
    m = re.search(r"Point\(([0-9.]+),([0-9.]+)\)", r.text)
    if not m:
        return None
    x, y = float(m.group(1)), float(m.group(2))

    # 4a. Extrait normalise (identique au parcours "Outils avances > Imprimer").
    # Centre du cadre = centre de l'emprise de la parcelle projetee dans la
    # zone CC de la feuille ; a defaut, la pastille de la parcelle.
    cx, cy = x, y
    span_x = span_y = 0.0
    if geom and geom.get("coordinates"):
        try:
            zone = 41 + round((y - 200_000) / 1_000_000)
            pts = []

            def walk(c):
                if c and isinstance(c[0], (int, float)):
                    pts.append(wgs84_to_cc(c[0], c[1], zone))
                else:
                    for sub in c:
                        walk(sub)

            walk(geom["coordinates"])
            if pts:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                mx, my = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                if abs(mx - x) < 2000 and abs(my - y) < 2000:  # garde-fou zone/projection
                    cx, cy = mx, my
                    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
        except Exception:
            pass

    echelle = _ECHELLES_EXTRAIT[-1]
    for ech in _ECHELLES_EXTRAIT:
        if (span_x <= _A4_PORTRAIT_CMM[0] * 1e-5 * ech * 0.85
                and span_y <= _A4_PORTRAIT_CMM[1] * 1e-5 * ech * 0.85):
            echelle = ech
            break
    half_w = _A4_PORTRAIT_CMM[0] * 1e-5 * echelle / 2
    half_h = _A4_PORTRAIT_CMM[1] * 1e-5 * echelle / 2
    try:
        r = s.post(
            f"{CADASTRE_GOUV}/imprimerExtraitCadastral.do?CSRF_TOKEN={csrf}",
            data={
                "MAPBBOX": f"{cx - half_w},{cy - half_h},{cx + half_w},{cy + half_h}",
                "MAPROTATION": "0", "TAILLEPAGE": "A4", "ORIENTPAGE": "Portrait",
                "RFV_REF": "", "RFV_X": str(x), "RFV_Y": str(y),
                "ECHELLE": str(echelle), "NATURE": "V", "RESOLUTION": "", "DRAPEAU": "false",
            },
            timeout=60,
        )
        if r.ok and r.content[:4] == b"%PDF":
            return r.content
    except requests.exceptions.RequestException as e:
        logger.warning("extrait normalise indisponible: %s", e)

    # 4b. Secours : extrait non normalise ~1/500
    w = max(95.0, parcel_size_m * 1.4)
    h = w * 675.0 / 700.0
    bbox = f"{x - w/2},{y - h/2},{x + w/2},{y + h/2}"
    r = s.post(
        f"{CADASTRE_GOUV}/imprimerExtraitCadastralNonNormalise.do?CSRF_TOKEN={csrf}",
        data={
            "WIDTH": "700", "HEIGHT": "675", "MAPBBOX": bbox,
            "SLD_BODY": "", "RFV_REF": f_id,
            "DRAPEAU": "false", "SELECTION": p_id,
        },
        timeout=40,
    )
    if r.ok and r.headers.get("content-type", "").startswith("application/pdf"):
        return r.content
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  GEOMETRY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def bbox_from_geometry(geom: dict) -> list:
    lons, lats = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            lons.append(c[0])
            lats.append(c[1])
        else:
            for sub in c:
                walk(sub)

    walk(geom["coordinates"])
    return [min(lons), min(lats), max(lons), max(lats)]


def centroid_from_geometry(geom: dict) -> tuple[float, float]:
    """Centroide approximatif (moyenne des coordonnees)."""
    bbox = bbox_from_geometry(geom)
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def pad_bbox(bbox: list, factor: float = 3.0) -> list:
    dlon = (bbox[2] - bbox[0]) * (factor - 1) / 2
    dlat = (bbox[3] - bbox[1]) * (factor - 1) / 2
    return [bbox[0] - dlon, bbox[1] - dlat, bbox[2] + dlon, bbox[3] + dlat]


# ═══════════════════════════════════════════════════════════════════════════════
#  WMS MAP IMAGE
# ═══════════════════════════════════════════════════════════════════════════════

def wms_image(bbox: list, layers: str, width=800, height=600, transparent=True) -> bytes | None:
    fmt = "image/png" if transparent else "image/jpeg"
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": layers, "STYLES": "", "CRS": "EPSG:4326",
        "BBOX": f"{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}",
        "WIDTH": width, "HEIGHT": height, "FORMAT": fmt,
    }
    try:
        r = requests.get(IGN_WMS, params=params, timeout=30)
        ct = r.headers.get("content-type", "")
        if r.ok and ct.startswith("image"):
            return r.content
    except Exception:
        pass
    return None


def build_map(bbox: list, geom: dict | None = None, width=800, height=600) -> io.BytesIO | None:
    """Carte 2D : fond Plan IGN + parcelles cadastrales + contour rouge de la parcelle."""
    _load_pdf_libs()
    from PIL import ImageDraw

    has_data = False

    # 1. Fond Plan IGN (ou blanc si indisponible)
    plan_data = wms_image(bbox, "GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", width, height, transparent=False)
    if plan_data:
        base = PILImage.open(io.BytesIO(plan_data)).convert("RGBA")
        has_data = True
    else:
        base = PILImage.new("RGBA", (width, height), (255, 255, 255, 255))

    # 2. Overlay parcelles cadastrales (transparent)
    parcels_data = wms_image(bbox, "CADASTRALPARCELS.PARCELLAIRE_EXPRESS", width, height, transparent=True)
    if parcels_data:
        overlay = PILImage.open(io.BytesIO(parcels_data)).convert("RGBA")
        base = PILImage.alpha_composite(base, overlay)
        has_data = True

    # 3. Contour rouge de la parcelle selectionnee
    if geom and geom.get("coordinates"):
        draw = ImageDraw.Draw(base)
        has_data = True

        def geo_to_pixel(lon, lat):
            px = (lon - bbox[0]) / (bbox[2] - bbox[0]) * width
            py = (bbox[3] - lat) / (bbox[3] - bbox[1]) * height
            return (int(round(px)), int(round(py)))

        def draw_ring(ring):
            points = [geo_to_pixel(c[0], c[1]) for c in ring]
            if len(points) >= 3:
                for i in range(len(points) - 1):
                    draw.line([points[i], points[i + 1]], fill=(220, 38, 38, 255), width=3)
                draw.line([points[-1], points[0]], fill=(220, 38, 38, 255), width=3)

        geom_type = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if geom_type == "Polygon":
            for ring in coords:
                draw_ring(ring)
        elif geom_type == "MultiPolygon":
            for polygon in coords:
                for ring in polygon:
                    draw_ring(ring)

    if not has_data:
        return None

    combined = base.convert("RGB")
    buf = io.BytesIO()
    combined.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
#  PDF GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

# Color constants — initialized lazily with _load_pdf_libs()
BLUE = DARK_BLUE = LIGHT_BLUE = ACCENT = GREY_LINE = GREY_BG = None
TEXT_DARK = TEXT_MID = TEXT_LIGHT = GREEN_BG = GREEN_TXT = RED_BG = RED_TXT = None

def _init_colors():
    global BLUE, DARK_BLUE, LIGHT_BLUE, ACCENT, GREY_LINE, GREY_BG
    global TEXT_DARK, TEXT_MID, TEXT_LIGHT, GREEN_BG, GREEN_TXT, RED_BG, RED_TXT
    if BLUE is not None:
        return
    BLUE       = colors.HexColor("#1a3a6b")
    DARK_BLUE  = colors.HexColor("#0f2847")
    LIGHT_BLUE = colors.HexColor("#e8edf8")
    ACCENT     = colors.HexColor("#2563eb")
    GREY_LINE  = colors.HexColor("#d1d5db")
    GREY_BG    = colors.HexColor("#f8f9fa")
    TEXT_DARK   = colors.HexColor("#1f2937")
    TEXT_MID    = colors.HexColor("#4b5563")
    TEXT_LIGHT  = colors.HexColor("#6b7280")
    GREEN_BG   = colors.HexColor("#ecfdf5")
    GREEN_TXT  = colors.HexColor("#065f46")
    RED_BG     = colors.HexColor("#fef2f2")
    RED_TXT    = colors.HexColor("#991b1b")


def _section_header(title: str, avail_w: float) -> Table:
    """Bandeau colore pour titres de section."""
    tbl = Table(
        [[Paragraph(f"<b>{title}</b>",
                    ParagraphStyle("sh", fontSize=11, fontName="Helvetica-Bold",
                                   textColor=colors.white, leading=14))]],
        colWidths=[avail_w],
        rowHeights=[0.7 * cm],
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), BLUE),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ROUNDEDCORNERS", [4, 4, 0, 0]),
    ]))
    return tbl


def _kv_table(rows: list[tuple[str, str]], avail_w: float) -> Table:
    """Tableau cle-valeur professionnel a 2 colonnes."""
    key_w = avail_w * 0.35
    val_w = avail_w * 0.65
    key_style = ParagraphStyle("k", fontSize=9, fontName="Helvetica-Bold",
                                textColor=TEXT_MID, leading=12)
    val_style = ParagraphStyle("v", fontSize=9.5, fontName="Helvetica",
                                textColor=TEXT_DARK, leading=12)
    data = []
    for k, v in rows:
        data.append([Paragraph(k, key_style), Paragraph(str(v), val_style)])
    tbl = Table(data, colWidths=[key_w, val_w])
    style_cmds = [
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (0, -1), 10),
        ("LEFTPADDING",   (1, 0), (1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW",     (0, 0), (-1, -2), 0.5, GREY_LINE),
    ]
    # Alternate row background
    for i in range(len(data)):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), GREY_BG))
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def make_pdf(parcel: dict, address_props: dict | None = None,  # noqa: C901
             zonage_info: list | None = None,
             plu_docs: list | None = None,
             patrimoine_info: list | None = None,
             all_prescriptions: list | None = None,
             elevation: float | None = None,
             errial_url: str | None = None) -> io.BytesIO:
    _load_pdf_libs()
    _init_colors()
    props = parcel.get("properties", {})
    geom  = parcel.get("geometry", {})

    code_insee = props.get("code_insee", "")
    section    = props.get("section", "")
    numero     = props.get("numero", "")
    contenance = props.get("contenance")

    city   = props.get("nom_com", "") or commune_name(code_insee) or code_insee
    bbox   = bbox_from_geometry(geom)
    clon, clat = centroid_from_geometry(geom)

    # Carte generee cote serveur (Plan IGN + cadastre + contour rouge)
    padded = pad_bbox(bbox, 3.0)
    map_buf = build_map(padded, geom)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=f"Parcelle cadastrale {code_insee} {section} {numero}",
        author="Expert PLU",
    )

    avail_w = A4[0] - 3 * cm

    # ── Styles
    h1 = ParagraphStyle("h1", fontSize=22, textColor=DARK_BLUE,
                         fontName="Helvetica-Bold", spaceAfter=2, alignment=TA_CENTER,
                         leading=26)
    sub = ParagraphStyle("sub", fontSize=10, textColor=TEXT_LIGHT,
                         spaceAfter=6, alignment=TA_CENTER, leading=13)
    ref_style = ParagraphStyle("ref", fontSize=13, textColor=BLUE,
                                fontName="Helvetica-Bold", alignment=TA_CENTER,
                                spaceAfter=2, leading=16)
    legend = ParagraphStyle("legend", fontSize=7, textColor=TEXT_LIGHT,
                            alignment=TA_CENTER, fontName="Helvetica-Oblique",
                            spaceBefore=3, spaceAfter=6)
    foot = ParagraphStyle("foot", fontSize=7, textColor=TEXT_LIGHT,
                          alignment=TA_CENTER, leading=10)
    normal = ParagraphStyle("norm", fontSize=9.5, spaceAfter=3, leading=12,
                            textColor=TEXT_DARK)
    small = ParagraphStyle("small", fontSize=8.5, textColor=TEXT_MID,
                           spaceAfter=2, leading=11)
    bullet = ParagraphStyle("bullet", fontSize=9.5, spaceAfter=2, leading=12,
                            textColor=TEXT_DARK, leftIndent=12, bulletIndent=0)
    link_p = ParagraphStyle("linkp", fontSize=8.5, textColor=ACCENT,
                            spaceAfter=3, leading=11, leftIndent=12)

    if contenance:
        area_str = f"{int(contenance):,} m\u00b2  ({contenance / 10_000:.4f} ha)".replace(",", "\u202f")
    else:
        area_str = "N/A"

    alt_str = f"{elevation:.1f} m" if elevation is not None else "N/D"
    ref_full = f"{code_insee} {section} {numero}"

    # ═══════════════════════════════════════════════════════════════════════════
    #  STORY
    # ═══════════════════════════════════════════════════════════════════════════

    story = []

    # ── En-tete
    # Barre de titre pleine largeur
    title_tbl = Table(
        [[Paragraph("<b>FICHE PARCELLE</b>",
                     ParagraphStyle("t", fontSize=18, fontName="Helvetica-Bold",
                                    textColor=colors.white, alignment=TA_CENTER, leading=22)),
          ]],
        colWidths=[avail_w],
        rowHeights=[1.0 * cm],
    )
    title_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), DARK_BLUE),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [6, 6, 0, 0]),
    ]))
    subtitle_tbl = Table(
        [[Paragraph(f"Analyse fonciere et urbanistique — {city}",
                     ParagraphStyle("st", fontSize=9, textColor=TEXT_MID,
                                    alignment=TA_CENTER, leading=12))]],
        colWidths=[avail_w],
        rowHeights=[0.55 * cm],
    )
    subtitle_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [0, 0, 6, 6]),
    ]))
    story += [title_tbl, subtitle_tbl, Spacer(1, 0.4 * cm)]

    # ── Identification parcelle
    story.append(_section_header("Identification de la parcelle", avail_w))
    id_rows = []
    if address_props:
        id_rows.append(("Adresse", address_props.get("label", "")))
    id_rows += [
        ("Reference cadastrale", ref_full),
        ("Commune", f"{city} ({code_insee})"),
        ("Section / Parcelle", f"{section} / {numero}"),
        ("Contenance", area_str),
    ]
    story += [_kv_table(id_rows, avail_w), Spacer(1, 0.3 * cm)]

    # ── Localisation GPS
    story.append(_section_header("Localisation", avail_w))
    gps_rows = [
        ("Latitude", f"{clat:.6f}"),
        ("Longitude", f"{clon:.6f}"),
        ("Altitude", alt_str),
    ]
    story += [_kv_table(gps_rows, avail_w), Spacer(1, 0.3 * cm)]

    # ── Carte cadastrale 2D
    if map_buf:
        story.append(_section_header("Plan cadastral", avail_w))
        img_h = min(avail_w * 0.6, 11 * cm)
        # Cadre autour de la carte
        map_tbl = Table(
            [[RLImage(map_buf, width=avail_w - 4, height=img_h)]],
            colWidths=[avail_w],
        )
        map_tbl.setStyle(TableStyle([
            ("BOX",        (0, 0), (-1, -1), 1, GREY_LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",  (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]))
        story += [
            map_tbl,
            Paragraph("Source : IGN Geoplateforme / DGFiP — Donnees indicatives, sans valeur juridique", legend),
        ]

    # ── Document d'urbanisme (PLU)
    story += [Spacer(1, 0.2 * cm), _section_header("Document d'urbanisme", avail_w)]
    if plu_docs:
        doc_rows = []
        for d in plu_docs:
            dp = d.get("properties", {})
            typedoc = dp.get("du_type") or dp.get("typedoc") or "?"
            titre = dp.get("grid_title", "")
            statut = dp.get("gpu_status", "")
            ref = dp.get("grid_name") or dp.get("partition", "")
            date = (dp.get("gpu_timestamp", "") or "")[:10]
            doc_rows.append(("Type de document", typedoc))
            if titre:
                doc_rows.append(("Intitule", titre))
            if statut:
                doc_rows.append(("Statut", statut))
            if ref:
                doc_rows.append(("Reference", ref))
            if date:
                doc_rows.append(("Mise a jour GPU", date))
        story.append(_kv_table(doc_rows, avail_w))
    else:
        story.append(Paragraph("Aucun document d'urbanisme trouve.", small))

    # ── Zonage PLU
    story += [Spacer(1, 0.2 * cm), _section_header("Zonage PLU", avail_w)]
    if zonage_info:
        zone_rows = []
        for z in zonage_info:
            zp = z.get("properties", {})
            zone_type = zp.get("typezone", "?")
            libelle = zp.get("libelle", zp.get("libelong", ""))
            dest = zp.get("destdomi", "")
            nomfic = zp.get("nomfic", "")
            zone_label = zone_type
            if libelle:
                zone_label += f" — {libelle}"
            zone_rows.append(("Zone", zone_label))
            if dest:
                zone_rows.append(("Destination dominante", dest))
            if nomfic:
                zone_rows.append(("Reglement", nomfic))
        story.append(_kv_table(zone_rows, avail_w))
    else:
        story.append(Paragraph("Aucun zonage PLU trouve pour cette parcelle.", small))

    # ── Patrimoine / ABF
    story += [Spacer(1, 0.2 * cm), _section_header("Patrimoine et servitudes (ABF)", avail_w)]
    if patrimoine_info:
        pat_rows = []
        for p_feat in patrimoine_info:
            pp = p_feat.get("properties", {})
            lib = pp.get("libelle", pp.get("txt", "Prescription surfacique"))
            typep = pp.get("typepsc", "")
            pat_rows.append((typep, lib))
        story.append(_kv_table(pat_rows, avail_w))
    else:
        # Badge vert "aucune servitude"
        ok_tbl = Table(
            [[Paragraph("Aucun perimetre de protection ABF identifie",
                         ParagraphStyle("ok", fontSize=9, textColor=GREEN_TXT, leading=12))]],
            colWidths=[avail_w],
        )
        ok_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), GREEN_BG),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(ok_tbl)

    # ── Prescriptions surfaciques (non ABF)
    non_abf = []
    if all_prescriptions:
        for p in all_prescriptions:
            pp = p.get("properties", {})
            typepsc = pp.get("typepsc", "")
            if not typepsc.startswith("AC") and not typepsc.startswith("05"):
                non_abf.append(pp)
    if non_abf:
        story += [Spacer(1, 0.2 * cm), _section_header("Autres prescriptions surfaciques", avail_w)]
        presc_rows = []
        for pp in non_abf:
            typep = pp.get("typepsc", "")
            lib = pp.get("libelle", pp.get("txt", ""))
            presc_rows.append((typep, lib))
        story.append(_kv_table(presc_rows, avail_w))

    # ── Liens utiles
    story += [Spacer(1, 0.2 * cm), _section_header("Liens utiles", avail_w)]
    links_rows = []
    if errial_url:
        links_rows.append(("Risques (Georisques)", errial_url))
    dvf_url = (
        f"https://explore.data.gouv.fr/immobilier?onglet=carte"
        f"&lat={clat}&lng={clon}&zoom=18"
    )
    links_rows.append(("Valeurs foncieres (DVF)", dvf_url))
    atlas_bbox = bbox_from_geometry(geom)
    atlas_pad = pad_bbox(atlas_bbox, 1.5)
    atlas_url = (
        f"http://atlas.patrimoines.culture.fr/atlas/trunk/index.php"
        f"?ap_theme=DOMREG&ap_bbox={atlas_pad[0]:.6f}%3B{atlas_pad[1]:.6f}"
        f"%3B{atlas_pad[2]:.6f}%3B{atlas_pad[3]:.6f}"
    )
    links_rows.append(("Atlas des Patrimoines", atlas_url))
    gpu_url = f"https://www.geoportail-urbanisme.gouv.fr/map/#tile=1&lon={clon}&lat={clat}&zoom=19"
    links_rows.append(("Geoportail de l'Urbanisme", gpu_url))
    geo_url = f"https://www.geoportail.gouv.fr/carte?c={clon},{clat}&z=17&permalink=yes"
    links_rows.append(("Geoportail IGN", geo_url))

    link_key_style = ParagraphStyle("lk", fontSize=9, fontName="Helvetica-Bold",
                                     textColor=TEXT_MID, leading=11)
    link_val_style = ParagraphStyle("lv", fontSize=7.5, textColor=ACCENT, leading=10)
    link_data = []
    for k, v in links_rows:
        # Echapper & pour le parser XML de ReportLab (sinon &lat= casse le lien)
        v_xml = v.replace("&", "&amp;")
        disp = v[:80] + ("..." if len(v) > 80 else "")
        disp_xml = disp.replace("&", "&amp;")
        link_data.append([
            Paragraph(k, link_key_style),
            Paragraph(f'<a href="{v_xml}" color="#2563eb">{disp_xml}</a>', link_val_style),
        ])
    link_tbl = Table(link_data, colWidths=[avail_w * 0.30, avail_w * 0.70])
    link_style_cmds = [
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (0, -1), 10),
        ("LEFTPADDING",   (1, 0), (1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW",     (0, 0), (-1, -2), 0.5, GREY_LINE),
    ]
    for i in range(len(link_data)):
        if i % 2 == 0:
            link_style_cmds.append(("BACKGROUND", (0, i), (-1, i), GREY_BG))
    link_tbl.setStyle(TableStyle(link_style_cmds))
    story.append(link_tbl)

    # ── Footer
    story += [
        Spacer(1, 0.6 * cm),
        HRFlowable(width="100%", thickness=1, color=BLUE, spaceAfter=6),
        Paragraph(
            f"Document genere le {datetime.now().strftime('%d/%m/%Y a %H:%M')}",
            foot,
        ),
        Paragraph(
            "Sources : IGN / DGFiP / GPU / Georisques — Ce document n'a aucune valeur juridique",
            foot,
        ),
    ]

    doc.build(story)
    buf.seek(0)
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
#  FICHE INFORMATION DETAILLEE D'UNE PARCELLE (Geoportail de l'Urbanisme) -> PDF
# ═══════════════════════════════════════════════════════════════════════════════

GPU_SITE = "https://www.geoportail-urbanisme.gouv.fr"


def gpu_parcel_id(props: dict) -> str:
    """departement_commune_arrondissement_communeAbsorbee_section_numero."""
    return "_".join([
        str(props.get("code_dep") or ""), str(props.get("code_com") or ""),
        str(props.get("code_arr") or "000"), str(props.get("com_abs") or "000"),
        str(props.get("section") or ""), str(props.get("numero") or ""),
    ])


def get_fiche_gpu(parcel_id: str) -> dict | None:
    """Donnees de la fiche GPU (celles qu'affiche /map/parcel-info/<id>/)."""
    r = requests.get(f"{GPU_SITE}/api/feature-info/parcel/{parcel_id}/fiche",
                     headers={"User-Agent": _CAD_UA}, timeout=30)
    if not r.ok:
        return None
    data = r.json()
    if not data.get("parcel", {}).get("features"):
        return None
    return data


def _pdf_txt(s) -> str:
    """Texte sur pour un Paragraph ReportLab en Helvetica (XML echappe, cp1252)."""
    s = str(s or "").replace(" ", " ").replace(" ", " ").strip()
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "".join(c if c.encode("cp1252", "ignore") else "?" for c in s)


def make_fiche_gpu_pdf(fiche: dict, online_url: str) -> io.BytesIO:  # noqa: C901
    from gpu_tables import SUP_LIBELLES, PRESCRIPTION_GROUPES
    _load_pdf_libs()
    _init_colors()

    parcel = fiche["parcel"]["features"][0]
    p = parcel.get("properties", {})
    geom = parcel.get("geometry", {})
    feats = fiche.get("features", [])

    def of(kind):
        return [f.get("properties", {}) for f in feats if str(f.get("id", "")).split(".")[0] == kind]

    def uniq(items):
        seen, out = set(), []
        for it in items:
            k = it.strip().lower()
            if it and k not in seen:
                seen.add(k)
                out.append(it)
        return out

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.4 * cm, bottomMargin=1.4 * cm,
        title=f"Fiche GPU {p.get('nom_com', '')} {p.get('section', '')} {p.get('numero', '')}",
        author="Expert PLU",
    )
    avail_w = A4[0] - 3 * cm

    sub_title = ParagraphStyle("gs", fontSize=10.5, textColor=BLUE, fontName="Helvetica-Bold",
                               alignment=TA_CENTER, leading=14)
    h3 = ParagraphStyle("g3", fontSize=9.5, fontName="Helvetica-Bold", textColor=DARK_BLUE,
                        spaceBefore=7, spaceAfter=3, leading=12)
    h4 = ParagraphStyle("g4", fontSize=8.8, fontName="Helvetica-Bold", textColor=TEXT_MID,
                        spaceBefore=5, spaceAfter=2, leading=11, leftIndent=8)
    item = ParagraphStyle("gi", fontSize=9, textColor=TEXT_DARK, leading=11.5,
                          leftIndent=20, bulletIndent=10, spaceAfter=1.5)
    desc = ParagraphStyle("gd", fontSize=8.3, textColor=TEXT_MID, leading=10.5,
                          leftIndent=20, spaceAfter=3)
    small = ParagraphStyle("gsm", fontSize=8.5, textColor=TEXT_MID, leading=11)
    legend = ParagraphStyle("gl", fontSize=7, textColor=TEXT_LIGHT, alignment=TA_CENTER,
                            fontName="Helvetica-Oblique", spaceBefore=3, spaceAfter=4)
    foot = ParagraphStyle("gf", fontSize=7.2, textColor=TEXT_LIGHT, alignment=TA_CENTER, leading=9.5)

    story = []

    # ── En-tete
    title_tbl = Table([[Paragraph("<b>FICHE INFORMATION DÉTAILLÉE D'UNE PARCELLE</b>",
                                  ParagraphStyle("gt", fontSize=14, fontName="Helvetica-Bold",
                                                 textColor=colors.white, alignment=TA_CENTER,
                                                 leading=18))]],
                      colWidths=[avail_w], rowHeights=[0.95 * cm])
    title_tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), DARK_BLUE),
                                   ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("ROUNDEDCORNERS", [6, 6, 6, 6])]))
    story += [title_tbl, Spacer(1, 0.18 * cm),
              Paragraph(_pdf_txt(f"Commune de {p.get('nom_com', '')} - Section {p.get('section', '')}"
                                 f" - Parcelle {p.get('numero', '')}"), sub_title),
              Spacer(1, 0.3 * cm)]

    # ── References cadastrales
    story.append(_section_header("Références cadastrales", avail_w))
    rows = [("Département", p.get("code_dep", "")), ("Commune", p.get("nom_com", "")),
            ("Code Insee", p.get("code_insee", ""))]
    if str(p.get("code_arr") or "000") != "000":
        rows.append(("Code d'arrondissement", p.get("code_arr")))
    try:
        cont = f"{int(float(p.get('contenance'))):,} m²".replace(",", " ")
    except (TypeError, ValueError):
        cont = "N/D"
    rows += [("Section", p.get("section", "")), ("Feuille", p.get("feuille", "")),
             ("Numéro de parcelle", p.get("numero", "")), ("Contenance", cont)]
    story += [_kv_table([(k, _pdf_txt(v)) for k, v in rows], avail_w), Spacer(1, 0.25 * cm)]

    # ── Carte (Plan IGN + cadastre + contour rouge)
    try:
        map_buf = build_map(pad_bbox(bbox_from_geometry(geom), 3.0), geom)
    except Exception:
        map_buf = None
    if map_buf:
        img_h = min(avail_w * 0.5, 8 * cm)
        mt = Table([[RLImage(map_buf, width=avail_w - 4, height=img_h)]], colWidths=[avail_w])
        mt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1, GREY_LINE),
                                ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                                ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
        story += [mt, Paragraph("© IGN — Plan IGN et parcellaire cadastral, parcelle en rouge", legend)]

    # ── Documents d'urbanisme
    story += [Spacer(1, 0.15 * cm), _section_header("Vue détaillée des documents d'urbanisme", avail_w)]
    docs = of("document")
    for d in docs:
        nom = " — ".join(x for x in [d.get("du_type", ""), d.get("grid_title", "")] if x)
        story.append(Paragraph(_pdf_txt(f"Document : {nom}"), small))
    if not docs:
        story.append(Paragraph("Aucun document d'urbanisme publié sur le Géoportail de l'Urbanisme "
                               "pour cette parcelle.", small))

    zones = of("zone_urba")
    if zones:
        story.append(Paragraph("Zonage(s)", h3))
        for z in zones:
            story.append(Paragraph(_pdf_txt(f"Parcelle classée {z.get('libelle', '')}"), item, bulletText="•"))
            if z.get("libelong"):
                story.append(Paragraph(_pdf_txt(z["libelong"]), desc))

    prescs = of("prescription_surf") + of("prescription_lin") + of("prescription_pct")
    infos = of("info_surf") + of("info_lin") + of("info_pct")
    if prescs or infos:
        story.append(Paragraph("La parcelle est soumise aux dispositions suivantes", h3))
        rest = list(prescs)
        for titre, codes in PRESCRIPTION_GROUPES:
            dans = [x for x in rest if str(x.get("typepsc", "")) in codes]
            rest = [x for x in rest if x not in dans]
            libs = uniq([x.get("libelle", "") for x in dans])
            if libs:
                story.append(Paragraph(_pdf_txt(titre), h4))
                story += [Paragraph(_pdf_txt(lib), item, bulletText="•") for lib in libs]
        libs = uniq([x.get("libelle", "") for x in rest])
        if libs:
            story.append(Paragraph("Autres prescriptions", h4))
            story += [Paragraph(_pdf_txt(lib), item, bulletText="•") for lib in libs]
        libs = uniq([x.get("libelle", "") for x in infos])
        if libs:
            story.append(Paragraph("Périmètres d'informations", h4))
            story += [Paragraph(_pdf_txt(lib), item, bulletText="•") for lib in libs]

    # ── Servitudes d'utilite publique
    sups = of("assiette_sup_s") + of("assiette_sup_l") + of("assiette_sup_p")
    story += [Spacer(1, 0.2 * cm), _section_header("Servitudes d'utilité publique", avail_w)]
    codes = uniq([str(s.get("suptype", "")).upper() for s in sups])
    if codes:
        for c in codes:
            story.append(Paragraph(_pdf_txt(f"{SUP_LIBELLES.get(c, 'Servitude')} ({c})"), item, bulletText="•"))
    else:
        story.append(Paragraph("Aucune servitude d'utilité publique publiée sur cette parcelle.", small))

    # ── SCoT
    scots = uniq([s.get("title", "") for s in of("scot")])
    story += [Spacer(1, 0.2 * cm), _section_header("Schéma de cohérence territoriale", avail_w)]
    if scots:
        story += [Paragraph(_pdf_txt(t), item, bulletText="•") for t in scots]
    else:
        story.append(Paragraph("Aucun SCoT publié pour cette parcelle.", small))

    # ── Pied de page
    url_xml = online_url.replace("&", "&amp;")
    story += [
        Spacer(1, 0.45 * cm),
        HRFlowable(width="100%", thickness=0.8, color=BLUE, spaceAfter=5),
        Paragraph("NB : cette fiche a un caractère informatif et ne peut pas être considérée comme "
                  "un document opposable. Certaines SUP dites « protégées » n'apparaissent pas à "
                  "toutes les échelles.", foot),
        Paragraph(f"Source : Géoportail de l'Urbanisme — <a href=\"{url_xml}\" color=\"#2563eb\">"
                  f"{url_xml}</a>", foot),
        Paragraph(f"Données extraites le {datetime.now().strftime('%d/%m/%Y à %H:%M')}", foot),
    ]
    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/api/fiche-gpu", methods=["POST"])
def api_fiche_gpu():
    body = request.get_json(force=True)
    parcel = body.get("parcel")
    if not parcel:
        return jsonify({"error": "Donnees de parcelle manquantes"}), 400
    props = parcel.get("properties", {})
    pid = gpu_parcel_id(props)
    try:
        fiche = get_fiche_gpu(pid)
    except requests.exceptions.Timeout:
        return jsonify({"error": "Le Géoportail de l'Urbanisme ne répond pas, réessayez"}), 504
    except Exception as e:
        logger.warning("fiche GPU %s: %s", pid, e)
        fiche = None
    if not fiche:
        return jsonify({"error": "Fiche indisponible sur le Géoportail de l'Urbanisme "
                                 "pour cette parcelle"}), 502
    pdf = make_fiche_gpu_pdf(fiche, f"{GPU_SITE}/map/parcel-info/{pid}/")
    fname = f"fiche_gpu_{props.get('code_insee', '')}_{props.get('section', '')}_{props.get('numero', '')}.pdf"
    return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=fname)


@app.route("/")
def index():
    return render_template("index.html")


# ── Autocompletion BAN ────────────────────────────────────────────────────────

@app.route("/api/autocomplete")
def api_autocomplete():
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify([])
    try:
        features = ban_autocomplete(q, limit=6)
        results = []
        for f in features:
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            results.append({
                "label": p.get("label", ""),
                "city": p.get("city", ""),
                "postcode": p.get("postcode", ""),
                "lon": lon,
                "lat": lat,
            })
        return jsonify(results)
    except Exception:
        return jsonify([])


# ── Recherche principale ──────────────────────────────────────────────────────

@app.route("/api/search", methods=["POST"])
def api_search():
    body        = request.get_json(force=True)
    search_type = body.get("type", "address")

    try:
        if search_type == "address":
            # Si lon/lat fournis directement (selection autocomplete)
            lon = body.get("lon")
            lat = body.get("lat")
            addr_props = body.get("address_props")

            if lon is None or lat is None:
                addr = body.get("address", "").strip()
                if not addr:
                    return jsonify({"error": "Adresse requise"}), 400
                lon, lat, addr_props = geocode(addr)
                if lon is None:
                    return jsonify({"error": "Adresse introuvable"}), 404

            data = parcel_by_coords(lon, lat)
            if not data.get("features"):
                # Adresse geocodee sur la voirie : repli sur la parcelle la plus proche
                feat, dist = nearest_parcel(float(lon), float(lat))
                if not feat:
                    return jsonify({"error": "Aucune parcelle cadastrale a cette position"}), 404
                return jsonify({
                    "parcel": feat,
                    "address": addr_props,
                    "lon": lon, "lat": lat,
                    "approx": True,
                    "distance_m": round(dist, 1),
                })

            return jsonify({
                "parcel": data["features"][0],
                "address": addr_props,
                "lon": lon, "lat": lat,
            })

        elif search_type == "reference":
            # Nouveau format : code_insee + section_numero separes
            code_insee = body.get("code_insee", "").strip()
            section_numero = body.get("section_numero", "").strip()

            if code_insee and section_numero:
                # Parse section + numero (ex: "AB 124", "A 42", "AB0001")
                sn = section_numero.upper().replace(" ", "").replace("-", "")
                m = re.match(r"^([A-Z]{1,2})(\d{1,4})$", sn)
                if not m:
                    return jsonify({
                        "error": "Format invalide — ex : AB 124 ou A 42"
                    }), 400
                sec = m.group(1)
                num = m.group(2).zfill(4)
                data = parcel_by_ref(code_insee, sec, num)
            else:
                # Ancien format : reference complete (retrocompatibilite)
                ref = body.get("reference", "").strip()
                if not ref:
                    return jsonify({"error": "Reference cadastrale requise"}), 400
                code_insee, sec, num = parse_ref(ref)
                if not code_insee:
                    return jsonify({
                        "error": "Format invalide — ex : 69123AB0001"
                    }), 400
                data = parcel_by_ref(code_insee, sec, num)

            if not data.get("features"):
                return jsonify({"error": "Parcelle introuvable"}), 404

            parcel = data["features"][0]
            geom = parcel.get("geometry", {})
            clon, clat = centroid_from_geometry(geom)

            return jsonify({
                "parcel": parcel,
                "lon": clon, "lat": clat,
            })

        elif search_type == "coords":
            # Clic sur carte
            lon = body.get("lon")
            lat = body.get("lat")
            if lon is None or lat is None:
                return jsonify({"error": "Coordonnees requises"}), 400

            data = parcel_by_coords(float(lon), float(lat))
            if not data.get("features"):
                return jsonify({"error": "Aucune parcelle a cette position"}), 404

            return jsonify({
                "parcel": data["features"][0],
                "lon": lon, "lat": lat,
            })

        return jsonify({"error": "Type de recherche inconnu"}), 400

    except requests.exceptions.Timeout:
        return jsonify({"error": "Delai d'attente depasse, reessayez"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Altitude ──────────────────────────────────────────────────────────────────

@app.route("/api/elevation")
def api_elevation():
    lon = request.args.get("lon", type=float)
    lat = request.args.get("lat", type=float)
    if lon is None or lat is None:
        return jsonify({"error": "lon/lat requis"}), 400
    elev = get_elevation(lon, lat)
    if elev:
        return jsonify({"elevation": elev.get("z"), "accuracy": elev.get("acc")})
    return jsonify({"elevation": None})


# ── Zonage PLU ────────────────────────────────────────────────────────────────

@app.route("/api/zonage")
def api_zonage():
    lon = request.args.get("lon", type=float)
    lat = request.args.get("lat", type=float)
    if lon is None or lat is None:
        return jsonify({"error": "lon/lat requis"}), 400
    try:
        zones = get_zonage(lon, lat)
        result = []
        for z in zones:
            p = z.get("properties", {})
            result.append({
                "typezone": p.get("typezone", ""),
                "libelle": p.get("libelle", p.get("libelong", "")),
                "destdomi": p.get("destdomi", ""),
                "nomfic": p.get("nomfic", ""),
                "urlfic": p.get("urlfic", ""),
                "datvalid": p.get("datvalid", ""),
                "partition": p.get("partition", ""),
            })
        return jsonify({"zones": result})
    except Exception as e:
        return jsonify({"zones": [], "error": str(e)})


# ── Document PLU ──────────────────────────────────────────────────────────────

@app.route("/api/plu-document")
def api_plu_document():
    lon = request.args.get("lon", type=float)
    lat = request.args.get("lat", type=float)
    if lon is None or lat is None:
        return jsonify({"error": "lon/lat requis"}), 400
    try:
        docs = get_plu_document(lon, lat)
        result = []
        for d in docs:
            p = d.get("properties", {})
            result.append({
                # Champs reels renvoyes par l'API GPU /document
                "type": p.get("du_type", p.get("typedoc", "")),
                "titre": p.get("grid_title", ""),
                "nom": p.get("name", ""),
                "statut": p.get("gpu_status", ""),
                "reference": p.get("grid_name", p.get("partition", "")),
                "date": (p.get("gpu_timestamp", "") or "")[:10],
                # Compat ascendante
                "typedoc": p.get("du_type", ""),
                "idurba": p.get("grid_name", ""),
            })
        return jsonify({"documents": result})
    except Exception as e:
        return jsonify({"documents": [], "error": str(e)})


# ── Patrimoine / ABF ─────────────────────────────────────────────────────────

@app.route("/api/patrimoine")
def api_patrimoine():
    lon = request.args.get("lon", type=float)
    lat = request.args.get("lat", type=float)
    if lon is None or lat is None:
        return jsonify({"error": "lon/lat requis"}), 400
    try:
        prescs = get_prescriptions(lon, lat)
        # Filtrer les prescriptions liees au patrimoine (types AC)
        patrimoine = []
        for p in prescs:
            props = p.get("properties", {})
            typepsc = props.get("typepsc", "")
            # AC = patrimoine/architecture : AC1-AC4
            if typepsc.startswith("AC") or typepsc.startswith("05"):
                patrimoine.append({
                    "typepsc": typepsc,
                    "libelle": props.get("libelle", props.get("txt", "")),
                    "nomfic": props.get("nomfic", ""),
                    "urlfic": props.get("urlfic", ""),
                })
        # Renvoyer aussi toutes les prescriptions pour reference
        all_prescs = []
        for p in prescs:
            props = p.get("properties", {})
            all_prescs.append({
                "typepsc": props.get("typepsc", ""),
                "libelle": props.get("libelle", props.get("txt", "")),
            })
        return jsonify({
            "patrimoine": patrimoine,
            "all_prescriptions": all_prescs,
            "is_abf": len(patrimoine) > 0,
        })
    except Exception as e:
        return jsonify({"patrimoine": [], "all_prescriptions": [], "is_abf": False, "error": str(e)})


# ── Georisques ────────────────────────────────────────────────────────────────

@app.route("/api/risques")
def api_risques():
    lon = request.args.get("lon", type=float)
    lat = request.args.get("lat", type=float)
    code_insee = request.args.get("code_insee", "")
    if lon is None or lat is None:
        return jsonify({"error": "lon/lat requis"}), 400

    risques_commune = []
    if code_insee:
        risques_commune = get_risques_commune(code_insee)

    # Liste propre des risques par famille
    risques = flatten_risques(risques_commune)

    # Lien Georisques : page principale d'etat des risques
    georisques_url = georisques_rapport_url(lon, lat, code_insee, request.args.get("adresse", ""))

    return jsonify({
        "risques": risques,
        "errial_url": georisques_url,
    })


# ── Extrait de plan officiel cadastre.gouv.fr ────────────────────────────────

@app.route("/api/extrait-cadastral", methods=["POST"])
def api_extrait_cadastral():
    body   = request.get_json(force=True)
    parcel = body.get("parcel")
    if not parcel:
        return jsonify({"error": "Donnees de parcelle manquantes"}), 400

    props = parcel.get("properties", {})
    geom  = parcel.get("geometry", {})
    code_insee = props.get("code_insee", "")
    section    = props.get("section", "")
    numero     = props.get("numero", "")
    prefixe    = props.get("com_abs") or "000"
    ville      = props.get("nom_com", "")
    if not (code_insee and section and numero):
        return jsonify({"error": "Reference cadastrale incomplete"}), 400

    # Paris/Lyon/Marseille : cadastre.gouv.fr travaille par arrondissement
    # (ex : Lyon 2e = 69382), fourni par l'IGN dans code_arr.
    code_arr = str(props.get("code_arr") or "").strip()
    if code_arr and code_arr != "000":
        code_insee = code_insee[:2] + code_arr.zfill(3)

    # Taille de la parcelle en metres (pour adapter l'echelle de l'extrait)
    parcel_size_m = 0.0
    try:
        b = bbox_from_geometry(geom)
        clat = (b[1] + b[3]) / 2
        parcel_size_m = max(
            (b[2] - b[0]) * 111320.0 * math.cos(math.radians(clat)),
            (b[3] - b[1]) * 110540.0,
        )
    except Exception:
        pass

    try:
        pdf = get_extrait_cadastral(code_insee, section, numero,
                                    prefixe=prefixe, ville=ville,
                                    parcel_size_m=parcel_size_m, geom=geom)
    except requests.exceptions.Timeout:
        return jsonify({"error": "cadastre.gouv.fr ne repond pas, reessayez"}), 504
    except Exception as e:
        logger.warning("extrait cadastral: %s", e)
        pdf = None

    if not pdf:
        return jsonify({"error": "Extrait indisponible pour cette parcelle "
                                 "sur cadastre.gouv.fr"}), 502

    fname = f"extrait_cadastre_{code_insee}_{section}_{numero}.pdf"
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=True, download_name=fname)


# ── PDF cadastral enrichi ────────────────────────────────────────────────────

@app.route("/api/pdf", methods=["POST"])
def api_pdf():
    body      = request.get_json(force=True)
    parcel    = body.get("parcel")
    addr      = body.get("address")

    if not parcel:
        return jsonify({"error": "Donnees de parcelle manquantes"}), 400

    try:
        # Recuperer les donnees enrichies
        geom = parcel.get("geometry", {})
        clon, clat = centroid_from_geometry(geom)
        code_insee = parcel.get("properties", {}).get("code_insee", "")

        def _safe(fn, *args, default=None):
            try:
                return fn(*args)
            except Exception:
                return default

        # Tous les appels externes en parallele (gain majeur sur Render free tier)
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_elev = ex.submit(_safe, get_elevation, clon, clat)
            f_zon = ex.submit(_safe, get_zonage, clon, clat, default=[])
            f_doc = ex.submit(_safe, get_plu_document, clon, clat, default=[])
            f_presc = ex.submit(_safe, get_prescriptions, clon, clat, default=[])

            elev_data = f_elev.result()
            zonage_list = f_zon.result() or []
            plu_docs_list = f_doc.result() or []
            all_prescs_list = f_presc.result() or []

        elevation = elev_data.get("z") if elev_data else None
        patrimoine_list = [
            p for p in all_prescs_list
            if p.get("properties", {}).get("typepsc", "").startswith("AC")
            or p.get("properties", {}).get("typepsc", "").startswith("05")
        ]

        _pp = parcel.get("properties", {})
        _label = (addr or {}).get("label") or f"{_pp.get('nom_com', '')} {_pp.get('section', '')}{_pp.get('numero', '')}".strip()
        errial_url = georisques_rapport_url(clon, clat, code_insee, _label)

        pdf = make_pdf(
            parcel, addr,
            zonage_info=zonage_list,
            plu_docs=plu_docs_list,
            patrimoine_info=patrimoine_list,
            all_prescriptions=all_prescs_list,
            elevation=elevation,
            errial_url=errial_url,
        )
        p     = parcel.get("properties", {})
        fname = f"fiche_{p.get('code_insee','')}_{p.get('section','')}_{p.get('numero','')}.pdf"
        return send_file(pdf, mimetype="application/pdf",
                         as_attachment=True, download_name=fname)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, port=5001)
