"""Importação e exportação de KML/KMZ.

Exportar: monta um KMZ com o que está visível na cena. As camadas e os circuitos desmarcados
não entram no arquivo — o KMZ é um retrato do que o usuário está vendo, não da base inteira.
As altitudes saem absolutas (relevo + altura do equipamento), então o Google Earth mostra a
rede na altura certa em vez de deitada no chão.

Importar: lê KML ou KMZ (que é um zip com o KML dentro), guarda as feições em lon/lat e as
converte para o referencial de cada rede na hora de desenhar.
"""
from __future__ import annotations

import base64
import math
import re
import struct
import zipfile
from functools import lru_cache
from io import BytesIO
from xml.etree import ElementTree

from pyproj import Transformer

from .config import settings

KML_NS = "http://www.opengis.net/kml/2.2"
MAX_COORD_POR_FEICAO = 20000


class KmlError(Exception):
    """Arquivo KML/KMZ ilegível ou sem feições aproveitáveis."""


@lru_cache(maxsize=8)
def _para_geo(epsg: int) -> Transformer:
    return Transformer.from_crs(epsg, 4326, always_xy=True)


@lru_cache(maxsize=8)
def _de_geo(epsg: int) -> Transformer:
    return Transformer.from_crs(4326, epsg, always_xy=True)


# --- relevo ------------------------------------------------------------------------------------
class Relevo:
    """Amostra a mesma grade de relevo que o navegador usa, para o KMZ sair na altura certa."""

    def __init__(self, terreno: dict | None):
        self.base = 0.0
        self.h = None
        if not terreno:
            return
        bruto = base64.b64decode(terreno["h"])
        self.h = struct.unpack(f"<{len(bruto) // 4}f", bruto)
        self.x0, self.y1 = terreno["x0"], terreno["y1"]
        self.dx, self.nx, self.ny = terreno["dx"], terreno["nx"], terreno["ny"]
        self.base = terreno.get("base", 0.0)

    def altura(self, x: float, y: float) -> float:
        """Cota absoluta (m) no ponto local (x, y), interpolada nos quatro vizinhos."""
        if self.h is None:
            return 0.0
        c = min(max((x - self.x0) / self.dx, 0), self.nx - 1)
        r = min(max((self.y1 - y) / self.dx, 0), self.ny - 1)
        c0 = min(int(c), self.nx - 2)
        r0 = min(int(r), self.ny - 2)
        fc, fr = c - c0, r - r0
        i = r0 * self.nx + c0
        topo = self.h[i] * (1 - fc) + self.h[i + 1] * fc
        baixo = self.h[i + self.nx] * (1 - fc) + self.h[i + self.nx + 1] * fc
        return self.base + topo * (1 - fr) + baixo * fr


# --- exportação --------------------------------------------------------------------------------
def _escapar(texto) -> str:
    return (str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _cor_kml(hexa: str, alpha: str = "ff") -> str:
    """#rrggbb do navegador -> aabbggrr do KML (o Google inverte os canais)."""
    h = (hexa or "#cccccc").lstrip("#")
    if len(h) != 6:
        h = "cccccc"
    return f"{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".lower()


class _Kml:
    """Montagem incremental do XML: evita guardar a rede inteira em memória duas vezes."""

    def __init__(self, nome: str, descricao: str = ""):
        self.partes = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<kml xmlns="{KML_NS}"><Document>',
            f"<name>{_escapar(nome)}</name>",
        ]
        if descricao:
            self.partes.append(f"<description>{_escapar(descricao)}</description>")
        self._estilos: set[str] = set()

    def estilo_linha(self, cor: str, largura: float = 2.4) -> str:
        id_ = f"l{_cor_kml(cor)}{int(largura * 10)}"
        if id_ not in self._estilos:
            self._estilos.add(id_)
            self.partes.append(
                f'<Style id="{id_}"><LineStyle><color>{_cor_kml(cor)}</color>'
                f"<width>{largura}</width></LineStyle></Style>")
        return id_

    def estilo_ponto(self, cor: str, escala: float = 0.9) -> str:
        id_ = f"p{_cor_kml(cor)}"
        if id_ not in self._estilos:
            self._estilos.add(id_)
            self.partes.append(
                f'<Style id="{id_}"><IconStyle><color>{_cor_kml(cor)}</color><scale>{escala}</scale>'
                '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>'
                "</IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>")
        return id_

    def abrir_pasta(self, nome: str, visivel: bool = True) -> None:
        self.partes.append(f"<Folder><name>{_escapar(nome)}</name><open>0</open>"
                           f"<visibility>{1 if visivel else 0}</visibility>")

    def fechar_pasta(self) -> None:
        self.partes.append("</Folder>")

    def ponto(self, nome: str, estilo: str, lon: float, lat: float, alt: float, props: list) -> None:
        self.partes.append(
            f"<Placemark><name>{_escapar(nome)}</name><styleUrl>#{estilo}</styleUrl>"
            f"{_tabela(props)}<Point><altitudeMode>absolute</altitudeMode>"
            f"<coordinates>{lon:.7f},{lat:.7f},{alt:.1f}</coordinates></Point></Placemark>")

    def linha(self, nome: str, estilo: str, coords: list, props: list) -> None:
        if len(coords) < 2:
            return
        texto = " ".join(f"{lon:.7f},{lat:.7f},{alt:.1f}" for lon, lat, alt in coords)
        self.partes.append(
            f"<Placemark><name>{_escapar(nome)}</name><styleUrl>#{estilo}</styleUrl>{_tabela(props)}"
            f"<LineString><altitudeMode>absolute</altitudeMode><tessellate>1</tessellate>"
            f"<coordinates>{texto}</coordinates></LineString></Placemark>")

    def poligono(self, nome: str, estilo: str, coords: list, props: list) -> None:
        if len(coords) < 3:
            return
        anel = coords + [coords[0]]
        texto = " ".join(f"{lon:.7f},{lat:.7f},{alt:.1f}" for lon, lat, alt in anel)
        self.partes.append(
            f"<Placemark><name>{_escapar(nome)}</name><styleUrl>#{estilo}</styleUrl>{_tabela(props)}"
            f"<Polygon><altitudeMode>absolute</altitudeMode><extrude>1</extrude><outerBoundaryIs>"
            f"<LinearRing><coordinates>{texto}</coordinates></LinearRing>"
            "</outerBoundaryIs></Polygon></Placemark>")

    def finalizar(self) -> str:
        return "".join([*self.partes, "</Document></kml>"])


def _tabela(props: list) -> str:
    if not props:
        return ""
    linhas = "".join(f"<tr><td><b>{_escapar(k)}</b></td><td>{_escapar(v)}</td></tr>" for k, v in props if v not in (None, ""))
    return f"<description><![CDATA[<table>{linhas}</table>]]></description>"


def exportar_kmz(model: dict, visiveis: set[str], circuitos: set | None = None,
                 veg: dict | None = None, camadas: list[dict] | None = None) -> bytes:
    """Monta o KMZ só com as camadas e os circuitos marcados na tela."""
    meta = model["meta"]
    epsg = meta.get("epsg") or settings.epsg
    ox, oy = meta["origem"]["x"], meta["origem"]["y"]
    geo = _para_geo(epsg)
    relevo = Relevo(model.get("terreno"))
    postes = model["postes"]
    circ_por_id = {c["id"]: c for c in model["circuitos"]}
    nome_circ = lambda cid: circ_por_id.get(cid, {}).get("codigo", "—")  # noqa: E731
    cor_circ = lambda cid: circ_por_id.get(cid, {}).get("cor", "#cccccc")  # noqa: E731

    def ponto_geo(x: float, y: float, h: float = 0.0):
        lon, lat = geo.transform(ox + x, oy + y)
        return lon, lat, relevo.altura(x, y) + h

    def circuito_visivel(cid) -> bool:
        return circuitos is None or cid is None or cid in circuitos

    arr = model.get("arranjo") or {}
    alt_cruzeta = max((f[1] for f in arr.get("fases", [[0, 8]])), default=8.0)
    kml = _Kml(meta.get("nome") or "Expansion 3D",
               f"Exportado do Expansion 3D · {len(visiveis)} camada(s) visível(is)")

    if "postes" in visiveis:
        kml.abrir_pasta("Postes")
        estilo = kml.estilo_ponto("#8d8d8d", 0.6)
        for p in postes:
            if not circuito_visivel(p.get("c")):
                continue
            lon, lat, alt = ponto_geo(p["x"], p["y"], alt_cruzeta + 1)
            kml.ponto(p.get("cod") or "Poste", estilo, lon, lat, alt,
                      [("Circuito", nome_circ(p.get("c"))), ("Vãos", p.get("grau")),
                       ("UTM E", round(ox + p["x"], 1)), ("UTM N", round(oy + p["y"], 1))])
        kml.fechar_pasta()

    if "condutores" in visiveis:
        kml.abrir_pasta("Condutores")
        por_circuito: dict = {}
        for v in model["vaos"]:
            if circuito_visivel(v.get("c")):
                por_circuito.setdefault(v.get("c"), []).append(v)
        for cid, vaos in por_circuito.items():
            kml.abrir_pasta(f"Circuito {nome_circ(cid)}")
            estilo = kml.estilo_linha(cor_circ(cid), 2.6)
            for v in vaos:
                a, b = postes[v["a"]], postes[v["b"]]
                coords = [ponto_geo(a["x"], a["y"], alt_cruzeta), ponto_geo(b["x"], b["y"], alt_cruzeta)]
                kml.linha(f"Trecho {v['id']}", estilo, coords, [
                    ("Circuito", nome_circ(cid)), ("Cabo", v.get("cabo")), ("Fases", v.get("f")),
                    ("Formação", "Spacer Cable" if v.get("spacer") else "Cabo nu em cruzeta"),
                    ("Comprimento", f"{v.get('l', 0):.1f} m"), ("Corrente admissível", v.get("iadm")),
                    ("De / para", f"{a.get('cod')} → {b.get('cod')}")])
            kml.fechar_pasta()
        kml.fechar_pasta()

    equipamentos = [
        ("trafos", "Transformadores (ET)", "#5b6b7a",
         [t for t in model["trafos"] if t.get("tipo") == "ET"], alt_cruzeta - 3),
        ("cabines", "Cabines de cliente MT (EP)", "#c7a24a",
         [t for t in model["trafos"] if t.get("tipo") == "EP"], 3.0),
        ("equip", "Reguladores e capacitores", "#7a4fd6",
         [*model["reguladores"], *model["capacitores"]], alt_cruzeta - 2),
        ("gd", "Geração distribuída", "#f2c230", model["geradores"], 2.0),
    ]
    for chave, rotulo, cor, itens, altura in equipamentos:
        if chave not in visiveis or not itens:
            continue
        kml.abrir_pasta(rotulo)
        estilo = kml.estilo_ponto(cor)
        for it in itens:
            if not circuito_visivel(it.get("c")):
                continue
            p = postes[it["p"]]
            lon, lat, alt = ponto_geo(p["x"], p["y"], altura)
            kml.ponto(it.get("cod") or rotulo, estilo, lon, lat, alt, [
                ("Circuito", nome_circ(it.get("c"))),
                ("Potência", it.get("snom") or it.get("qnom")),
                ("Poste", p.get("cod"))])
        kml.fechar_pasta()

    chaves = [("chaves_rl", "Religadoras (RL)", "RL"), ("chaves_cf", "Chaves faca (CF)", "CF"),
              ("chaves_outras", "Outras chaves", None)]
    for chave, rotulo, sigla in chaves:
        if chave not in visiveis:
            continue
        itens = [s for s in model["chaves"]
                 if (s.get("sigla") == sigla if sigla else s.get("sigla") not in ("RL", "CF"))]
        if not itens:
            continue
        kml.abrir_pasta(rotulo)
        for s in itens:
            if not circuito_visivel(s.get("c")):
                continue
            p = postes[s["p"]]
            lon, lat, alt = ponto_geo(p["x"], p["y"], alt_cruzeta - 1)
            estilo = kml.estilo_ponto("#e0483e" if s.get("estado") == 0 else "#2fb36b")
            kml.ponto(s.get("cod") or rotulo, estilo, lon, lat, alt, [
                ("Circuito", nome_circ(s.get("c"))), ("Tipo", s.get("tipo")),
                ("Estado", "Aberta" if s.get("estado") == 0 else "Fechada"), ("Poste", p.get("cod"))])
        kml.fechar_pasta()

    if "se" in visiveis and model["subestacoes"]:
        kml.abrir_pasta("Subestações")
        estilo = kml.estilo_ponto("#9b9a92", 1.3)
        for se in model["subestacoes"]:
            lon, lat, alt = ponto_geo(se["x"], se["y"], 8)
            kml.ponto(f"SE {se.get('nome') or se.get('cod')}", estilo, lon, lat, alt, [
                ("Código", se.get("cod")),
                ("Tensões", f"{se.get('valta')} / {se.get('vmedia')} kV"),
                ("Alimentadores", ", ".join(nome_circ(a["circ"]) for a in se.get("alimentadores", [])))])
        kml.fechar_pasta()

    if "interlig" in visiveis and model.get("interligacoes"):
        kml.abrir_pasta("Interligações (socorro)")
        estilo = kml.estilo_ponto("#ff4fd8")
        for t in model["interligacoes"]:
            lon, lat, alt = ponto_geo(t["x"], t["y"], alt_cruzeta + 5)
            kml.ponto(t.get("cod") or "Interligação", estilo, lon, lat, alt,
                      [("Alimentador", t.get("de")), ("Socorro por", t.get("para"))])
        kml.fechar_pasta()

    if "folga" in visiveis and veg:
        cores = {"contato": "#e0483e", "critico": "#ff8a3d", "atencao": "#f5d547"}
        rotulos = {"contato": "Contato provável", "critico": "Crítico", "atencao": "Atenção"}
        criticos = [r for r in veg.get("criticos", []) if circuito_visivel(r.get("c"))]
        if criticos:
            kml.abrir_pasta("Vãos com vegetação próxima")
            vao_por_id = {v["id"]: v for v in model["vaos"]}
            for r in criticos:
                v = vao_por_id.get(r["id"])
                if not v:
                    continue
                a, b = postes[v["a"]], postes[v["b"]]
                estilo = kml.estilo_linha(cores.get(r["classe"], "#e0483e"), 5)
                kml.linha(f"Trecho {r['id']} · {rotulos.get(r['classe'], r['classe'])}", estilo,
                          [ponto_geo(a["x"], a["y"], alt_cruzeta), ponto_geo(b["x"], b["y"], alt_cruzeta)],
                          [("Classe", rotulos.get(r["classe"], r["classe"])),
                           ("Folga mínima", f"{r.get('folga', 0):.1f} m"),
                           ("Circuito", nome_circ(r.get("c")))])
            kml.fechar_pasta()

    for camada in camadas or []:
        kml.abrir_pasta(f"Importado: {camada.get('nome', 'camada')}")
        for f in camada.get("feicoes", []):
            coords = [(lon, lat, (alt or 0) or relevo.altura(*_local(lon, lat, epsg, ox, oy)) + 1)
                      for lon, lat, alt in f["coords"]]
            props = [("Origem", camada.get("nome")), *(f.get("props") or [])]
            if f["tipo"] == "ponto" and coords:
                kml.ponto(f.get("nome") or "Ponto", kml.estilo_ponto(f.get("cor") or "#ffffff"),
                          *coords[0], props)
            elif f["tipo"] == "linha":
                kml.linha(f.get("nome") or "Linha", kml.estilo_linha(f.get("cor") or "#ffffff"), coords, props)
            else:
                kml.poligono(f.get("nome") or "Área", kml.estilo_linha(f.get("cor") or "#ffffff"), coords, props)
        kml.fechar_pasta()

    saida = BytesIO()
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("doc.kml", kml.finalizar())
    return saida.getvalue()


def _local(lon: float, lat: float, epsg: int, ox: float, oy: float) -> tuple[float, float]:
    x, y = _de_geo(epsg).transform(lon, lat)
    return x - ox, y - oy


# --- importação --------------------------------------------------------------------------------
def _sem_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _texto(elemento, nome: str) -> str:
    for filho in elemento:
        if _sem_ns(filho.tag) == nome and filho.text:
            return filho.text.strip()
    return ""


def _coordenadas(texto: str) -> list:
    """"lon,lat[,alt] lon,lat[,alt] …" -> [(lon, lat, alt), …], descartando o que não for número."""
    pontos = []
    for item in re.split(r"\s+", texto.strip()):
        if not item:
            continue
        partes = item.split(",")
        if len(partes) < 2:
            continue
        try:
            lon, lat = float(partes[0]), float(partes[1])
            alt = float(partes[2]) if len(partes) > 2 and partes[2] else 0.0
        except ValueError:
            continue
        if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
            pontos.append((lon, lat, alt))
        if len(pontos) >= MAX_COORD_POR_FEICAO:
            break
    return pontos


def _estilos(raiz) -> dict:
    """Mapa styleUrl -> cor #rrggbb, lendo Style e StyleMap do documento."""
    cores, mapas = {}, {}
    for el in raiz.iter():
        tag = _sem_ns(el.tag)
        id_ = el.get("id")
        if tag == "Style" and id_:
            for filho in el.iter():
                if _sem_ns(filho.tag) in ("LineStyle", "PolyStyle", "IconStyle"):
                    cor = _texto(filho, "color")
                    if len(cor) == 8:  # aabbggrr -> #rrggbb
                        cores[f"#{id_}"] = f"#{cor[6:8]}{cor[4:6]}{cor[2:4]}".lower()
                        break
        elif tag == "StyleMap" and id_:
            for par in el:
                if _sem_ns(par.tag) == "Pair" and _texto(par, "key") == "normal":
                    mapas[f"#{id_}"] = _texto(par, "styleUrl")
    for origem, destino in mapas.items():
        if destino in cores:
            cores[origem] = cores[destino]
    return cores


def _dados_extras(placemark) -> list:
    props = []
    for el in placemark.iter():
        if _sem_ns(el.tag) == "Data":
            nome = el.get("name") or ""
            valor = _texto(el, "value")
            if nome and valor:
                props.append((nome, valor))
    return props[:20]


def ler(conteudo: bytes, nome_arquivo: str) -> dict:
    """Lê KML ou KMZ e devolve as feições em lon/lat, prontas para guardar no acervo."""
    if len(conteudo) > settings.kml_max_mb * 1024 * 1024:
        raise KmlError(f"Arquivo maior que o limite de {settings.kml_max_mb} MB.")
    texto = conteudo
    if nome_arquivo.lower().endswith(".kmz") or conteudo[:2] == b"PK":
        try:
            with zipfile.ZipFile(BytesIO(conteudo)) as z:
                internos = [n for n in z.namelist() if n.lower().endswith(".kml")]
                if not internos:
                    raise KmlError("O KMZ não tem nenhum arquivo .kml dentro.")
                # doc.kml é o principal por convenção; senão, o primeiro .kml do pacote
                escolhido = next((n for n in internos if n.lower().endswith("doc.kml")), internos[0])
                texto = z.read(escolhido)
        except zipfile.BadZipFile as exc:
            raise KmlError("O arquivo parece um KMZ, mas não abriu como zip.") from exc

    try:
        raiz = ElementTree.fromstring(texto)
    except ElementTree.ParseError as exc:
        raise KmlError(f"XML inválido: {exc}") from exc

    cores = _estilos(raiz)
    feicoes, cortado = [], False
    for placemark in raiz.iter():
        if _sem_ns(placemark.tag) != "Placemark":
            continue
        if len(feicoes) >= settings.kml_max_feicoes:
            cortado = True
            break
        nome = _texto(placemark, "name")
        cor = cores.get(_texto(placemark, "styleUrl"))
        props = _dados_extras(placemark)
        for geo in placemark.iter():
            tag = _sem_ns(geo.tag)
            if tag not in ("Point", "LineString", "LinearRing", "Polygon", "Track"):
                continue
            if tag == "Polygon":
                continue  # os anéis do polígono entram pelo LinearRing de dentro
            bruto = ""
            for filho in geo.iter():
                if _sem_ns(filho.tag) == "coordinates" and filho.text:
                    bruto = filho.text
                    break
            coords = _coordenadas(bruto)
            if not coords:
                continue
            tipo = "ponto" if tag == "Point" else "area" if tag == "LinearRing" else "linha"
            feicoes.append({"tipo": tipo, "nome": nome, "cor": cor, "coords": coords, "props": props})

    if not feicoes:
        raise KmlError("Nenhum ponto, linha ou área encontrada no arquivo.")

    lons = [c[0] for f in feicoes for c in f["coords"]]
    lats = [c[1] for f in feicoes for c in f["coords"]]
    return {
        "feicoes": feicoes,
        "n": len(feicoes),
        "pontos": sum(len(f["coords"]) for f in feicoes),
        "bbox": [min(lons), min(lats), max(lons), max(lats)],
        "cortado": cortado,
        "tipos": {t: sum(1 for f in feicoes if f["tipo"] == t) for t in ("ponto", "linha", "area")},
    }


def para_rede(camada: dict, model: dict) -> dict:
    """Converte as feições de lon/lat para o referencial local da rede (x, y em metros)."""
    meta = model["meta"]
    epsg = meta.get("epsg") or settings.epsg
    ox, oy = meta["origem"]["x"], meta["origem"]["y"]
    transf = _de_geo(epsg)
    saida = []
    for f in camada["feicoes"]:
        pontos = []
        for lon, lat, alt in f["coords"]:
            x, y = transf.transform(lon, lat)
            pontos.append([round(x - ox, 2), round(y - oy, 2), round(alt, 2)])
        saida.append({"tipo": f["tipo"], "nome": f.get("nome") or "", "cor": f.get("cor"),
                      "props": f.get("props") or [], "coords": pontos})
    return {"feicoes": saida, "n": len(saida)}
