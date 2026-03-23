"""
CAdastre — Application PropTech d'analyse de parcelles foncieres
APIs : BAN, API Carto (Cadastre + GPU), IGN Geoplateforme, Georisques
"""

from __future__ import annotations

import io
import re
import sys
import json
import base64
import logging
from datetime import datetime

logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)

import requests
from flask import Flask, render_template, request, send_file, jsonify
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB for map image uploads
logger.info("Flask app created successfully")

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


def build_map(bbox: list, width=800, height=600) -> io.BytesIO | None:
    """Carte 2D : fond neutre + parcelles cadastrales."""
    parcels = wms_image(bbox, "CADASTRALPARCELS.PARCELLAIRE_EXPRESS", width, height, transparent=True)
    if not parcels:
        return None
    # Fond blanc + overlay cadastral = carte 2D propre
    base = PILImage.new("RGBA", (width, height), (255, 255, 255, 255))
    overlay = PILImage.open(io.BytesIO(parcels)).convert("RGBA")
    combined = PILImage.alpha_composite(base, overlay).convert("RGB")
    buf = io.BytesIO()
    combined.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
#  PDF GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

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


def make_pdf(parcel: dict, address_props: dict | None = None,
             zonage_info: list | None = None,
             plu_docs: list | None = None,
             patrimoine_info: list | None = None,
             all_prescriptions: list | None = None,
             risques_info: list | None = None,
             elevation: float | None = None,
             errial_url: str | None = None,
             map_image_data: str | None = None) -> io.BytesIO:
    props = parcel.get("properties", {})
    geom  = parcel.get("geometry", {})

    code_insee = props.get("code_insee", "")
    section    = props.get("section", "")
    numero     = props.get("numero", "")
    contenance = props.get("contenance")

    city   = props.get("nom_com", "") or commune_name(code_insee) or code_insee
    bbox   = bbox_from_geometry(geom)
    clon, clat = centroid_from_geometry(geom)

    # Use client-captured map image if available, otherwise fallback to WMS
    map_buf = None
    if map_image_data:
        try:
            # Strip data:image/...;base64, prefix
            if "," in map_image_data:
                map_image_data = map_image_data.split(",", 1)[1]
            raw = base64.b64decode(map_image_data)
            img = PILImage.open(io.BytesIO(raw)).convert("RGB")
            map_buf = io.BytesIO()
            img.save(map_buf, format="JPEG", quality=92)
            map_buf.seek(0)
        except Exception:
            map_buf = None
    if map_buf is None:
        padded = pad_bbox(bbox, 3.0)
        map_buf = build_map(padded)

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
            [[Image(map_buf, width=avail_w - 4, height=img_h)]],
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
            typedoc = dp.get("typedoc", "?")
            idurba = dp.get("idurba", "")
            etat = dp.get("etat", "")
            datappro = dp.get("datappro", "")
            doc_rows.append(("Type", typedoc))
            if idurba:
                doc_rows.append(("Identifiant", idurba))
            if etat:
                doc_rows.append(("Etat", etat))
            if datappro:
                doc_rows.append(("Date d'approbation", datappro))
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

    # ── Risques reglementaires
    story += [Spacer(1, 0.2 * cm), _section_header("Risques reglementaires", avail_w)]
    if risques_info:
        risk_rows = []
        for risque in risques_info:
            lib = risque.get("libelle_risque_jo", risque.get("libelle", ""))
            if lib:
                risk_rows.append(("Risque", lib))
        if risk_rows:
            story.append(_kv_table(risk_rows, avail_w))
        else:
            story.append(Paragraph("Aucun risque reglementaire recense.", small))
    else:
        ok_tbl = Table(
            [[Paragraph("Aucun risque reglementaire recense",
                         ParagraphStyle("ok2", fontSize=9, textColor=GREEN_TXT, leading=12))]],
            colWidths=[avail_w],
        )
        ok_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), GREEN_BG),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(ok_tbl)

    # ── Liens utiles
    story += [Spacer(1, 0.2 * cm), _section_header("Liens utiles", avail_w)]
    links_rows = []
    if errial_url:
        links_rows.append(("Etat des risques (ERRIAL)", errial_url))
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
        link_data.append([
            Paragraph(k, link_key_style),
            Paragraph(f'<a href="{v}" color="#2563eb">{v[:80]}{"..." if len(v) > 80 else ""}</a>', link_val_style),
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
                return jsonify({"error": "Aucune parcelle cadastrale a cette position"}), 404

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
                "idurba": p.get("idurba", ""),
                "typedoc": p.get("typedoc", ""),
                "etat": p.get("etat", ""),
                "nomplan": p.get("nomplan", ""),
                "urlplan": p.get("urlplan", ""),
                "nomreg": p.get("nomreg", ""),
                "urlreg": p.get("urlreg", ""),
                "datappro": p.get("datappro", ""),
                "datefin": p.get("datefin", ""),
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

    # Lien ERRIAL
    errial_url = (
        f"https://errial.georisques.gouv.fr/#/?lon={lon}&lat={lat}"
    )
    # Lien rapport Georisques
    georisques_url = (
        f"https://www.georisques.gouv.fr/mes-risques/connaitre-les-risques-pres-de-chez-moi"
    )

    return jsonify({
        "risques": risques_commune,
        "errial_url": errial_url,
        "georisques_url": georisques_url,
    })


# ── PDF cadastral enrichi ────────────────────────────────────────────────────

@app.route("/api/pdf", methods=["POST"])
def api_pdf():
    body      = request.get_json(force=True)
    parcel    = body.get("parcel")
    addr      = body.get("address")
    map_image = body.get("map_image")  # base64 data-URL from client

    if not parcel:
        return jsonify({"error": "Donnees de parcelle manquantes"}), 400

    try:
        # Recuperer les donnees enrichies
        geom = parcel.get("geometry", {})
        clon, clat = centroid_from_geometry(geom)
        code_insee = parcel.get("properties", {}).get("code_insee", "")

        elev_data = get_elevation(clon, clat)
        elevation = elev_data.get("z") if elev_data else None

        zonage_list = []
        plu_docs_list = []
        patrimoine_list = []
        all_prescs_list = []
        risques_list = []
        try:
            zonage_list = get_zonage(clon, clat)
        except Exception:
            pass
        try:
            plu_docs_list = get_plu_document(clon, clat)
        except Exception:
            pass
        try:
            prescs = get_prescriptions(clon, clat)
            all_prescs_list = prescs
            patrimoine_list = [
                p for p in prescs
                if p.get("properties", {}).get("typepsc", "").startswith("AC")
                or p.get("properties", {}).get("typepsc", "").startswith("05")
            ]
        except Exception:
            pass
        try:
            if code_insee:
                risques_list = get_risques_commune(code_insee)
        except Exception:
            pass

        errial_url = f"https://errial.georisques.gouv.fr/#/?lon={clon}&lat={clat}"

        pdf = make_pdf(
            parcel, addr,
            zonage_info=zonage_list,
            plu_docs=plu_docs_list,
            patrimoine_info=patrimoine_list,
            all_prescriptions=all_prescs_list,
            risques_info=risques_list,
            elevation=elevation,
            errial_url=errial_url,
            map_image_data=map_image,
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
