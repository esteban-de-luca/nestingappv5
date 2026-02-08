import io
import csv
import zipfile
import unicodedata
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Tuple

import pandas as pd
import streamlit as st

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Google Drive
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# =========================================================
# CUBRO - Quick Nesting v5 (Drive dropdown + manual upload)
# =========================================================
APP_TITLE = "CUBRO - Quick Nesting v5"
SIDEBAR_LOGO_PATH = Path("assets/logo.png")

GAP_BETWEEN = 15  # mm separación obligatoria entre piezas
EDGE_MARGIN = 7   # mm separación obligatoria a borde de tablero (mínimo)

BOARD_RULES = {
    "wood": {"board_w": 1250, "board_h": 3050, "rotate": False},
    "laminado": {"board_w": 1300, "board_h": 3050, "rotate": True},
    "linoleo": {"board_w": 1300, "board_h": 3050, "rotate": True},
    "laca": {"board_w": 1220, "board_h": 2750, "rotate": True},
}

GAMA_SYNONYMS = {
    "lac": "laca",
    "woo": "wood",
    "lin": "linoleo",
    "lam": "laminado",
    "laca": "laca",
    "wood": "wood",
    "madera": "wood",
    "linoleo": "linoleo",
    "linóleo": "linoleo",
    "laminado": "laminado",
}

GAMA_DISPLAY = {
    "laca": "Laca",
    "wood": "Wood",
    "linoleo": "Linóleo",
    "laminado": "Laminado",
}

PREVIEW_WIDTH_PRESETS = {
    "XS (muy pequeño)": 220,
    "S (pequeño)": 280,
    "M (medio)": 340,
}
DEFAULT_PREVIEW_PRESET = "S (pequeño)"


# =========================================================
# Drive helpers
# =========================================================
def get_drive_service():
    if "gdrive_sa" not in st.secrets:
        raise ValueError("Falta la sección [gdrive_sa] en Secrets.")

    sa_info = dict(st.secrets["gdrive_sa"])

    pk = sa_info.get("private_key", "")
    if not isinstance(pk, str) or not pk.strip():
        raise ValueError("private_key vacío o inválido en Secrets [gdrive_sa].")

    pk = pk.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    pk = pk.strip()
    pk = "\n".join(line.strip() for line in pk.split("\n"))

    if "BEGIN PRIVATE KEY" not in pk or "END PRIVATE KEY" not in pk:
        raise ValueError("private_key no contiene delimitadores BEGIN/END.")

    sa_info["private_key"] = pk

    required = ["type", "client_email", "token_uri", "private_key"]
    missing = [k for k in required if k not in sa_info or not sa_info[k]]
    if missing:
        raise ValueError(f"Secrets [gdrive_sa] incompleto. Faltan: {missing}")

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@st.cache_data(ttl=60, show_spinner=False)
def list_csv_files_in_folder(folder_id: str) -> List[dict]:
    service = get_drive_service()
    q = f"'{folder_id}' in parents and mimeType='text/csv' and trashed=false"
    resp = service.files().list(
        q=q,
        fields="files(id,name,modifiedTime,size)",
        orderBy="modifiedTime desc",
        pageSize=200
    ).execute()
    return resp.get("files", [])


def download_drive_file_as_bytes(file_id: str) -> bytes:
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


class BytesUploadedFile:
    def __init__(self, data: bytes, name: str = "drive.csv"):
        self._data = data
        self.name = name

    def getvalue(self):
        return self._data


# =========================================================
# Normalización
# =========================================================
def _norm_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s


def normalize_gama(gama: str) -> str:
    g = _norm_text(gama)
    return GAMA_SYNONYMS.get(g, g)


def normalize_acabado(acabado: str) -> str:
    return _norm_text(acabado)


def normalize_material(material: str) -> str:
    m = _norm_text(material).upper()
    if m in ["MDF", "PLY"]:
        return m
    return m if m else "—"


def get_board_rule(gama_norm: str, acabado_norm: str):
    g = (gama_norm or "").strip().lower()
    a = (acabado_norm or "").strip().lower()

    if g not in BOARD_RULES:
        return None

    rule = BOARD_RULES[g].copy()

    # Excepción: Laminado + Metal => 1220x3050 (rotación sí)
    if g == "laminado" and a == "metal":
        rule["board_w"] = 1220
        rule["board_h"] = 3050
        rule["rotate"] = True

    return rule


def auto_preview_cols(preview_width_px: int) -> int:
    if preview_width_px >= 360:
        return 2
    if preview_width_px >= 300:
        return 3
    return 4


# =========================================================
# CSV robusto
# =========================================================
def read_csv_robust(uploaded_file) -> pd.DataFrame:
    raw = uploaded_file.getvalue()
    if raw is None or len(raw) == 0:
        raise ValueError("El archivo está vacío (0 bytes).")

    text = None
    for enc in ["utf-8-sig", "utf-8", "latin-1"]:
        try:
            text = raw.decode(enc)
            break
        except Exception:
            pass
    if text is None:
        raise ValueError("No pude decodificar el archivo. Prueba guardarlo como CSV UTF-8.")

    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not lines:
        raise ValueError("El archivo solo contiene líneas vacías.")

    sample = "\n".join(lines[:60])

    sep_candidates = []
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        sep_candidates.append(dialect.delimiter)
    except Exception:
        pass
    for s in [";", ",", "\t"]:
        if s not in sep_candidates:
            sep_candidates.append(s)

    last_err = None
    for sep in sep_candidates:
        try:
            df = pd.read_csv(
                io.StringIO("\n".join(lines)),
                sep=sep,
                dtype=str,
                engine="python",
                keep_default_na=False,
            )
            if df.shape[1] <= 1:
                continue
            return df
        except Exception as e:
            last_err = e

    raise ValueError(f"No pude interpretar el CSV con separadores ; , o tab. Error: {last_err}")


def to_float_mm(x):
    x = str(x).strip().replace(",", ".")
    try:
        return float(x)
    except Exception:
        return None


def load_pieces_v5(uploaded_file) -> pd.DataFrame:
    df = read_csv_robust(uploaded_file)

    if df.shape[1] < 9:
        raise ValueError(f"El CSV tiene {df.shape[1]} columnas. Se esperan al menos 9 (A..I).")

    out = pd.DataFrame(
        {
            "ProjectID": df.iloc[:, 0],
            "PieceID": df.iloc[:, 2] if df.shape[1] > 2 else "",
            "Typology": df.iloc[:, 3] if df.shape[1] > 3 else "",
            "W": df.iloc[:, 4] if df.shape[1] > 4 else "",
            "H": df.iloc[:, 5] if df.shape[1] > 5 else "",
            "Material": df.iloc[:, 6] if df.shape[1] > 6 else "",
            "Gama": df.iloc[:, 7] if df.shape[1] > 7 else "",
            "Acabado": df.iloc[:, 8] if df.shape[1] > 8 else "",
            "Machining": df.iloc[:, 9] if df.shape[1] > 9 else "",
            "HandleModel": df.iloc[:, 10] if df.shape[1] > 10 else "",
            "HandlePos": df.iloc[:, 11] if df.shape[1] > 11 else "",
            "DoorOpen": df.iloc[:, 12] if df.shape[1] > 12 else "",
            "HandleFinish": df.iloc[:, 13] if df.shape[1] > 13 else "",
        }
    )

    out["W"] = out["W"].map(to_float_mm)
    out["H"] = out["H"].map(to_float_mm)

    out = out.dropna(subset=["ProjectID", "PieceID", "Typology", "Material", "Gama", "Acabado", "W", "H"])
    out = out[(out["W"] > 0) & (out["H"] > 0)]

    out["Material_norm"] = out["Material"].map(normalize_material)
    out["Gama_norm"] = out["Gama"].map(normalize_gama)
    out["Acabado_norm"] = out["Acabado"].map(normalize_acabado)

    return out


# =========================================================
# Nesting (guillotine)
# =========================================================
@dataclass
class FreeRect:
    x: float
    y: float
    w: float
    h: float


@dataclass
class PlacedPiece:
    piece_id: str
    typology: str
    x: float
    y: float
    w: float
    h: float
    rotated: bool


@dataclass
class PieceItem:
    piece_id: str
    typology: str
    w: float
    h: float


def rect_contains(a: FreeRect, b: FreeRect) -> bool:
    return (b.x >= a.x and b.y >= a.y and (b.x + b.w) <= (a.x + a.w) and (b.y + b.h) <= (a.y + a.h))


def prune_free_rects(frees: List[FreeRect]) -> List[FreeRect]:
    pruned = []
    for i, r in enumerate(frees):
        contained = False
        for j, s in enumerate(frees):
            if i == j:
                continue
            if rect_contains(s, r):
                contained = True
                break
        if not contained:
            pruned.append(r)
    return pruned


def split_free_rect(fr: FreeRect, placed: FreeRect) -> List[FreeRect]:
    res = []
    rw = fr.w - placed.w
    rh = placed.h
    if rw > 0 and rh > 0:
        res.append(FreeRect(fr.x + placed.w, fr.y, rw, rh))
    uw = fr.w
    uh = fr.h - placed.h
    if uw > 0 and uh > 0:
        res.append(FreeRect(fr.x, fr.y + placed.h, uw, uh))
    return res


def pack_group_with_positions(
    items: List[PieceItem],
    usable_w: float,
    usable_h: float,
    allow_rotate: bool,
) -> Tuple[List[List[PlacedPiece]], List[PieceItem]]:
    unplaced: List[PieceItem] = []
    work: List[PieceItem] = []

    for it in items:
        w_eff = it.w + GAP_BETWEEN
        h_eff = it.h + GAP_BETWEEN
        fits = (w_eff <= usable_w and h_eff <= usable_h) or (allow_rotate and h_eff <= usable_w and w_eff <= usable_h)
        if not fits:
            unplaced.append(it)
        else:
            work.append(it)

    work.sort(key=lambda p: (max(p.w, p.h), p.w * p.h), reverse=True)
    boards: List[List[PlacedPiece]] = []

    while work:
        frees = [FreeRect(0, 0, usable_w, usable_h)]
        placed_this_board: List[PlacedPiece] = []
        remaining: List[PieceItem] = []

        for it in work:
            best = None  # (score, free_index, ww_eff, hh_eff, rotated, ww_nom, hh_nom)
            for fi, fr in enumerate(frees):
                ww_eff = it.w + GAP_BETWEEN
                hh_eff = it.h + GAP_BETWEEN
                if ww_eff <= fr.w and hh_eff <= fr.h:
                    score = (fr.w - ww_eff) + (fr.h - hh_eff)
                    cand = (score, fi, ww_eff, hh_eff, False, it.w, it.h)
                    if best is None or cand < best:
                        best = cand

                if allow_rotate:
                    ww_eff_r = it.h + GAP_BETWEEN
                    hh_eff_r = it.w + GAP_BETWEEN
                    if ww_eff_r <= fr.w and hh_eff_r <= fr.h:
                        score = (fr.w - ww_eff_r) + (fr.h - hh_eff_r)
                        cand = (score, fi, ww_eff_r, hh_eff_r, True, it.h, it.w)
                        if best is None or cand < best:
                            best = cand

            if best is None:
                remaining.append(it)
                continue

            _, fi, ww_eff, hh_eff, rotated, ww_nom, hh_nom = best
            fr = frees.pop(fi)

            placed_eff = FreeRect(fr.x, fr.y, ww_eff, hh_eff)
            frees.extend(split_free_rect(fr, placed_eff))
            frees = prune_free_rects(frees)

            placed_this_board.append(
                PlacedPiece(
                    piece_id=str(it.piece_id),
                    typology=str(it.typology),
                    x=placed_eff.x,
                    y=placed_eff.y,
                    w=ww_nom,
                    h=hh_nom,
                    rotated=rotated,
                )
            )

        boards.append(placed_this_board)
        work = remaining

    return boards, unplaced


def typology_color_map(typologies: List[str]) -> Dict[str, Tuple[float, float, float, float]]:
    uniq = sorted({str(t) for t in typologies})
    cmap = plt.get_cmap("tab20")
    return {t: cmap(i % 20) for i, t in enumerate(uniq)}


def render_board_png(
    board_w: int,
    board_h: int,
    usable_w: float,
    usable_h: float,
    pieces: List[PlacedPiece],
    title: str,
    color_by_typology: Dict[str, Tuple[float, float, float, float]],
    legend_max_items: int = 30,
) -> bytes:
    fig_w = 12
    fig_h = max(6, 10 * (board_h / board_w) * 0.35)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    fig.subplots_adjust(right=0.78)

    ax.add_patch(Rectangle((0, 0), board_w, board_h, fill=False, linewidth=2))
    ax.add_patch(Rectangle((EDGE_MARGIN, EDGE_MARGIN), usable_w, usable_h, fill=False, linestyle="--", linewidth=1))

    for p in pieces:
        x = EDGE_MARGIN + p.x
        y = EDGE_MARGIN + p.y
        col = color_by_typology.get(str(p.typology), (0.7, 0.7, 0.7, 1.0))

        ax.add_patch(Rectangle((x, y), p.w, p.h, facecolor=col, edgecolor="black", linewidth=0.6, alpha=0.9))
        ax.text(x + p.w / 2, y + p.h / 2, str(p.piece_id), ha="center", va="center", fontsize=6, color="black")

    ax.set_title(title, fontsize=12)
    ax.set_xlim(0, board_w)
    ax.set_ylim(0, board_h)
    ax.set_aspect("equal", adjustable="box")
    ax.invert_yaxis()
    ax.axis("off")

    present_typologies = sorted({str(p.typology) for p in pieces})
    legend_items = present_typologies[:legend_max_items]
    if legend_items:
        handles = [
            Rectangle((0, 0), 1, 1, facecolor=color_by_typology.get(t, (0.7, 0.7, 0.7, 1.0)), edgecolor="black")
            for t in legend_items
        ]
        ax.legend(
            handles,
            legend_items,
            loc="center left",
            bbox_to_anchor=(1.01, 0.5),
            borderaxespad=0.0,
            frameon=False,
            fontsize=7,
            ncol=1,
        )

        if len(present_typologies) > legend_max_items:
            ax.text(
                1.01,
                0.02,
                f"+{len(present_typologies) - legend_max_items} tipologías más",
                transform=ax.transAxes,
                fontsize=7,
                ha="left",
                va="bottom",
            )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# =========================================================
# UI
# =========================================================
st.set_page_config(page_title=APP_TITLE, layout="wide")

# --- Session state init (CLAVE para que no se pierda el CSV) ---
if "csv_bytes" not in st.session_state:
    st.session_state["csv_bytes"] = None
if "csv_name" not in st.session_state:
    st.session_state["csv_name"] = None

st.markdown(
    """
<style>
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
h1 { font-size: 1.8rem; }
small, .stCaption { opacity: 0.85; }
hr { margin: 0.8rem 0; }
</style>
""",
    unsafe_allow_html=True,
)

st.title(APP_TITLE)
st.caption("v5: selector CSV desde Drive (dropdown) o subida manual. (Fix: el CSV no se pierde al generar layouts)")

if SIDEBAR_LOGO_PATH.exists():
    st.sidebar.image(str(SIDEBAR_LOGO_PATH), use_container_width=True)

st.sidebar.header("Configuración")

source = st.sidebar.radio("Origen del CSV", ["Google Drive (carpeta)", "Subida manual"], index=0)

# ---- Drive selection / manual upload ----
if source == "Google Drive (carpeta)":
    if "gdrive" not in st.secrets or "folder_id" not in st.secrets["gdrive"]:
        st.sidebar.error("Falta configurar [gdrive].folder_id en Secrets.")
        st.stop()

    folder_id = st.secrets["gdrive"]["folder_id"]

    try:
        files = list_csv_files_in_folder(folder_id)
    except Exception as e:
        st.sidebar.error(f"No pude listar archivos en Drive. Revisa permisos/Secrets. Error: {e}")
        st.stop()

    if not files:
        st.sidebar.warning("No se encontraron CSV en la carpeta (o no hay permisos).")
        st.stop()

    options = {
        f'{f["name"]}  —  {str(f.get("modifiedTime",""))[:10]}': (f["id"], f["name"])
        for f in files
    }
    chosen_label = st.sidebar.selectbox("Selecciona un CSV de Drive", list(options.keys()))
    chosen_id, chosen_name = options[chosen_label]

    b1, b2 = st.sidebar.columns(2)
    with b1:
        if st.button("Refrescar lista"):
            list_csv_files_in_folder.clear()
            st.rerun()
    with b2:
        if st.button("Cargar CSV"):
            with st.spinner("Descargando CSV desde Drive..."):
                data = download_drive_file_as_bytes(chosen_id)
                st.session_state["csv_bytes"] = data
                st.session_state["csv_name"] = chosen_name
            st.sidebar.success(f"Cargado: {chosen_name}")

else:
    up = st.sidebar.file_uploader("Subir CSV", type=["csv"])
    if up is not None:
        # Persistir bytes en session_state para que no se pierda en reruns
        st.session_state["csv_bytes"] = up.getvalue()
        st.session_state["csv_name"] = getattr(up, "name", "upload.csv")
        st.sidebar.success(f"Cargado: {st.session_state['csv_name']}")

st.sidebar.caption("Formato esperado: A=Proyecto, C=PieceID, D=Typology, E=Ancho, F=Alto, G=Material, H=Gama, I=Acabado.")

with st.sidebar.expander("Nesting visual", expanded=True):
    make_layouts = st.checkbox("Generar layouts (PNG + ZIP)", value=False)
    preview_preset = st.selectbox(
        "Tamaño miniaturas",
        list(PREVIEW_WIDTH_PRESETS.keys()),
        index=list(PREVIEW_WIDTH_PRESETS.keys()).index(DEFAULT_PREVIEW_PRESET),
    )
    preview_max_boards_per_group = st.number_input(
        "Máx. tableros por grupo (0=todos)",
        min_value=0,
        max_value=200,
        value=6,
        step=1,
    )

with st.sidebar.expander("Notas (para export)", expanded=False):
    nota_titulo = st.text_input("Título / referencia", value="")
    nota_texto = st.text_area("Notas", value="", height=90)

# ---- Usar SIEMPRE el CSV persistido ----
if st.session_state["csv_bytes"] is None:
    st.info("Carga un CSV desde Drive o súbelo manualmente desde el panel lateral.")
    st.stop()

uploaded = BytesUploadedFile(st.session_state["csv_bytes"], name=st.session_state["csv_name"] or "data.csv")

try:
    pieces = load_pieces_v5(uploaded)
except Exception as e:
    st.error(str(e))
    st.stop()

projects = sorted(pieces["ProjectID"].astype(str).unique().tolist())
with st.sidebar.expander("Filtros", expanded=True):
    default_sel = projects[:50] if len(projects) > 50 else projects
    selected_projects = st.multiselect("ID de proyecto", projects, default=default_sel)

filtered = pieces[pieces["ProjectID"].astype(str).isin([str(p) for p in selected_projects])].copy()
if filtered.empty:
    st.warning("Con esos filtros no quedan piezas.")
    st.stop()

# ---- Resumen rápido ----
total_projects = filtered["ProjectID"].nunique()
total_pieces = len(filtered)

boards_total_global = 0
util_vals = []

for keys, grp in filtered.groupby(["Material_norm", "Gama_norm", "Acabado_norm"], dropna=False):
    _mat_n, gama_n, acab_n = keys
    rule = get_board_rule(gama_n, acab_n)
    if rule is None:
        continue

    board_w, board_h, allow_rotate = rule["board_w"], rule["board_h"], rule["rotate"]
    usable_w = board_w - 2 * EDGE_MARGIN
    usable_h = board_h - 2 * EDGE_MARGIN

    items = [
        PieceItem(piece_id=str(r["PieceID"]), typology=str(r["Typology"]), w=float(r["W"]), h=float(r["H"]))
        for _, r in grp.iterrows()
    ]
    boards, _ = pack_group_with_positions(items, usable_w, usable_h, allow_rotate)
    bcount = len(boards)
    boards_total_global += bcount

    total_nom_area = float((grp["W"] * grp["H"]).sum())
    usable_area = usable_w * usable_h
    util = total_nom_area / (bcount * usable_area) if bcount > 0 else 0.0
    util_vals.append(util)

avg_util = (sum(util_vals) / len(util_vals)) if util_vals else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Proyectos", f"{total_projects}")
m2.metric("Piezas", f"{total_pieces}")
m3.metric("Tableros (est.)", f"{boards_total_global}")
m4.metric("Aprovechamiento medio", f"{avg_util*100:.1f}%")
st.divider()

# ---- Vista previa ----
with st.expander("Vista previa (filtrable)", expanded=False):
    HIDE_PREVIEW_COLS = {"Material_norm", "Gama_norm", "Acabado_norm"}
    cols_all = [c for c in pieces.columns if c not in HIDE_PREVIEW_COLS]
    cols_selected = st.multiselect("Columnas a mostrar", cols_all, default=cols_all)

    col_filter = st.selectbox("Filtrar por columna (opcional)", ["—"] + cols_selected)
    if col_filter != "—":
        uniq = sorted(filtered[col_filter].astype(str).unique().tolist())
        if len(uniq) > 300:
            st.info(f"Hay {len(uniq)} valores distintos. Usa búsqueda por texto.")
            query = st.text_input("Buscar texto", value="")
            preview_df = (
                filtered[filtered[col_filter].astype(str).str.contains(query, case=False, na=False)]
                if query else filtered
            )
        else:
            selected_vals = st.multiselect("Valores", uniq, default=uniq)
            preview_df = filtered[filtered[col_filter].astype(str).isin(selected_vals)]
    else:
        preview_df = filtered

    st.dataframe(preview_df[cols_selected].head(250), use_container_width=True)

# ---- Tablas resultados ----
rows = []
issues = []

group_cols_project = ["ProjectID", "Material_norm", "Gama_norm", "Acabado_norm", "Material", "Gama", "Acabado"]
for keys, grp in filtered.groupby(group_cols_project, dropna=False):
    proj, mat_n, gama_n, acab_n, _mat_raw, gama_raw, acab_raw = keys

    rule = get_board_rule(gama_n, acab_n)
    if rule is None:
        issues.append(f"⚠️ Gama desconocida: '{gama_raw}' en proyecto {proj}. No se calcula.")
        continue

    board_w, board_h, allow_rotate = rule["board_w"], rule["board_h"], rule["rotate"]
    usable_w = board_w - 2 * EDGE_MARGIN
    usable_h = board_h - 2 * EDGE_MARGIN

    items = [
        PieceItem(piece_id=str(r["PieceID"]), typology=str(r["Typology"]), w=float(r["W"]), h=float(r["H"]))
        for _, r in grp.iterrows()
    ]
    boards, unplaced = pack_group_with_positions(items, usable_w, usable_h, allow_rotate)

    if unplaced:
        issues.append(f"⚠️ {proj} / {mat_n} / {gama_raw} / {acab_raw}: {len(unplaced)} pieza(s) no caben y se omiten del layout.")

    boards_count = len(boards)
    total_nom_area = float((grp["W"] * grp["H"]).sum())
    util = total_nom_area / (boards_count * (usable_w * usable_h)) if boards_count > 0 else 0.0

    rows.append(
        {
            "ProjectID": proj,
            "Material": mat_n,
            "Gama": GAMA_DISPLAY.get(gama_n, str(gama_raw)),
            "Acabado": str(acab_raw),
            "Tablero (mm)": f"{board_w}×{board_h}",
            "Rotar": "Sí" if allow_rotate else "No",
            "Piezas": len(grp),
            "Tableros": int(boards_count),
            "Aprovechamiento est.": f"{util * 100:.1f}%",
        }
    )

by_project_df = pd.DataFrame(rows)
if not by_project_df.empty:
    by_project_df = by_project_df.sort_values(["ProjectID", "Material", "Gama", "Acabado"]).reset_index(drop=True)

rows2 = []
group_cols_global = ["Material_norm", "Gama_norm", "Acabado_norm", "Material", "Gama", "Acabado"]
for keys, grp in filtered.groupby(group_cols_global, dropna=False):
    mat_n, gama_n, acab_n, _mat_raw, gama_raw, acab_raw = keys
    rule = get_board_rule(gama_n, acab_n)
    if rule is None:
        continue

    board_w, board_h, allow_rotate = rule["board_w"], rule["board_h"], rule["rotate"]
    usable_w = board_w - 2 * EDGE_MARGIN
    usable_h = board_h - 2 * EDGE_MARGIN

    items = [
        PieceItem(piece_id=str(r["PieceID"]), typology=str(r["Typology"]), w=float(r["W"]), h=float(r["H"]))
        for _, r in grp.iterrows()
    ]
    boards, _ = pack_group_with_positions(items, usable_w, usable_h, allow_rotate)
    boards_count = len(boards)

    total_nom_area = float((grp["W"] * grp["H"]).sum())
    util = total_nom_area / (boards_count * (usable_w * usable_h)) if boards_count > 0 else 0.0

    rows2.append(
        {
            "Material": mat_n,
            "Gama": GAMA_DISPLAY.get(gama_n, str(gama_raw)),
            "Acabado": str(acab_raw),
            "Tablero (mm)": f"{board_w}×{board_h}",
            "Piezas": len(grp),
            "Tableros": int(boards_count),
            "Aprovechamiento est.": f"{util * 100:.1f}%",
        }
    )

by_finish_df = pd.DataFrame(rows2)
if not by_finish_df.empty:
    by_finish_df = by_finish_df.sort_values(["Material", "Gama", "Acabado"]).reset_index(drop=True)

with st.expander("Resultados por proyecto (tableros por proyecto + material + gama + acabado)", expanded=True):
    st.dataframe(by_project_df, use_container_width=True)

with st.expander("Resumen global (material + gama + acabado)", expanded=True):
    st.dataframe(by_finish_df, use_container_width=True)

if issues:
    with st.expander("Avisos", expanded=False):
        for msg in issues:
            st.write(msg)

# ---- Descargas ----
st.subheader("Descargas")
d1, d2 = st.columns(2)

with d1:
    st.download_button(
        "Descargar CSV (por proyecto)",
        data=by_project_df.to_csv(index=False).encode("utf-8"),
        file_name="CUBRO_QuickNesting_v5_por_proyecto.csv",
        mime="text/csv",
        use_container_width=True,
    )

with d2:
    by_finish_df_export = by_finish_df.copy()
    by_finish_df_export.insert(0, "Titulo", nota_titulo)
    by_finish_df_export.insert(1, "Notas", nota_texto)

    st.download_button(
        "Descargar resumen (CSV)",
        data=by_finish_df_export.to_csv(index=False).encode("utf-8"),
        file_name="CUBRO_QuickNesting_v5_resumen_global.csv",
        mime="text/csv",
        use_container_width=True,
    )

st.divider()

# ---- Nesting visual ----
st.subheader("Nesting visual")

if not make_layouts:
    st.info("Activa “Generar layouts (PNG + ZIP)” desde el panel lateral para generar nesting visual.")
    st.stop()

st.caption("Se generan PNGs por tablero para cada grupo Material + Gama + Acabado (según el filtro de proyectos).")

# Importante: botón que rerunea pero NO pierde el CSV porque está en session_state
if st.button("Generar layouts y preparar descarga ZIP", type="primary"):
    colors = typology_color_map(filtered["Typology"].astype(str).tolist())
    preview_width_px = PREVIEW_WIDTH_PRESETS.get(preview_preset, 280)
    cols_n = auto_preview_cols(int(preview_width_px))

    preview_images: List[Tuple[str, int, bytes]] = []
    zip_buf = io.BytesIO()

    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for keys, grp in filtered.groupby(
            ["Material_norm", "Gama_norm", "Acabado_norm", "Material", "Gama", "Acabado"], dropna=False
        ):
            mat_n, gama_n, acab_n, _mat_raw, gama_raw, acab_raw = keys
            rule = get_board_rule(gama_n, acab_n)
            if rule is None:
                continue

            board_w, board_h, _allow_rotate = rule["board_w"], rule["board_h"], rule["rotate"]
            usable_w = board_w - 2 * EDGE_MARGIN
            usable_h = board_h - 2 * EDGE_MARGIN

            items = [
                PieceItem(piece_id=str(r["PieceID"]), typology=str(r["Typology"]), w=float(r["W"]), h=float(r["H"]))
                for _, r in grp.iterrows()
            ]
            boards, unplaced = pack_group_with_positions(items, usable_w, usable_h, rule["rotate"])

            gama_disp = GAMA_DISPLAY.get(gama_n, str(gama_raw))
            group_name = f"{mat_n}__{gama_disp}__{str(acab_raw)}".replace("/", "-").replace("\\", "-").replace(":", "-")

            for bi, placed_list in enumerate(boards, start=1):
                title = f"{APP_TITLE} | {group_name.replace('__', ' / ')} | Tablero {bi}/{len(boards)}"
                png_bytes = render_board_png(
                    board_w=board_w,
                    board_h=board_h,
                    usable_w=usable_w,
                    usable_h=usable_h,
                    pieces=placed_list,
                    title=title,
                    color_by_typology=colors,
                )
                zf.writestr(f"{group_name}/TABLERO_{bi:03d}.png", png_bytes)

                if preview_max_boards_per_group == 0 or bi <= int(preview_max_boards_per_group):
                    preview_images.append((group_name, bi, png_bytes))

            if unplaced:
                txt = "PIEZAS QUE NO CABEN EN EL TABLERO (omitidas):\n\n"
                for it in unplaced:
                    txt += f"- PieceID: {it.piece_id} | Typology: {it.typology} | W:{it.w} | H:{it.h}\n"
                zf.writestr(f"{group_name}/_PIEZAS_NO_CABEN.txt", txt)

    zip_buf.seek(0)

    if preview_images:
        st.markdown("### Previsualización")
        groups: Dict[str, List[Tuple[int, bytes]]] = {}
        for g, bi, png in preview_images:
            groups.setdefault(g, []).append((bi, png))

        for group_name, imgs in groups.items():
            imgs.sort(key=lambda x: x[0])
            with st.expander(group_name.replace("__", " / "), expanded=False):
                cols = st.columns(cols_n)
                for idx, (bi, pngbytes) in enumerate(imgs):
                    with cols[idx % cols_n]:
                        st.image(pngbytes, caption=f"Tablero {bi}", width=int(preview_width_px))

    st.success("ZIP generado.")
    st.download_button(
        "Descargar ZIP de layouts (PNGs)",
        data=zip_buf.getvalue(),
        file_name="CUBRO_QuickNesting_v5_layouts.zip",
        mime="application/zip",
        use_container_width=True,
    )






