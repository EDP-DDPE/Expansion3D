"""Teste da leitura de KML/KMZ e da montagem do KMZ exportado. Rode: python tools/teste_kml.py"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import kml  # noqa: E402

EXEMPLO = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Style id="vermelho"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>
  <StyleMap id="mapa"><Pair><key>normal</key><styleUrl>#vermelho</styleUrl></Pair></StyleMap>
  <Folder><name>Obra</name>
    <Placemark><name>Poste novo</name><styleUrl>#vermelho</styleUrl>
      <ExtendedData><Data name="Etapa"><value>1</value></Data></ExtendedData>
      <Point><coordinates>-46.6333,-23.5505,760</coordinates></Point></Placemark>
    <Placemark><name>Trecho projetado</name><styleUrl>#mapa</styleUrl>
      <LineString><coordinates>-46.6333,-23.5505,760 -46.6320,-23.5500,762 -46.6310,-23.5495,765
      </coordinates></LineString></Placemark>
    <Placemark><name>Area</name>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>
        -46.634,-23.551,0 -46.633,-23.551,0 -46.633,-23.550,0 -46.634,-23.551,0
      </coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
    <Placemark><name>Sem geometria</name></Placemark>
  </Folder>
</Document></kml>"""

falhas = 0


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    if not condicao:
        falhas += 1
    print(f"{'ok  ' if condicao else 'FALHA'} {rotulo}{f' · {detalhe}' if detalhe else ''}")


def main() -> int:
    lido = kml.ler(EXEMPLO.encode(), "obra.kml")
    conferir("3 feições lidas", lido["n"] == 3, f"n={lido['n']} tipos={lido['tipos']}")
    conferir("tipos separados", lido["tipos"] == {"ponto": 1, "linha": 1, "area": 1})
    ponto = lido["feicoes"][0]
    conferir("cor do Style lida", ponto["cor"] == "#ff0000", f"cor={ponto['cor']}")
    conferir("ExtendedData lido", ponto["props"] == [("Etapa", "1")], f"props={ponto['props']}")
    linha = lido["feicoes"][1]
    conferir("StyleMap resolvido", linha["cor"] == "#ff0000", f"cor={linha['cor']}")
    conferir("linha com 3 pontos", len(linha["coords"]) == 3)
    conferir("bbox coerente", lido["bbox"][0] < lido["bbox"][2] and lido["bbox"][1] < lido["bbox"][3])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", EXEMPLO)
        z.writestr("imagens/logo.png", b"\x89PNG")
    kmz = kml.ler(buf.getvalue(), "obra.kmz")
    conferir("KMZ dá o mesmo que o KML", kmz["n"] == lido["n"] and kmz["tipos"] == lido["tipos"])

    for rotulo, dados, arquivo in (
        ("XML inválido", b"isto nao e xml", "x.kml"),
        ("KMZ corrompido", b"PK\x03\x04lixo", "x.kmz"),
        ("documento sem feições", b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2">'
                                  b"<Document/></kml>", "x.kml"),
    ):
        try:
            kml.ler(dados, arquivo)
            conferir(f"rejeita {rotulo}", False, "aceitou")
        except kml.KmlError as exc:
            conferir(f"rejeita {rotulo}", True, str(exc))

    # exportação: rede mínima, com relevo, conferindo que camada desmarcada não entra
    modelo = {
        "meta": {"nome": "Rede de teste", "epsg": 31983, "origem": {"x": 330000.0, "y": 7390000.0}},
        "postes": [{"cod": "P1", "x": 0.0, "y": 0.0, "c": 1, "grau": 1},
                   {"cod": "P2", "x": 40.0, "y": 0.0, "c": 1, "grau": 1}],
        "vaos": [{"id": 10, "a": 0, "b": 1, "c": 1, "cabo": "1S:185 mm²", "f": "ABC", "l": 40.0,
                  "spacer": True, "iadm": 350}],
        "circuitos": [{"id": 1, "codigo": "ALI-01", "cor": "#3aa0ff"}],
        "trafos": [{"cod": "T1", "tipo": "ET", "p": 0, "c": 1, "snom": 45}],
        "chaves": [{"cod": "CH1", "sigla": "RL", "tipo": "Religadora", "p": 1, "c": 1, "estado": 0}],
        "reguladores": [], "capacitores": [], "geradores": [], "interligacoes": [],
        "subestacoes": [{"cod": "SE1", "nome": "Teste", "x": -100.0, "y": 50.0, "valta": 138,
                         "vmedia": 13.8, "alimentadores": [{"circ": 1}]}],
        "arranjo": {"fases": [[-1.0, 8.5], [0.0, 9.0], [1.0, 8.5]], "neutro": [0.0, 6.0]},
        "terreno": None,
    }
    todas = {"postes", "condutores", "trafos", "chaves_rl", "se"}
    bruto = kml.exportar_kmz(modelo, todas)
    with zipfile.ZipFile(io.BytesIO(bruto)) as z:
        doc = z.read("doc.kml").decode()
    conferir("KMZ é um zip com doc.kml", "<kml" in doc and doc.startswith("<?xml"))
    for esperado in ("P1", "Trecho 10", "T1", "CH1", "SE Teste", "Spacer Cable"):
        conferir(f"KMZ contém {esperado}", esperado in doc)

    parcial = kml.exportar_kmz(modelo, {"postes"})
    with zipfile.ZipFile(io.BytesIO(parcial)) as z:
        doc_parcial = z.read("doc.kml").decode()
    conferir("camada desmarcada fica fora", "Trecho 10" not in doc_parcial and "T1" not in doc_parcial)
    conferir("camada marcada entra", "P1" in doc_parcial)

    sem_circuito = kml.exportar_kmz(modelo, todas, circuitos=set())
    with zipfile.ZipFile(io.BytesIO(sem_circuito)) as z:
        doc_sem = z.read("doc.kml").decode()
    conferir("circuito desmarcado fica fora", "P1" not in doc_sem and "Trecho 10" not in doc_sem)

    print("\nresultado:", "tudo certo" if not falhas else f"{falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
