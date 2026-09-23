"""Contexto 3D ao redor da rede com dados abertos: relevo, vegetação, prédios e ruas.

Fontes gratuitas:
- Relevo: Copernicus DEM GLO-30 (AWS Open Data). É um modelo de SUPERFÍCIE; o terreno é aproximado
  subtraindo a altura média do dossel em cada célula de 30 m.
- Vegetação: Meta/WRI High Resolution Canopy Height Maps (~1 m, altura acima do solo, AWS Open Data).
- Prédios e ruas: Overture Maps (Google Open Buildings, Microsoft ML Buildings e OpenStreetMap).

O cache é organizado em LADRILHOS FIXOS em coordenadas UTM absolutas (dossel a cada 2 km, Overture a
cada 10 km), e não por rede. Assim redes vizinhas aproveitam o que já foi baixado e o sistema busca
na internet apenas os ladrilhos que faltam.
"""
from __future__ import annotations

import base64
import json
import math
import re
import urllib.request
from pathlib import Path

import duckdb
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import reproject
from rasterio.windows import Window, from_bounds
from scipy import ndimage
from shapely import wkb as shapely_wkb
from shapely.geometry import LineString
from shapely.ops import unary_union

from .config import settings

CACHE_DIR = settings.cache_dir
DEM_URL = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
CHM_URL = "/vsicurl/https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float/chm/{qk}.tif"
OVERTURE_LIST_URL = "https://overturemaps-us-west-2.s3.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
OVERTURE_FALLBACK_RELEASE = "2026-08-19.0"
OVERTURE_TEMAS = {
    "predios": ("theme=buildings/type=building", "1 = 1"),
    "ruas": ("theme=transportation/type=segment", "subtype = 'road'"),
}

TERRAIN_STEP_M = 30  # resolução do Copernicus GLO-30
CHM_STEP_M = 1
CHM_NODATA = 255
CANOPY_TILE_M = 2000  # ladrilho do cache de dossel
CANOPY_TILES_EM_MEMORIA = 24  # cada ladrilho ocupa 4 MB; mantém o consumo do serviço previsível
OVERTURE_TILE_M = 10000  # ladrilho do cache de prédios e ruas
TREE_PEAK_WINDOW_PX = 5
TREE_MIN_SPACING_M = 3
MAX_TREES = 200_000
PARALLEL_DROP_M = 1.4  # mesmo rebaixamento de circuitos paralelos usado no desenho
SPACER_DROP_M = 0.44  # no Spacer Cable a fase mais baixa fica abaixo do ponto de fixação
AREA_HEIGHT_ESTIMATE = [(80, 3.5), (250, 5.0), (800, 7.0), (3000, 9.0), (math.inf, 12.0)]
ROAD_WIDTH_M = {
    "motorway": 14, "trunk": 12, "primary": 10, "secondary": 9, "tertiary": 8,
    "residential": 6, "living_street": 5, "unclassified": 5, "service": 4, "track": 3.5,
    "pedestrian": 4, "footway": 2, "path": 2, "cycleway": 2.5, "steps": 2,
}
DEFAULT_ROAD_WIDTH_M = 5
ROAD_SIMPLIFY_M = 0.5

ATTRIBUTION = (
    "Relevo: Copernicus DEM GLO-30 (ESA/UE) · Vegetação: Meta & World Resources Institute (CC BY 4.0) · "
    "Prédios e ruas: Overture Maps (ODbL/CDLA)"
)


# --- grade da rede ---------------------------------------------------------------------------------
def _grid_spec(model: dict) -> dict:
    """Grade do relevo cobrindo os ladrilhos do mapa base, alinhada à grade de 1 m do dossel."""
    tiles = model["mapa"]["tiles"]
    if tiles:
        xs = [c for t in tiles for c in t["c"][0::2]]
        ys = [c for t in tiles for c in t["c"][1::2]]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    else:
        bx0, by0, bx1, by1 = model["meta"]["bbox"]
        x0, y0, x1, y1 = bx0 - 300, by0 - 300, bx1 + 300, by1 + 300
    step = TERRAIN_STEP_M
    x0, y1 = math.floor(x0), math.ceil(y1)
    nx = math.ceil((x1 - x0) / step) + 1
    ny = math.ceil((y1 - y0) / step) + 1
    return {
        "epsg": model["meta"]["epsg"], "ox": model["meta"]["origem"]["x"], "oy": model["meta"]["origem"]["y"],
        "x0": x0, "y1": y1, "step": step, "nx": nx, "ny": ny,
        "x1": x0 + (nx - 1) * step, "y0": y1 - (ny - 1) * step,
    }


def _bounds_abs(spec: dict, margem: float = 0.0) -> tuple[float, float, float, float]:
    """Retângulo da grade em coordenadas UTM absolutas."""
    return (spec["ox"] + spec["x0"] - margem, spec["oy"] + spec["y0"] - margem,
            spec["ox"] + spec["x1"] + margem, spec["oy"] + spec["y1"] + margem)


def _bounds_rede(model: dict, margem: float = 250.0) -> tuple[float, float, float, float]:
    """Retângulo da rede (não o do mapa) em UTM absoluto: limita o volume de prédios e ruas servidos."""
    bx0, by0, bx1, by1 = model["meta"]["bbox"]
    ox, oy = model["meta"]["origem"]["x"], model["meta"]["origem"]["y"]
    return (ox + bx0 - margem, oy + by0 - margem, ox + bx1 + margem, oy + by1 + margem)


def _lonlat_bounds(epsg: int, bounds: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    e0, n0, e1, n1 = bounds
    to_geo = Transformer.from_crs(epsg, 4326, always_xy=True)
    lons, lats = to_geo.transform([e0, e1, e0, e1], [n0, n0, n1, n1])
    return min(lons), min(lats), max(lons), max(lats)


def _tiles_for(bounds: tuple[float, float, float, float], size: int) -> list[tuple[int, int]]:
    e0, n0, e1, n1 = bounds
    return [(tx, ty)
            for tx in range(math.floor(e0 / size), math.floor(e1 / size) + 1)
            for ty in range(math.floor(n0 / size), math.floor(n1 / size) + 1)]


def _sampler(grid: np.ndarray, spec: dict, step: float):
    """Amostragem bilinear em coordenadas locais (linha 0 da grade = norte)."""
    def sample(x, y):
        col = (np.asarray(x, dtype=float) - spec["x0"]) / step
        row = (spec["y1"] - np.asarray(y, dtype=float)) / step
        return ndimage.map_coordinates(grid, [row, col], order=1, mode="nearest")
    return sample


# --- relevo ---------------------------------------------------------------------------------------
def _dem_tile(lat_floor: int, lon_floor: int) -> Path | None:
    ns, ew = ("N" if lat_floor >= 0 else "S"), ("E" if lon_floor >= 0 else "W")
    name = f"Copernicus_DSM_COG_10_{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00_DEM"
    path = CACHE_DIR / "dem" / f"{name}.tif"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        provisorio = path.with_suffix(".parte")
        try:
            urllib.request.urlretrieve(DEM_URL.format(name=name), provisorio)
        except OSError:
            return None  # oceano: o Copernicus não publica o ladrilho
        provisorio.replace(path)
    return path


def _dem_on_grid(spec: dict) -> np.ndarray:
    w, s, e, n = _lonlat_bounds(spec["epsg"], _bounds_abs(spec))
    step = spec["step"]
    transform = from_origin(spec["ox"] + spec["x0"] - step / 2, spec["oy"] + spec["y1"] + step / 2, step, step)
    dem = np.full((spec["ny"], spec["nx"]), np.nan, dtype=np.float32)
    for lat in range(math.floor(s), math.floor(n) + 1):
        for lon in range(math.floor(w), math.floor(e) + 1):
            path = _dem_tile(lat, lon)
            if path is None:
                continue
            with rasterio.open(path) as src:
                parte = np.full_like(dem, np.nan)
                reproject(rasterio.band(src, 1), parte, dst_transform=transform, dst_crs=f"EPSG:{spec['epsg']}",
                          dst_nodata=np.nan, resampling=Resampling.bilinear)
            dem = np.where(np.isnan(dem), parte, dem)
    if np.isnan(dem).all():
        raise RuntimeError("Não há relevo Copernicus para esta região.")
    return np.where(np.isnan(dem), np.nanmean(dem), dem)


# --- dossel (vegetação) em ladrilhos de 2 km --------------------------------------------------------
def _quadkeys(epsg: int, bounds: tuple[float, float, float, float], z: int = 9) -> set[str]:
    w, s, e, n = _lonlat_bounds(epsg, bounds)
    chaves = set()
    for lon in (w, e):
        for lat in (s, n):
            size = 2 ** z
            x = int((lon + 180) / 360 * size)
            y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * size)
            chaves.add("".join(str(((x >> i) & 1) + 2 * ((y >> i) & 1)) for i in range(z - 1, -1, -1)))
    return chaves


def _overlap_window(src, bounds: tuple[float, float, float, float]) -> Window | None:
    win = from_bounds(*bounds, src.transform)
    c0, r0 = max(int(math.floor(win.col_off)), 0), max(int(math.floor(win.row_off)), 0)
    c1 = min(int(math.ceil(win.col_off + win.width)), src.width)
    r1 = min(int(math.ceil(win.row_off + win.height)), src.height)
    return Window(c0, r0, c1 - c0, r1 - r0) if c1 > c0 and r1 > r0 else None


class Dossel:
    """Altura do dossel (m) em ladrilhos absolutos de 2 km, compartilhados por todas as redes."""

    def __init__(self, epsg: int) -> None:
        self.epsg = epsg
        self.pasta = CACHE_DIR / "dossel" / str(epsg)
        self._memoria: dict[tuple[int, int], np.ndarray | None] = {}

    def _arquivo(self, tx: int, ty: int) -> Path:
        return self.pasta / f"{tx}_{ty}.tif"

    def garantir(self, bounds: tuple[float, float, float, float]) -> dict:
        """Baixa de uma vez só os ladrilhos que ainda não estão no cache."""
        chaves = _tiles_for(bounds, CANOPY_TILE_M)
        faltando = [k for k in chaves if not self._arquivo(*k).exists()]
        if faltando:
            e0 = min(tx for tx, _ in faltando) * CANOPY_TILE_M
            n0 = min(ty for _, ty in faltando) * CANOPY_TILE_M
            e1 = (max(tx for tx, _ in faltando) + 1) * CANOPY_TILE_M
            n1 = (max(ty for _, ty in faltando) + 1) * CANOPY_TILE_M
            bloco = self._baixar(e0, n0, e1, n1)
            self.pasta.mkdir(parents=True, exist_ok=True)
            for tx, ty in faltando:
                col = int(tx * CANOPY_TILE_M - e0)
                row = int(n1 - (ty + 1) * CANOPY_TILE_M)
                self._gravar(tx, ty, bloco[row: row + CANOPY_TILE_M, col: col + CANOPY_TILE_M])
        return {"total": len(chaves), "novos": len(faltando), "ladrilho_m": CANOPY_TILE_M}

    def _baixar(self, e0: float, n0: float, e1: float, n1: float) -> np.ndarray:
        largura, altura = int(e1 - e0), int(n1 - n0)
        destino = np.full((altura, largura), CHM_NODATA, dtype=np.uint8)
        transform = from_origin(e0, n1, CHM_STEP_M, CHM_STEP_M)
        env = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES"}
        with rasterio.Env(**env):
            for qk in sorted(_quadkeys(self.epsg, (e0, n0, e1, n1))):
                try:
                    src = rasterio.open(CHM_URL.format(qk=qk))
                except rasterio.RasterioIOError:
                    continue
                with src:
                    para_src = Transformer.from_crs(self.epsg, src.crs, always_xy=True)
                    xs, ys = para_src.transform([e0, e1, e0, e1], [n0, n0, n1, n1])
                    win = _overlap_window(src, (min(xs) - 50, min(ys) - 50, max(xs) + 50, max(ys) + 50))
                    if win is None:
                        continue
                    parte = np.full_like(destino, CHM_NODATA)
                    reproject(src.read(1, window=win), parte, src_transform=src.window_transform(win),
                              src_crs=src.crs, dst_transform=transform, dst_crs=f"EPSG:{self.epsg}",
                              dst_nodata=CHM_NODATA, resampling=Resampling.nearest)
                destino = np.where(destino == CHM_NODATA, parte, destino)
        return destino

    def _gravar(self, tx: int, ty: int, dados: np.ndarray) -> None:
        perfil = {
            "driver": "GTiff", "height": dados.shape[0], "width": dados.shape[1], "count": 1,
            "dtype": "uint8", "nodata": CHM_NODATA, "crs": f"EPSG:{self.epsg}",
            "transform": from_origin(tx * CANOPY_TILE_M, (ty + 1) * CANOPY_TILE_M, CHM_STEP_M, CHM_STEP_M),
            "compress": "deflate", "tiled": True,
        }
        with rasterio.open(self._arquivo(tx, ty), "w", **perfil) as ds:
            ds.write(dados, 1)

    def ladrilho(self, tx: int, ty: int) -> np.ndarray | None:
        if (tx, ty) not in self._memoria:
            arquivo = self._arquivo(tx, ty)
            if arquivo.exists():
                with rasterio.open(arquivo) as ds:
                    self._memoria[(tx, ty)] = ds.read(1)
            else:
                self._memoria[(tx, ty)] = None
            while len(self._memoria) > CANOPY_TILES_EM_MEMORIA:
                self._memoria.pop(next(iter(self._memoria)))  # descarta o mais antigo
        return self._memoria[(tx, ty)]

    def amostrar(self, E, N) -> np.ndarray:
        """Altura do dossel (0 onde não há dado) nas coordenadas UTM absolutas dadas."""
        E, N = np.asarray(E, dtype=float), np.asarray(N, dtype=float)
        saida = np.zeros(E.shape, dtype=np.float32)
        tx = np.floor(E / CANOPY_TILE_M).astype(int)
        ty = np.floor(N / CANOPY_TILE_M).astype(int)
        for chave in {(int(a), int(b)) for a, b in zip(tx.ravel(), ty.ravel())}:
            arr = self.ladrilho(*chave)
            if arr is None:
                continue
            m = (tx == chave[0]) & (ty == chave[1])
            i = np.clip((E[m] - chave[0] * CANOPY_TILE_M).astype(int), 0, arr.shape[1] - 1)
            j = np.clip(((chave[1] + 1) * CANOPY_TILE_M - N[m]).astype(int), 0, arr.shape[0] - 1)
            v = arr[j, i]
            saida[m] = np.where(v == CHM_NODATA, 0, v)
        return saida


def _dossel_medio(spec: dict, dossel: Dossel) -> np.ndarray:
    """Média do dossel em cada célula de 30 m (9 amostras por célula), para corrigir o DSM."""
    xs = spec["ox"] + spec["x0"] + np.arange(spec["nx"]) * spec["step"]
    ys = spec["oy"] + spec["y1"] - np.arange(spec["ny"]) * spec["step"]
    X, Y = np.meshgrid(xs, ys)
    total = np.zeros(X.shape, dtype=np.float32)
    for dx in (-10, 0, 10):
        for dy in (-10, 0, 10):
            total += dossel.amostrar(X + dx, Y + dy)
    return total / 9


def _relevo(spec: dict, dossel: Dossel) -> np.ndarray:
    chave = f"{spec['epsg']}_{spec['x0']}_{spec['y1']}_{spec['nx']}_{spec['ny']}_{spec['ox']}_{spec['oy']}"
    arquivo = CACHE_DIR / "relevo" / f"{chave}.npy"
    if arquivo.exists():
        return np.load(arquivo)
    ground = (_dem_on_grid(spec) - _dossel_medio(spec, dossel)).astype(np.float32)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    np.save(arquivo, ground)
    return ground


def terrain(model: dict) -> dict:
    spec = _grid_spec(model)
    dossel = Dossel(spec["epsg"])
    ladrilhos = dossel.garantir(_bounds_abs(spec))
    ground = _relevo(spec, dossel)
    base = float(math.floor(ground.min()))
    return {
        "x0": spec["x0"], "y1": spec["y1"], "dx": spec["step"], "nx": spec["nx"], "ny": spec["ny"],
        "base": base, "min": round(float(ground.min()), 1), "max": round(float(ground.max()), 1),
        "h": base64.b64encode(np.ascontiguousarray(ground - base, dtype="<f4").tobytes()).decode(),
        "ladrilhos": ladrilhos,
        "fonte": "Copernicus DEM GLO-30, descontada a altura média do dossel",
    }


# --- vegetação -------------------------------------------------------------------------------------
def _linhas_vaos(model: dict, spec: dict) -> list[LineString]:
    """Eixos dos vãos em coordenadas UTM absolutas."""
    postes, ox, oy = model["postes"], spec["ox"], spec["oy"]
    return [LineString([(postes[v["a"]]["x"] + ox, postes[v["a"]]["y"] + oy),
                        (postes[v["b"]]["x"] + ox, postes[v["b"]]["y"] + oy)]) for v in model["vaos"]]


def _arvores(dossel: Dossel, spec: dict, linhas: list[LineString]) -> np.ndarray:
    """Topos de copa (máximos locais) no corredor da rede -> [x, y, altura, raio] em coordenadas locais."""
    corredor = unary_union(linhas).buffer(settings.corredor_m)
    achados = []
    for tx, ty in _tiles_for(corredor.bounds, CANOPY_TILE_M):
        arr = dossel.ladrilho(tx, ty)
        if arr is None:
            continue
        transform = from_origin(tx * CANOPY_TILE_M, (ty + 1) * CANOPY_TILE_M, CHM_STEP_M, CHM_STEP_M)
        mascara = rasterize([(corredor, 1)], out_shape=arr.shape, transform=transform, fill=0, dtype=np.uint8)
        if not mascara.any():
            continue
        alturas = np.where(arr == CHM_NODATA, 0, arr)
        picos = ((alturas == ndimage.maximum_filter(alturas, size=TREE_PEAK_WINDOW_PX))
                 & (alturas >= settings.altura_min_arvore_m) & mascara.astype(bool))
        rows, cols = np.nonzero(picos)
        if not len(rows):
            continue
        # platôs de mesma altura geram picos vizinhos: um por célula de TREE_MIN_SPACING_M
        _, primeiros = np.unique((rows // TREE_MIN_SPACING_M) * 1_000_000 + cols // TREE_MIN_SPACING_M,
                                 return_index=True)
        rows, cols = rows[primeiros], cols[primeiros]
        h = alturas[rows, cols].astype(np.float32)
        achados.append(np.column_stack([
            tx * CANOPY_TILE_M + cols + 0.5 - spec["ox"],
            (ty + 1) * CANOPY_TILE_M - rows - 0.5 - spec["oy"],
            h, np.clip(0.25 * h + 0.5, 1.2, 7.0),  # raio de copa estimado pela altura
        ]).astype(np.float32))
    if not achados:
        return np.zeros((0, 4), dtype=np.float32)
    return np.vstack(achados)[:MAX_TREES]


def vegetation(model: dict) -> dict:
    spec = _grid_spec(model)
    dossel = Dossel(spec["epsg"])
    ladrilhos = dossel.garantir(_bounds_abs(spec))
    ground = _sampler(_relevo(spec, dossel), spec, spec["step"])
    ox, oy = spec["ox"], spec["oy"]
    postes, arranjo = model["postes"], model["arranjo"]
    fixacao = max(f[1] for f in arranjo["fases"])
    offsets = np.linspace(-settings.faixa_m, settings.faixa_m, 6)

    classes, criticos = [], []
    km = {nome: 0.0 for _, _, nome in settings.limiares}
    for i, v in enumerate(model["vaos"]):
        a, b = postes[v["a"]], postes[v["b"]]
        comprimento = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        if comprimento < 2:
            classes.append(0)
            continue
        t = np.linspace(0, 1, int(comprimento) + 1)
        xs, ys = a["x"] + (b["x"] - a["x"]) * t, a["y"] + (b["y"] - a["y"]) * t
        nx, ny = -(b["y"] - a["y"]) / comprimento, (b["x"] - a["x"]) / comprimento
        copas = dossel.amostrar((xs[:, None] + nx * offsets).ravel() + ox,
                                (ys[:, None] + ny * offsets).ravel() + oy)
        topo = copas.reshape(len(t), len(offsets)).max(axis=1)
        h = fixacao - v["lvl"] * PARALLEL_DROP_M - (SPACER_DROP_M if v.get("spacer") else 0)
        g = ground(xs, ys)
        ga, gb = ground([a["x"], b["x"]], [a["y"], b["y"]])
        flecha = min(max(comprimento * 0.015, 0.05), 1.2)
        cabo = (ga + h) * (1 - t) + (gb + h) * t - 4 * flecha * t * (1 - t) - g
        folga = cabo - topo
        pior = int(np.argmin(folga))
        codigo, nome = 0, "ok"
        for limite, c, n in settings.limiares:
            if folga[pior] < limite:
                codigo, nome = c, n
                break
        classes.append(codigo)
        if codigo:
            metros = float((folga < settings.limiares[-1][0]).sum() * comprimento / max(len(t) - 1, 1))
            km[nome] += comprimento / 1000
            criticos.append({
                "i": i, "id": v["id"], "c": v["c"], "classe": nome,
                "folga": round(float(folga[pior]), 1), "copa": int(topo[pior]), "cabo": round(float(cabo[pior]), 1),
                "extensao": round(metros, 1), "x": round(float(xs[pior]), 1), "y": round(float(ys[pior]), 1),
            })

    criticos.sort(key=lambda r: (r["folga"], -r["extensao"]))
    arvores = _arvores(dossel, spec, _linhas_vaos(model, spec))
    return {
        "arvores": {"n": int(len(arvores)),
                    "xyhr": base64.b64encode(np.ascontiguousarray(arvores, dtype="<f4").tobytes()).decode()},
        "classes": classes,
        "criticos": criticos,
        "limiares": [{"classe": n, "folga_menor_que": lim} for lim, _, n in settings.limiares],
        "resumo": {n: sum(1 for r in criticos if r["classe"] == n) for _, _, n in settings.limiares}
                  | {"km_" + n: round(k, 2) for n, k in km.items()},
        "ladrilhos": ladrilhos,
        "fonte": "Meta & WRI High Resolution Canopy Height (~1 m)",
    }


# --- Overture: prédios e ruas em ladrilhos de 10 km --------------------------------------------------
def _overture_release() -> str:
    try:
        xml = urllib.request.urlopen(OVERTURE_LIST_URL, timeout=20).read().decode()
        versoes = re.findall(r"<Prefix>release/([^/<]+)/</Prefix>", xml)
        return max(versoes) if versoes else OVERTURE_FALLBACK_RELEASE
    except OSError:
        return OVERTURE_FALLBACK_RELEASE


def _estimar_altura(area: float) -> float:
    return next(h for limite, h in AREA_HEIGHT_ESTIMATE if area < limite)


def _consultar_overture(tema: str, epsg: int, bounds: tuple[float, float, float, float]) -> list[dict]:
    """Feições do tema dentro do retângulo, já em coordenadas UTM absolutas."""
    w, s, e, n = _lonlat_bounds(epsg, bounds)
    caminho, filtro = OVERTURE_TEMAS[tema]
    release = _overture_release()
    con = duckdb.connect()
    for sql in ("INSTALL httpfs", "LOAD httpfs", "INSTALL spatial", "LOAD spatial", "SET s3_region='us-west-2'"):
        con.execute(sql)
    fonte = f"read_parquet('s3://overturemaps-us-west-2/release/{release}/{caminho}/*', hive_partitioning=1)"
    onde = f"{filtro} AND bbox.xmin <= {e} AND bbox.xmax >= {w} AND bbox.ymin <= {n} AND bbox.ymax >= {s}"
    colunas = ("height, num_floors, class, NULL AS nome" if tema == "predios"
               else "NULL AS height, NULL AS num_floors, class, struct_extract(names, 'primary') AS nome")
    try:
        linhas = con.execute(f"SELECT {colunas}, ST_AsWKB(geometry) AS geom FROM {fonte} WHERE {onde}").fetchall()
    except duckdb.Error:  # geometria ainda como WKB bruto
        linhas = con.execute(f"SELECT {colunas}, geometry AS geom FROM {fonte} WHERE {onde}").fetchall()

    partes = []
    for altura, andares, classe, nome, geom in linhas:
        forma = shapely_wkb.loads(bytes(geom))
        if tema == "predios":
            if forma.geom_type == "MultiPolygon":
                forma = max(forma.geoms, key=lambda p: p.area)
            if forma.geom_type == "Polygon":
                partes.append((altura, andares, classe, nome, np.asarray(forma.exterior.coords)[:-1, :2]))
        else:
            pedacos = list(forma.geoms) if forma.geom_type == "MultiLineString" else [forma]
            for pedaco in pedacos:
                if pedaco.geom_type == "LineString" and len(pedaco.coords) >= 2:
                    partes.append((altura, andares, classe, nome, np.asarray(pedaco.coords)[:, :2]))
    if not partes:
        return []

    para_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    todos = np.vstack([p[4] for p in partes])
    ex, ny_ = para_utm.transform(todos[:, 0], todos[:, 1])
    utm = np.column_stack([np.asarray(ex), np.asarray(ny_)])

    itens, inicio = [], 0
    for altura, andares, classe, nome, coords in partes:
        xy = utm[inicio: inicio + len(coords)]
        inicio += len(coords)
        if tema == "predios":
            area = 0.5 * abs(np.dot(xy[:, 0], np.roll(xy[:, 1], 1)) - np.dot(xy[:, 1], np.roll(xy[:, 0], 1)))
            if altura:
                h, estimada = float(altura), 0
            elif andares:
                h, estimada = float(andares) * settings.altura_por_andar_m, 0
            else:
                h, estimada = _estimar_altura(area), 1
            itens.append({"c": np.round(xy, 1).ravel().tolist(), "h": round(h, 1), "e": estimada,
                          "k": classe, "a": round(area)})
        else:
            simples = np.asarray(LineString(xy).simplify(ROAD_SIMPLIFY_M).coords)
            itens.append({"c": np.round(simples, 1).ravel().tolist(),
                          "w": ROAD_WIDTH_M.get(classe, DEFAULT_ROAD_WIDTH_M), "k": classe, "nome": nome})
    return itens


def _overture_tiles(tema: str, epsg: int, bounds: tuple[float, float, float, float]) -> tuple[list[dict], dict]:
    """Lê os ladrilhos de 10 km do cache, baixando de uma vez só os que faltam."""
    pasta = CACHE_DIR / "overture" / tema / str(epsg)
    chaves = _tiles_for(bounds, OVERTURE_TILE_M)
    faltando = [k for k in chaves if not (pasta / f"{k[0]}_{k[1]}.json").exists()]
    if faltando:
        pasta.mkdir(parents=True, exist_ok=True)
        e0 = min(tx for tx, _ in faltando) * OVERTURE_TILE_M
        n0 = min(ty for _, ty in faltando) * OVERTURE_TILE_M
        e1 = (max(tx for tx, _ in faltando) + 1) * OVERTURE_TILE_M
        n1 = (max(ty for _, ty in faltando) + 1) * OVERTURE_TILE_M
        por_ladrilho: dict[tuple[int, int], list[dict]] = {k: [] for k in faltando}
        for item in _consultar_overture(tema, epsg, (e0, n0, e1, n1)):
            c = item["c"]
            cx, cy = float(np.mean(c[0::2])), float(np.mean(c[1::2]))  # ladrilho pelo centro da feição
            chave = (math.floor(cx / OVERTURE_TILE_M), math.floor(cy / OVERTURE_TILE_M))
            if chave in por_ladrilho:
                por_ladrilho[chave].append(item)
        for chave, itens_chave in por_ladrilho.items():
            arquivo = pasta / f"{chave[0]}_{chave[1]}.json"
            arquivo.write_text(json.dumps({"itens": itens_chave}, ensure_ascii=False), encoding="utf-8")

    itens: list[dict] = []
    for tx, ty in chaves:
        arquivo = pasta / f"{tx}_{ty}.json"
        if arquivo.exists():
            itens += json.loads(arquivo.read_text(encoding="utf-8"))["itens"]
    return itens, {"total": len(chaves), "novos": len(faltando), "ladrilho_m": OVERTURE_TILE_M}


def _no_retangulo(item: dict, bounds: tuple[float, float, float, float]) -> bool:
    """O cache cobre ladrilhos de 10 km; para o navegador vai só o que toca a área da rede."""
    e0, n0, e1, n1 = bounds
    xs, ys = item["c"][0::2], item["c"][1::2]
    return max(xs) >= e0 and min(xs) <= e1 and max(ys) >= n0 and min(ys) <= n1


def _para_local(itens: list[dict], ox: float, oy: float) -> list[dict]:
    saida = []
    for item in itens:
        local = dict(item)
        local["c"] = [round(v - (ox if i % 2 == 0 else oy), 1) for i, v in enumerate(item["c"])]
        saida.append(local)
    return saida


def buildings(model: dict) -> dict:
    meta = model["meta"]
    bounds = _bounds_rede(model)
    itens, ladrilhos = _overture_tiles("predios", meta["epsg"], bounds)
    locais = _para_local([i for i in itens if _no_retangulo(i, bounds)],
                         meta["origem"]["x"], meta["origem"]["y"])
    return {
        "n": len(locais), "estimadas": sum(1 for b in locais if b["e"]), "itens": locais, "ladrilhos": ladrilhos,
        "fonte": "Overture Maps (Google Open Buildings, Microsoft ML Buildings, OpenStreetMap)",
    }


def roads(model: dict) -> dict:
    meta = model["meta"]
    bounds = _bounds_rede(model)
    itens, ladrilhos = _overture_tiles("ruas", meta["epsg"], bounds)
    locais = _para_local([i for i in itens if _no_retangulo(i, bounds)],
                         meta["origem"]["x"], meta["origem"]["y"])
    return {
        "n": len(locais), "itens": locais, "ladrilhos": ladrilhos,
        "fonte": "Overture Maps · transportation (OpenStreetMap)",
    }
