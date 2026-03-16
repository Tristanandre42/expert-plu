"""
CAdastre — Application PropTech d'analyse de parcelles foncieres
APIs : BAN, API Carto (Cadastre + GPU), IGN Geoplateforme, Georisques
"""

import io
import re
import json
from datetime import datetime

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
    plan    = wms_image(bbox, "GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", width, height, transparent=False)
    parcels = wms_image(bbox, "CADASTRALPARCELS.PARCELLAIRE_EXPRESS", width, height, transparent=True)
    if plan and parcels:
        base    = PILImage.open(io.BytesIO(plan)).convert("RGBA")
        overlay = PILImage.open(io.BytesIO(parcels)).convert("RGBA")
        combined = PILImage.alpha_composite(base, overlay).convert("RGB")
    elif plan:
        combined = PILImage.open(io.BytesIO(plan)).convert("RGB")
    elif parcels:
        combined = PILImage.open(io.BytesIO(parcels)).convert("RGB")
    else:
        return None
    buf = io.BytesIO()
    combined.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
#  PDF GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

BLUE       = colors.HexColor("#1a3a6b")
LIGHT_BLUE = colors.HexColor("#e8edf8")
GREY_LINE  = colors.HexColor("#cccccc")


def make_pdf(parcel: dict, address_props: dict | None = None,
             zonage_info: list | None = None,
             plu_docs: list | None = None,
             patrimoine_info: list | None = None,
             all_prescriptions: list | None = None,
             risques_info: list | None = None,
             elevation: float | None = None,
             errial_url: str | None = None) -> io.BytesIO:
    props = parcel.get("properties", {})
    geom  = parcel.get("geometry", {})

    code_insee = props.get("code_insee", "")
    section    = props.get("section", "")
    numero     = props.get("numero", "")
    contenance = props.get("contenance")

    city   = props.get("nom_com", "") or commune_name(code_insee) or code_insee
    bbox   = bbox_from_geometry(geom)
    clon, clat = centroid_from_geometry(geom)
    padded = pad_bbox(bbox, 3.0)
    map_buf = build_map(padded)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=f"Parcelle cadastrale {code_insee} {section} {numero}",
        author="CAdastre",
    )

    h1 = ParagraphStyle("h1", fontSize=20, textColor=BLUE,
                         fontName="Helvetica-Bold", spaceAfter=4, alignment=TA_CENTER)
    sub = ParagraphStyle("sub", fontSize=11, textColor=colors.HexColor("#555555"),
                         spaceAfter=10, alignment=TA_CENTER)
    h2 = ParagraphStyle("h2", fontSize=13, textColor=BLUE,
                         fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=6)
    h3 = ParagraphStyle("h3", fontSize=10, textColor=BLUE,
                         fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4)
    legend = ParagraphStyle("legend", fontSize=7.5, textColor=colors.HexColor("#555555"),
                            alignment=TA_CENTER, fontName="Helvetica-Oblique")
    foot = ParagraphStyle("foot", fontSize=7, textColor=colors.HexColor("#999999"),
                          alignment=TA_CENTER)
    normal = ParagraphStyle("norm", fontSize=9, spaceAfter=3)
    small = ParagraphStyle("small", fontSize=8, textColor=colors.HexColor("#555555"),
                           spaceAfter=2)
    link_style = ParagraphStyle("link", fontSize=8, textColor=colors.HexColor("#2563eb"),
                                spaceAfter=2)

    if contenance:
        area_str = f"{int(contenance):,} m\u00b2  ({contenance / 10_000:.4f} ha)".replace(",", "\u202f")
    else:
        area_str = "N/A"

    # ── Main info table
    headers = ["Reference cadastrale", "Commune (INSEE)", "Section", "Parcelle", "Contenance"]
    values  = [
        f"{code_insee} {section} {numero}",
        f"{city}\n({code_insee})" if city else code_insee,
        section, numero, area_str,
    ]
    if address_props:
        headers.insert(0, "Adresse")
        values.insert(0, address_props.get("label", ""))

    n_cols = len(headers)
    col_w  = (A4[0] - 3 * cm) / n_cols
    tbl = Table([headers, values], colWidths=[col_w] * n_cols)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND",    (0, 1), (-1, 1), LIGHT_BLUE),
        ("GRID",          (0, 0), (-1, -1), 0.5, GREY_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    # ── GPS table
    gps_headers = ["Latitude", "Longitude", "Altitude"]
    alt_str = f"{elevation:.1f} m" if elevation is not None else "N/D"
    gps_values = [f"{clat:.6f}", f"{clon:.6f}", alt_str]
    gps_col_w = (A4[0] - 3 * cm) / 3
    tbl_gps = Table([gps_headers, gps_values], colWidths=[gps_col_w] * 3)
    tbl_gps.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND",    (0, 1), (-1, 1), LIGHT_BLUE),
        ("GRID",          (0, 0), (-1, -1), 0.5, GREY_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    # ── Story
    story = [
        Paragraph("FICHE PARCELLE", h1),
        Paragraph(f"Analyse fonciere — {city}", sub),
        HRFlowable(width="100%", thickness=2, color=BLUE, spaceAfter=8),
        tbl,
        Spacer(1, 0.2 * cm),
        tbl_gps,
        Spacer(1, 0.3 * cm),
    ]

    # Map image
    if map_buf:
        avail_w = A4[0] - 3 * cm
        img_h   = min(avail_w * 0.65, A4[1] - 16 * cm)
        story += [
            Image(map_buf, width=avail_w, height=img_h),
            Spacer(1, 0.15 * cm),
            Paragraph("Plan cadastral — Source : IGN Geoplateforme / DGFiP — donnees indicatives", legend),
        ]

    # ── Document d'urbanisme (PLU)
    story.append(Paragraph("Document d'urbanisme", h2))
    if plu_docs:
        for d in plu_docs:
            dp = d.get("properties", {})
            typedoc = dp.get("typedoc", "?")
            etat = dp.get("etat", "")
            idurba = dp.get("idurba", "")
            datappro = dp.get("datappro", "")
            datefin = dp.get("datefin", "")
            story.append(Paragraph(f"<b>{typedoc}</b> — {idurba}", normal))
            if etat:
                story.append(Paragraph(f"Etat : {etat}", small))
            if datappro:
                story.append(Paragraph(f"Date d'approbation : {datappro}", small))
            if datefin:
                story.append(Paragraph(f"Date de fin : {datefin}", small))
    else:
        story.append(Paragraph("Aucun document d'urbanisme trouve.", small))

    # ── Zonage PLU
    story.append(Paragraph("Zonage PLU", h2))
    if zonage_info:
        for z in zonage_info:
            zp = z.get("properties", {})
            zone_type = zp.get("typezone", "?")
            libelle = zp.get("libelle", zp.get("libelong", ""))
            dest = zp.get("destdomi", "")
            datvalid = zp.get("datvalid", "")
            nomfic = zp.get("nomfic", "")
            line = f"<b>{zone_type}</b>"
            if libelle:
                line += f" — {libelle}"
            if dest:
                line += f" (destination : {dest})"
            story.append(Paragraph(line, normal))
            if datvalid:
                story.append(Paragraph(f"Date de validite : {datvalid}", small))
            if nomfic:
                story.append(Paragraph(f"Reglement : {nomfic}", small))
    else:
        story.append(Paragraph("Aucun zonage PLU trouve pour cette parcelle.", small))

    # ── Patrimoine / ABF
    story.append(Paragraph("Secteurs proteges (ABF / Patrimoine)", h2))
    if patrimoine_info:
        for p_feat in patrimoine_info:
            pp = p_feat.get("properties", {})
            lib = pp.get("libelle", pp.get("txt", "Prescription surfacique"))
            typep = pp.get("typepsc", "")
            story.append(Paragraph(f"<b>{typep}</b> — {lib}", normal))
    else:
        story.append(Paragraph("Aucun perimetre de protection ABF identifie.", small))

    # ── Prescriptions surfaciques (toutes)
    non_abf = []
    if all_prescriptions:
        for p in all_prescriptions:
            pp = p.get("properties", {})
            typepsc = pp.get("typepsc", "")
            if not typepsc.startswith("AC") and not typepsc.startswith("05"):
                non_abf.append(pp)
    if non_abf:
        story.append(Paragraph("Autres prescriptions surfaciques", h3))
        for pp in non_abf:
            typep = pp.get("typepsc", "")
            lib = pp.get("libelle", pp.get("txt", ""))
            story.append(Paragraph(f"<b>{typep}</b> — {lib}", normal))

    # ── Risques reglementaires
    story.append(Paragraph("Risques reglementaires", h2))
    if risques_info:
        for risque in risques_info:
            lib = risque.get("libelle_risque_jo", risque.get("libelle", ""))
            if lib:
                story.append(Paragraph(f"- {lib}", normal))
    else:
        story.append(Paragraph("Aucun risque reglementaire recense.", small))

    # ── Liens utiles
    story.append(Paragraph("Liens utiles", h2))
    if errial_url:
        story.append(Paragraph(
            f'Etat des risques (ERRIAL) : <a href="{errial_url}" color="#2563eb">{errial_url}</a>',
            small))
    atlas_bbox = bbox_from_geometry(geom)
    atlas_pad = pad_bbox(atlas_bbox, 1.5)
    atlas_url = (
        f"http://atlas.patrimoines.culture.fr/atlas/trunk/index.php"
        f"?ap_theme=DOMREG&ap_bbox={atlas_pad[0]:.6f}%3B{atlas_pad[1]:.6f}"
        f"%3B{atlas_pad[2]:.6f}%3B{atlas_pad[3]:.6f}"
    )
    story.append(Paragraph(
        f'Atlas des Patrimoines : <a href="{atlas_url}" color="#2563eb">Voir la parcelle</a>',
        small))
    gpu_url = f"https://www.geoportail-urbanisme.gouv.fr/map/#tile=1&lon={clon}&lat={clat}&zoom=19"
    story.append(Paragraph(
        f'Geoportail de l\'Urbanisme : <a href="{gpu_url}" color="#2563eb">Carte PLU</a>',
        small))
    geo_url = f"https://www.geoportail.gouv.fr/carte?c={clon},{clat}&z=17&permalink=yes"
    story.append(Paragraph(
        f'Geoportail IGN : <a href="{geo_url}" color="#2563eb">Carte IGN</a>',
        small))

    # Footer
    story += [
        Spacer(1, 0.5 * cm),
        HRFlowable(width="100%", thickness=0.5, color=GREY_LINE, spaceAfter=4),
        Paragraph(
            f"Document genere le {datetime.now().strftime('%d/%m/%Y a %H:%M')}  ·  "
            "Sources : IGN / DGFiP / GPU / Georisques  ·  "
            "Ce document n'a aucune valeur juridique",
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
    body   = request.get_json(force=True)
    parcel = body.get("parcel")
    addr   = body.get("address")

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
