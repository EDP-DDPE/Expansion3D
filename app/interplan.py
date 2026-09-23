"""Leitura de uma base do Interplan (.mdb) e montagem do modelo 3D da rede de média tensão.

Convenções verificadas na base rede_SGO:
- BARRA.X/Y são coordenadas UTM em decímetros (distância entre barras / TRECHO.COMPR = 10);
- trechos de ~1 m ligam barras do mesmo poste (é onde o Interplan insere as chaves);
- CARGA com código "ET" é transformador de distribuição e "EP" é cabine de cliente MT;
- TRECHO/CARGA.FASES2: 1, 2, 3 = monofásico A, B, C; 13 = trifásico; 9 e 12 = bifásico.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import pyodbc
from pyproj import Transformer

from .config import settings

DEFAULT_PASSWORD = settings.senha
DEFAULT_EPSG = settings.epsg
COORD_SCALE = 0.1  # decímetros -> metros
SAME_POLE_MAX_M = 2.5
SE_NEIGHBORHOOD_M = 400.0
SE_YARD_OFFSET_M = 33.0
MAP_MARGIN_M = 250.0
MAX_MAP_TILES = settings.max_ladrilhos
CIRCUIT_COLORS = ["#3aa0ff", "#ff8a3d", "#34c38f", "#e05cd6", "#f5d547", "#4dd0e1", "#ff5c7a", "#9c7bff"]
PHASE_LABELS = {1: "A", 2: "B", 3: "C", 13: "ABC"}
# Bitolas (mm²) que na MT são cabo protegido (Spacer Cable): feixe em losango com mensageiro no topo.
SPACER_MM2 = settings.spacer_mm2


class InterplanError(Exception):
    """Base inexistente, protegida com outra senha ou fora do formato esperado."""


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, a: int) -> int:
        parent = self.parent
        parent.setdefault(a, a)
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _connect(path: str, password: str) -> pyodbc.Connection:
    conn_str = "DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=%s;PWD=%s;ReadOnly=1" % (path, password)
    try:
        return pyodbc.connect(conn_str)
    except pyodbc.Error as exc:
        raise InterplanError(f"Não foi possível abrir a base: {exc.args[-1]}") from exc


def _rows(cur: pyodbc.Cursor, sql: str, required: bool = False) -> list[dict]:
    # cursor.columns() quebra no driver do Access; os nomes vêm de cursor.description.
    try:
        cur.execute(sql)
    except pyodbc.Error as exc:
        if required:
            raise InterplanError(f"Base fora do formato Interplan esperado ({sql})") from exc
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _clean(code) -> str | None:
    return " ".join(str(code).split()) if code is not None else None


def _code_kind(code: str | None) -> str:
    c = (code or "").strip()
    return c[3:5] if len(c) >= 5 and c[:3].isdigit() else c[:2]


def _bitola(codigo: str | None) -> str | None:
    """Campo do meio do código do cabo ('1S:185 mm²:1/0 AWG' -> '185 mm²')."""
    parts = (codigo or "").split(":")
    return parts[1].strip() if len(parts) >= 2 and parts[1].strip() else None


def _is_spacer(bitola: str | None) -> bool:
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*mm", bitola or "")
    return bool(m) and float(m.group(1).replace(",", ".")) in SPACER_MM2


def _n_phases(fases: int | None) -> int:
    if fases == 13:
        return 3
    if fases in (1, 2, 3):
        return 1
    return 2


def _num(v, digits: int = 2):
    return round(float(v), digits) if v is not None else None


def build_model(path: str, password: str = DEFAULT_PASSWORD, epsg: int = DEFAULT_EPSG) -> dict:
    t0 = time.perf_counter()
    with closing(_connect(path, password)) as cn:
        cur = cn.cursor()
        subestacoes = _rows(cur, "SELECT SE_ID, CODIGO, NOME, X, Y, VALTA, VMEDIA FROM SE", required=True)
        circuitos = _rows(cur, "SELECT CIRC_ID, SE_ID, BARSE_ID, BARRA_ID, CODIGO, NOME, VNOM FROM CIRCUITO", required=True)
        barras = _rows(cur, "SELECT BARRA_ID, BLOCO_ID, CODIGO, EXTERN_ID, X, Y FROM BARRA", required=True)
        trechos = _rows(cur, "SELECT TRECHO_ID, BLOCO_ID, BARRA1_ID, BARRA2_ID, CABOF_ID, CABON_ID, FASES2, COMPR FROM TRECHO", required=True)
        bloco_circ = {r["BLOCO_ID"]: r["CIRC_ID"] for r in _rows(cur, "SELECT BLOCO_ID, CIRC_ID FROM BLOCO")}
        circ_info = {r["CIRC_ID"]: r for r in _rows(cur, "SELECT CIRC_ID, KM_REDE_MT, QTDE_CONS_MT, QTDE_CONS_BT FROM CIRCUITO_INFO")}
        # CABO_ID se repete entre TIPO 1 (cabos de MT) e TIPO 2 (BT multiplexado): a MT usa o TIPO 1.
        cabos_all = _rows(cur, "SELECT CABO_ID, TIPO, CODIGO, IADM FROM CABOS")
        cabos = {r["CABO_ID"]: r for r in cabos_all if r["TIPO"] != 1}
        cabos.update({r["CABO_ID"]: r for r in cabos_all if r["TIPO"] == 1})
        arranjos = _rows(cur, "SELECT * FROM ARRANJO")
        tipos_chave = {r["ID"]: r for r in _rows(cur, "SELECT ID, TIPO, CODIGO FROM TIPOCHAVE")}
        chaves = _rows(cur, "SELECT CHAVE_ID, TIPOCHV_ID, CIRC_ID, TRECHO_ID, CODIGO, ESTADO, ESTADO_NORMAL, CORN, ELO FROM CHAVE")
        cargas = _rows(cur, "SELECT CARGA_ID, BARRA_ID, CODIGO, SNOM, FASES2, VLINHASEC, IP_NUMLAMP FROM CARGA")
        mt_cons = _rows(cur, "SELECT ID, CARGA_ID, CODIGO, NOME, CONSUMO, CLASSE FROM MT_CONS")
        gd_mt = _rows(cur, "SELECT GERADOR_ID, MT_CONS_ID, CODIGO, SNOM, VNOM, TIPOGD FROM MT_GERADOR_CONS")
        gd_barra = _rows(cur, "SELECT GERADOR_ID, BARRA_ID, CODIGO, SNOM, VNOM FROM GERADOR")
        reguladores = _rows(cur, "SELECT REGU_ID, TRECHO_ID, CODIGO, SNOM, INOM, VNOM, NPASSOS, FAIXA FROM REGULADOR")
        capacitores = _rows(cur, "SELECT CAPAC_ID, BARRA_ID, CODIGO, QNOM, VNOM FROM CAPACITOR")
        se_trafos = _rows(cur, "SELECT SE_ID, CODIGO, VALTA, VMEDIA, SNOM FROM SE_TRAFO")
        se_chaves = _rows(cur, "SELECT SE_ID, BARRA2_ID, CODIGO, ESTADO FROM SE_CHAVE")
        socorros = _rows(cur, "SELECT CODIGO, CIRC1_COD, CIRC2_COD, BAR1_CX, BAR1_CY, CORN, TIPO FROM SOCORRO_EXT")

    # --- barras georreferenciadas -------------------------------------------------------
    pos: dict[int, tuple[float, float]] = {}
    bar_circ: dict[int, int | None] = {}
    bar_code: dict[int, str] = {}
    for b in barras:
        if not b["X"] or not b["Y"] or b["X"] <= 0 or b["Y"] <= 0:
            continue
        bid = b["BARRA_ID"]
        pos[bid] = (b["X"] * COORD_SCALE, b["Y"] * COORD_SCALE)
        bar_circ[bid] = bloco_circ.get(b["BLOCO_ID"])
        bar_code[bid] = str(b["EXTERN_ID"] or b["CODIGO"] or bid)
    if not pos:
        raise InterplanError("A base não possui barras com coordenadas.")

    # --- postes: barras coincidentes ou ligadas por trechos curtos -----------------------
    uf = _UnionFind()
    seen_xy: dict[tuple[float, float], int] = {}
    for bid, (e, n) in pos.items():
        uf.find(bid)
        key = (round(e, 1), round(n, 1))
        if key in seen_xy:
            uf.union(seen_xy[key], bid)
        else:
            seen_xy[key] = bid
    for t in trechos:
        a, b = t["BARRA1_ID"], t["BARRA2_ID"]
        if a in pos and b in pos and math.dist(pos[a], pos[b]) <= SAME_POLE_MAX_M:
            uf.union(a, b)

    groups: dict[int, list[int]] = defaultdict(list)
    for bid in pos:
        groups[uf.find(bid)].append(bid)

    es = [e for e, _ in pos.values()]
    ns = [n for _, n in pos.values()]
    ox, oy = round((min(es) + max(es)) / 2), round((min(ns) + max(ns)) / 2)

    pole_of: dict[int, int] = {}
    postes: list[dict] = []
    for i, members in enumerate(sorted(groups.values(), key=min)):
        for bid in members:
            pole_of[bid] = i
        circs = Counter(bar_circ[b] for b in members if bar_circ[b] is not None)
        code_bar = min(members, key=lambda b: (bar_code[b].endswith("_INI"), b))
        postes.append({
            "i": i,
            "x": round(sum(pos[b][0] for b in members) / len(members) - ox, 2),
            "y": round(sum(pos[b][1] for b in members) / len(members) - oy, 2),
            "c": circs.most_common(1)[0][0] if circs else None,
            "cod": bar_code[code_bar],
            "barras": len(members),
        })

    trecho_by_id = {t["TRECHO_ID"]: t for t in trechos}

    def trecho_circ(t: dict) -> int | None:
        return bloco_circ.get(t["BLOCO_ID"]) or bar_circ.get(t["BARRA1_ID"]) or bar_circ.get(t["BARRA2_ID"])

    # --- vãos entre postes -------------------------------------------------------------
    vaos: list[dict] = []
    pair_count: Counter = Counter()
    for t in trechos:
        a, b = pole_of.get(t["BARRA1_ID"]), pole_of.get(t["BARRA2_ID"])
        if a is None or b is None or a == b:
            continue
        pair = (min(a, b), max(a, b))
        cabo = cabos.get(t["CABOF_ID"], {})
        bitola = _bitola(cabo.get("CODIGO"))
        vaos.append({
            "id": t["TRECHO_ID"],
            "a": a,
            "b": b,
            "c": trecho_circ(t),
            "nf": _n_phases(t["FASES2"]),
            "f": PHASE_LABELS.get(t["FASES2"], f"bifásico ({t['FASES2']})"),
            "cabo": cabo.get("CODIGO"),
            "bitola": bitola,
            "spacer": _is_spacer(bitola),
            "iadm": _num(cabo.get("IADM"), 0),
            "l": _num(t["COMPR"], 1),
            "lvl": pair_count[pair],
            "neutro": (t["CABON_ID"] or -1) > 0,
        })
        pair_count[pair] += 1

    # Orientação da cruzeta: média (em ângulo dobrado) das direções dos vãos no poste.
    acc: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    grau: Counter = Counter()
    for v in vaos:
        pa, pb = postes[v["a"]], postes[v["b"]]
        ang = math.atan2(pb["y"] - pa["y"], pb["x"] - pa["x"])
        for p in (v["a"], v["b"]):
            acc[p][0] += math.cos(2 * ang)
            acc[p][1] += math.sin(2 * ang)
            grau[p] += 1
            if postes[p]["c"] is None:
                postes[p]["c"] = v["c"]
    for p in postes:
        c, s = acc.get(p["i"], (0.0, 0.0))
        p["rot"] = round(math.atan2(s, c) / 2, 3)
        p["grau"] = grau[p["i"]]

    # --- equipamentos ------------------------------------------------------------------
    stack: Counter = Counter()

    def slot(kind: str, p: int) -> int:
        k = stack[(kind, p)]
        stack[(kind, p)] += 1
        return k

    cons_by_carga: dict[int, list[dict]] = defaultdict(list)
    cons_by_id: dict[int, dict] = {}
    for m in mt_cons:
        cons_by_carga[m["CARGA_ID"]].append(m)
        cons_by_id[m["ID"]] = m

    carga_by_id = {cg["CARGA_ID"]: cg for cg in cargas}
    trafos: list[dict] = []
    for cg in cargas:
        p = pole_of.get(cg["BARRA_ID"])
        if p is None:
            continue
        tipo = "EP" if _code_kind(cg["CODIGO"]) == "EP" else "ET"
        trafos.append({
            "id": cg["CARGA_ID"],
            "cod": _clean(cg["CODIGO"]),
            "tipo": tipo,
            "p": p,
            "c": bar_circ.get(cg["BARRA_ID"]) or postes[p]["c"],
            "k": slot(tipo, p),
            "snom": _num(cg["SNOM"], 1),
            "fases": PHASE_LABELS.get(cg["FASES2"], f"bifásico ({cg['FASES2']})"),
            "vsec": _num(cg["VLINHASEC"], 0),
            "lampadas": cg["IP_NUMLAMP"] or 0,
            "consumidores": [
                {"cod": _clean(m["CODIGO"]), "nome": _clean(m["NOME"]), "kwh": _num(m["CONSUMO"], 0), "classe": m["CLASSE"]}
                for m in cons_by_carga.get(cg["CARGA_ID"], [])
            ],
        })

    geradores: list[dict] = []
    for g in gd_mt:
        cons = cons_by_id.get(g["MT_CONS_ID"])
        carga = carga_by_id.get(cons["CARGA_ID"]) if cons else None
        p = pole_of.get(carga["BARRA_ID"]) if carga else None
        if p is None:
            continue
        geradores.append({
            "id": g["GERADOR_ID"], "cod": _clean(g["CODIGO"]), "p": p, "c": postes[p]["c"], "k": slot("GD", p),
            "snom": _num(g["SNOM"], 1), "vnom": _num(g["VNOM"], 1),
            "cliente": _clean(cons["NOME"]), "carga": _clean(carga["CODIGO"]),
        })
    for g in gd_barra:
        p = pole_of.get(g["BARRA_ID"])
        if p is None:
            continue
        geradores.append({
            "id": g["GERADOR_ID"], "cod": _clean(g["CODIGO"]), "p": p, "c": postes[p]["c"], "k": slot("GD", p),
            "snom": _num(g["SNOM"], 1), "vnom": _num(g["VNOM"], 1), "cliente": None, "carga": None,
        })

    def trecho_pole(tid: int) -> int | None:
        t = trecho_by_id.get(tid)
        return pole_of.get(t["BARRA1_ID"]) if t else None

    lista_chaves: list[dict] = []
    for ch in chaves:
        p = trecho_pole(ch["TRECHO_ID"])
        if p is None:
            continue
        tipo = tipos_chave.get(ch["TIPOCHV_ID"], {})
        lista_chaves.append({
            "id": ch["CHAVE_ID"], "cod": _clean(ch["CODIGO"]), "p": p, "c": ch["CIRC_ID"] or postes[p]["c"],
            "k": slot("CH", p), "tipo": tipo.get("TIPO", "Chave"), "sigla": tipo.get("CODIGO"),
            "estado": ch["ESTADO"], "normal": ch["ESTADO_NORMAL"],
            "corrente": _num(ch["CORN"], 0), "elo": ch["ELO"] or None,
        })

    lista_reg: list[dict] = []
    for r in reguladores:
        p = trecho_pole(r["TRECHO_ID"])
        if p is None:
            continue
        lista_reg.append({
            "id": r["REGU_ID"], "cod": _clean(r["CODIGO"]), "p": p, "c": postes[p]["c"],
            "snom": _num(r["SNOM"], 1), "inom": _num(r["INOM"], 0), "vnom": _num(r["VNOM"], 2),
            "passos": r["NPASSOS"], "faixa": _num(r["FAIXA"], 3),
        })

    lista_cap: list[dict] = []
    for c in capacitores:
        p = pole_of.get(c["BARRA_ID"])
        if p is None:
            continue
        lista_cap.append({
            "id": c["CAPAC_ID"], "cod": _clean(c["CODIGO"]), "p": p, "c": postes[p]["c"],
            "qnom": _num(c["QNOM"], 0), "vnom": _num(c["VNOM"], 2),
        })

    # --- circuitos e subestações ---------------------------------------------------------
    km: Counter = Counter()
    for v in vaos:
        km[v["c"]] += v["l"] or 0

    lista_circ: list[dict] = []
    breaker_by_bar = {sc["BARRA2_ID"]: sc for sc in se_chaves}
    for i, c in enumerate(sorted(circuitos, key=lambda r: str(r["CODIGO"]))):
        info = circ_info.get(c["CIRC_ID"], {})
        lista_circ.append({
            "id": c["CIRC_ID"], "codigo": _clean(c["CODIGO"]), "nome": _clean(c["NOME"]), "se": c["SE_ID"],
            "vnom": _num(c["VNOM"], 2), "cor": CIRCUIT_COLORS[i % len(CIRCUIT_COLORS)],
            "poste0": pole_of.get(c["BARRA_ID"]), "km": round(km[c["CIRC_ID"]] / 1000, 2),
            "consMT": info.get("QTDE_CONS_MT"), "consBT": info.get("QTDE_CONS_BT"),
            "_barse": c["BARSE_ID"],
        })

    lista_se: list[dict] = []
    for se in subestacoes:
        if not se["X"] or not se["Y"]:
            continue
        sx, sy = se["X"] * COORD_SCALE - ox, se["Y"] * COORD_SCALE - oy
        near = [p for p in postes if math.hypot(p["x"] - sx, p["y"] - sy) <= SE_NEIGHBORHOOD_M]
        rot, cx, cy = 0.0, sx, sy
        if near:
            dx = sx - sum(p["x"] for p in near) / len(near)
            dy = sy - sum(p["y"] for p in near) / len(near)
            if math.hypot(dx, dy) > 1:
                rot = math.atan2(dy, dx)
                # o pátio fica do lado oposto à rede, com a saída dos alimentadores voltada para ela
                cx, cy = sx + math.cos(rot) * SE_YARD_OFFSET_M, sy + math.sin(rot) * SE_YARD_OFFSET_M
        alimentadores = []
        for c in lista_circ:
            if c["se"] != se["SE_ID"]:
                continue
            dj = breaker_by_bar.get(c["_barse"], {})
            alimentadores.append({"circ": c["id"], "disjuntor": _clean(dj.get("CODIGO")), "estado": dj.get("ESTADO")})
        lista_se.append({
            # há bases (ES) em que a SE não tem nome preenchido; o código serve de rótulo
            "id": se["SE_ID"], "cod": _clean(se["CODIGO"]), "nome": _clean(se["NOME"]) or _clean(se["CODIGO"]),
            "x": round(cx, 2), "y": round(cy, 2), "rot": round(rot, 3),
            "valta": _num(se["VALTA"], 1), "vmedia": _num(se["VMEDIA"], 1),
            "trafos": [
                {"cod": _clean(t["CODIGO"]), "snom": _num(t["SNOM"], 1), "valta": _num(t["VALTA"], 1), "vmedia": _num(t["VMEDIA"], 1)}
                for t in se_trafos if t["SE_ID"] == se["SE_ID"]
            ],
            "alimentadores": alimentadores,
        })
    for c in lista_circ:
        del c["_barse"]

    xs = [p["x"] for p in postes]
    ys = [p["y"] for p in postes]
    bbox = [min(xs), min(ys), max(xs), max(ys)]

    interligacoes = []
    for s in socorros:
        if not s["BAR1_CX"] or not s["BAR1_CY"]:
            continue
        x, y = s["BAR1_CX"] * COORD_SCALE - ox, s["BAR1_CY"] * COORD_SCALE - oy
        if not (bbox[0] - 500 <= x <= bbox[2] + 500 and bbox[1] - 500 <= y <= bbox[3] + 500):
            continue
        tipo = tipos_chave.get(int(s["TIPO"])) if str(s["TIPO"] or "").isdigit() else None
        interligacoes.append({
            "cod": _clean(s["CODIGO"]), "x": round(x, 2), "y": round(y, 2),
            "de": _clean(s["CIRC1_COD"]), "para": _clean(s["CIRC2_COD"]),
            "corrente": _num(s["CORN"], 0), "tipo": tipo["TIPO"] if tipo else None,
        })

    # --- arranjo de condutores (cruzeta MT) ------------------------------------------------
    mt = next((a for a in arranjos if (a.get("REDE") or "").upper() == "MT"), None)
    fases = [(mt[f"FASE{k}_X"], mt[f"FASE{k}_Y"]) for k in (1, 2, 3)] if mt else [(0.22, 10.0), (0.92, 10.0), (1.66, 10.0)]
    centro = sum(x for x, _ in fases) / 3
    neutro = (mt["FASE4_X"], mt["FASE4_Y"]) if mt else (0.0, 6.0)
    arranjo = {
        "fases": [[round(x - centro, 3), round(h, 2)] for x, h in fases],
        "neutro": [0.0, round(neutro[1], 2)],
    }

    # --- geografia e mapa base -----------------------------------------------------------
    to_geo = Transformer.from_crs(epsg, 4326, always_xy=True)
    lon, lat = to_geo.transform(ox, oy)
    mapa = _map_tiles(epsg, ox, oy, bbox)

    kva = sum(t["snom"] or 0 for t in trafos if t["tipo"] == "ET")
    return {
        "meta": {
            "arquivo": str(Path(path)),
            "nome": Path(path).parent.parent.name if Path(path).stem.lower() == "rede" else Path(path).stem,
            "epsg": epsg,
            "origem": {"x": ox, "y": oy},
            "lonlat": [round(lon, 6), round(lat, 6)],
            "bbox": bbox,
            "tempo_s": round(time.perf_counter() - t0, 2),
            "resumo": {
                "circuitos": len(lista_circ),
                "postes": len(postes),
                "vaos": len(vaos),
                "km": round(sum(km.values()) / 1000, 2),
                "trafos": sum(1 for t in trafos if t["tipo"] == "ET"),
                "kva": round(kva, 1),
                "cabines": sum(1 for t in trafos if t["tipo"] == "EP"),
                "chaves": len(lista_chaves),
                "chaves_abertas": sum(1 for c in lista_chaves if c["estado"] == 0),
                "reguladores": len(lista_reg),
                "capacitores": len(lista_cap),
                "geradores": len(geradores),
                "gd_kva": round(sum(g["snom"] or 0 for g in geradores), 1),
            },
        },
        "arranjo": arranjo,
        "subestacoes": lista_se,
        "circuitos": lista_circ,
        "postes": postes,
        "vaos": vaos,
        "trafos": trafos,
        "chaves": lista_chaves,
        "reguladores": lista_reg,
        "capacitores": lista_cap,
        "geradores": geradores,
        "interligacoes": interligacoes,
        "mapa": mapa,
    }


def _map_tiles(epsg: int, ox: float, oy: float, bbox: list[float]) -> dict:
    """Ladrilhos XYZ (Web Mercator) que cobrem a rede, com os cantos já em coordenadas locais."""
    to_geo = Transformer.from_crs(epsg, 4326, always_xy=True)
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    lon0, lat0 = to_geo.transform(ox + bbox[0] - MAP_MARGIN_M, oy + bbox[1] - MAP_MARGIN_M)
    lon1, lat1 = to_geo.transform(ox + bbox[2] + MAP_MARGIN_M, oy + bbox[3] + MAP_MARGIN_M)

    def tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
        n = 2 ** z
        return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

    def tile_corner(x: int, y: int, z: int) -> tuple[float, float]:
        n = 2 ** z
        lon = x / n * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        e, nn = to_utm.transform(lon, lat)
        return round(e - ox, 2), round(nn - oy, 2)

    for z in range(18, 9, -1):
        tx0, ty0 = tile_xy(lon0, lat1, z)
        tx1, ty1 = tile_xy(lon1, lat0, z)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= MAX_MAP_TILES:
            break

    tiles = []
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            nw, ne = tile_corner(tx, ty, z), tile_corner(tx + 1, ty, z)
            se, sw = tile_corner(tx + 1, ty + 1, z), tile_corner(tx, ty + 1, z)
            tiles.append({"x": tx, "y": ty, "c": [*nw, *ne, *se, *sw]})
    return {"z": z, "tiles": tiles}
