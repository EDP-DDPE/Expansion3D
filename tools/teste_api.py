"""Teste das regras de acesso da API: modo Visualização, redes privadas e exportação.

Sobe o serviço numa porta própria e conversa com ele por HTTP de verdade, como o navegador faz.
Use: python tools/teste_api.py
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from app import catalogo, sessao  # noqa: E402
from app.config import settings  # noqa: E402
from app.server import app  # noqa: E402

PORTA = 8019
BASE = f"http://127.0.0.1:{PORTA}"
falhas = 0


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    if not condicao:
        falhas += 1
    print(f"{'ok  ' if condicao else 'FALHA'} {rotulo}{f' · {detalhe}' if detalhe else ''}")


def cookie_de(matricula: str, nome: str, admin: bool = False) -> str:
    return sessao.criar_cookie(sessao.Usuario(matricula=matricula, nome=nome, admin=admin))


class Cliente:
    """Cliente HTTP mínimo (stdlib) que guarda o cookie de sessão."""

    def __init__(self, cookie: str | None = None):
        self.cookie = cookie

    def pedir(self, metodo: str, caminho: str, corpo=None, arquivo=None):
        req = urllib.request.Request(f"{BASE}{caminho}", method=metodo)
        if self.cookie:
            req.add_header("Cookie", f"{sessao.COOKIE}={self.cookie}")
        dados = None
        if corpo is not None:
            dados = json.dumps(corpo).encode()
            req.add_header("Content-Type", "application/json")
        elif arquivo is not None:
            nome, conteudo, campos = arquivo
            limite = "----expansion3dteste"
            partes = []
            for chave, valor in campos.items():
                partes.append(f"--{limite}\r\nContent-Disposition: form-data; name=\"{chave}\"\r\n\r\n{valor}\r\n".encode())
            partes.append(f"--{limite}\r\nContent-Disposition: form-data; name=\"arquivo\"; "
                          f"filename=\"{nome}\"\r\n\r\n".encode() + conteudo + b"\r\n")
            partes.append(f"--{limite}--\r\n".encode())
            dados = b"".join(partes)
            req.add_header("Content-Type", f"multipart/form-data; boundary={limite}")
        try:
            with urllib.request.urlopen(req, dados, timeout=600) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, err.read(), dict(err.headers)

    def json(self, caminho: str):
        return json.loads(self.pedir("GET", caminho)[1])

    def get(self, caminho: str):
        return self.pedir("GET", caminho)

    def post(self, caminho: str, corpo=None, arquivo=None):
        return self.pedir("POST", caminho, corpo, arquivo)

    def delete(self, caminho: str):
        return self.pedir("DELETE", caminho)


def subir_servico():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORTA, log_level="warning")
    servidor = uvicorn.Server(config)
    threading.Thread(target=servidor.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{BASE}/api/config", timeout=1).read()
            return servidor
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError("o serviço de teste não subiu")


def main() -> int:
    if not settings.sso_pronto:
        print("A integração com o Atlas está desligada no conf.ini; este teste precisa dela ligada.")
        return 1

    servidor = subir_servico()
    try:
        visitante = Cliente()
        dono = Cliente(cookie_de("7034", "Dono"))
        outro = Cliente(cookie_de("9999", "Outro"))
        chefe = Cliente(cookie_de("0001", "Administrador", admin=True))

        # --- sessão -------------------------------------------------------------------------
        conferir("visitante sem usuário", visitante.json("/api/sessao")["usuario"] is None)
        conferir("logado é reconhecido", dono.json("/api/sessao")["usuario"]["matricula"] == "7034")
        forjado = Cliente("corpo.assinaturafalsa")
        conferir("cookie forjado é ignorado", forjado.json("/api/sessao")["usuario"] is None)

        # --- modo Visualização ---------------------------------------------------------------
        status, _, _ = visitante.post("/api/acervo", arquivo=("x.mdb", b"123", {"regiao": "SP"}))
        conferir("visitante não envia rede", status == 401, f"HTTP {status}")
        status, _, _ = visitante.post("/api/camadas", arquivo=("x.kml", b"123", {}))
        conferir("visitante não importa KML", status == 401, f"HTTP {status}")
        status, _, _ = visitante.post("/api/projetos", corpo={"nome": "Obra"})
        conferir("visitante não cria projeto", status == 401, f"HTTP {status}")

        publicas = visitante.json("/api/acervo")["redes"]
        conferir("visitante vê as redes públicas", len(publicas) > 0, f"{len(publicas)} rede(s)")
        if not publicas:
            print("Sem rede pública no acervo: o resto do teste precisa de pelo menos uma.")
            return 1
        rede = publicas[0]

        # --- rede privada ---------------------------------------------------------------------
        privada = catalogo.adicionar("hash-teste-api", "privada.mdb", 10, "Dono", "teste",
                                     {"subestacoes": [], "circuitos": [], "numeros": {}},
                                     rede["regiao"], rede["epsg"], catalogo.PRIVADA, "7034")
        try:
            ids_visitante = {x["id"] for x in visitante.json("/api/acervo")["redes"]}
            ids_outro = {x["id"] for x in outro.json("/api/acervo")["redes"]}
            ids_dono = {x["id"] for x in dono.json("/api/acervo")["redes"]}
            conferir("privada escondida do visitante", privada["id"] not in ids_visitante)
            conferir("privada escondida de outro usuário", privada["id"] not in ids_outro)
            conferir("privada visível para o dono", privada["id"] in ids_dono)

            status, _, _ = outro.get(f"/api/rede?id={privada['id']}")
            conferir("privada dá 404 por id direto", status == 404, f"HTTP {status}")
            status, _, _ = outro.delete(f"/api/acervo/{privada['id']}")
            conferir("outro não exclui a privada", status == 404, f"HTTP {status}")
        finally:
            catalogo.remover(privada["id"])
        conferir("privada de teste removida", catalogo.obter_bruto(privada["id"]) is None)

        # --- exportação KMZ ---------------------------------------------------------------------
        t0 = time.perf_counter()
        status, conteudo, cabecalhos = visitante.post(
            "/api/exportar/kmz", corpo={"id": rede["id"], "camadas": ["postes", "condutores"]})
        conferir("visitante exporta rede pública", status == 200, f"HTTP {status}")
        if status == 200:
            with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
                doc = z.read("doc.kml").decode()
            conferir("KMZ tem postes e condutores", "<name>Postes" in doc and "<name>Condutores" in doc)
            conferir("KMZ não traz camada desmarcada", "Transformadores (ET)" not in doc)
            conferir("KMZ é anexo com nome", "attachment" in cabecalhos.get("content-disposition", ""))
            print(f"      KMZ de {len(conteudo) / 1024:.0f} kB em {time.perf_counter() - t0:.1f} s")

        status, _, _ = visitante.post("/api/exportar/kmz", corpo={"id": rede["id"], "camadas": []})
        conferir("exportar sem camada é recusado", status == 422, f"HTTP {status}")

        # --- projetos e etapas --------------------------------------------------------------------
        _, corpo, _ = dono.post("/api/projetos", corpo={"nome": "Obra de teste", "descricao": "regressão",
                                                        "regiao": rede["regiao"], "visibilidade": "privada"})
        projeto = json.loads(corpo)
        try:
            status, _, _ = dono.post(f"/api/projetos/{projeto['id']}/etapas",
                                     corpo={"nome": "Etapa 0 · rede atual", "rede_id": rede["id"]})
            conferir("etapa criada", status == 200, f"HTTP {status}")
            vistos = {p["id"] for p in outro.json("/api/projetos")["projetos"]}
            conferir("projeto privado escondido", projeto["id"] not in vistos)
            completo = dono.json(f"/api/projetos/{projeto['id']}")
            conferir("projeto traz suas etapas", len(completo["etapas"]) == 1,
                     f"{len(completo['etapas'])} etapa(s)")
            status, _, _ = outro.post(f"/api/projetos/{projeto['id']}/etapas",
                                      corpo={"rede_id": rede["id"]})
            conferir("outro não acrescenta etapa", status == 404, f"HTTP {status}")
        finally:
            dono.delete(f"/api/projetos/{projeto['id']}")
        conferir("projeto de teste removido", catalogo.obter_projeto(projeto["id"], "7034") is None)

        # --- subestações e composição --------------------------------------------------------
        multi = max(publicas, key=lambda r: len(r["resumo"].get("subestacoes", [])))
        ses = visitante.json(f"/api/subestacoes?id={multi['id']}")["subestacoes"]
        conferir("lista as subestações da rede", len(ses) >= 1,
                 ", ".join(f"{s['cod']}={s['postes']}p" for s in ses))

        inteira = visitante.json(f"/api/rede?id={multi['id']}")
        postes_inteira = inteira["meta"]["resumo"]["postes"]
        menor = min(ses, key=lambda s: s["postes"])
        t0 = time.perf_counter()
        recorte = visitante.json(f"/api/rede?c={multi['id']}:{menor['cod']}")
        dt = time.perf_counter() - t0
        conferir("recorte traz só a SE pedida",
                 [se["cod"] for se in recorte["subestacoes"]] == [menor["cod"]])
        conferir("recorte bate com o peso anunciado",
                 recorte["meta"]["resumo"]["postes"] == menor["postes"],
                 f"{recorte['meta']['resumo']['postes']} postes")
        if len(ses) > 1:
            conferir("recorte é bem menor que a rede inteira",
                     recorte["meta"]["resumo"]["postes"] < postes_inteira,
                     f"{recorte['meta']['resumo']['postes']} de {postes_inteira} postes "
                     f"({recorte['meta']['resumo']['postes'] / postes_inteira:.0%}) em {dt:.1f} s")
            duas = sorted(ses, key=lambda s: s["postes"])[:2]
            soma = visitante.json(f"/api/rede?c={multi['id']}:{duas[0]['cod']},{duas[1]['cod']}")
            juntos = soma["meta"]["resumo"]["postes"]
            previsto = duas[0]["postes"] + duas[1]["postes"]
            # não é soma exata: postes de fronteira pertencem às duas SEs e entram uma vez só
            conferir("duas SEs juntas não passam da soma das partes",
                     max(duas[0]["postes"], duas[1]["postes"]) <= juntos <= previsto,
                     f"{juntos} postes (soma das partes: {previsto}, "
                     f"{previsto - juntos} compartilhado(s))")
            conferir("as duas SEs vieram", len(soma["subestacoes"]) == 2)
            conferir("composição anota do que é feita", len(soma["meta"]["composicao"]) >= 1)

        status, corpo, _ = visitante.get(f"/api/rede?c={multi['id']}:NAOEXISTE")
        conferir("SE inexistente é recusada", status == 422, f"HTTP {status}")

        # juntar regiões diferentes não pode
        outra_regiao = next((r for r in publicas if r["epsg"] != multi["epsg"]), None)
        if outra_regiao:
            status, corpo, _ = visitante.get(
                f"/api/rede?c={multi['id']}:{menor['cod']};{outra_regiao['id']}:")
            conferir("misturar regiões é recusado", status == 422, f"HTTP {status}")

        # --- trocar público/privado ------------------------------------------------------------
        minha = catalogo.adicionar("hash-vis-teste", "minha.mdb", 10, "Dono", "teste",
                                   {"subestacoes": [], "circuitos": [], "numeros": {}},
                                   multi["regiao"], multi["epsg"], catalogo.PUBLICA, "7034")
        try:
            status, _, _ = outro.pedir("PATCH", f"/api/acervo/{minha['id']}",
                                       {"visibilidade": "privada"})
            conferir("quem não enviou não muda a visibilidade", status == 403, f"HTTP {status}")
            status, corpo, _ = dono.pedir("PATCH", f"/api/acervo/{minha['id']}",
                                          {"visibilidade": "privada"})
            conferir("o dono torna a rede privada", status == 200
                     and json.loads(corpo)["visibilidade"] == "privada", f"HTTP {status}")
            conferir("e some para os outros",
                     minha["id"] not in {r["id"] for r in outro.json("/api/acervo")["redes"]})
            dono.pedir("PATCH", f"/api/acervo/{minha['id']}", {"visibilidade": "publica"})
            conferir("e volta a aparecer quando fica pública",
                     minha["id"] in {r["id"] for r in outro.json("/api/acervo")["redes"]})
            status, _, _ = dono.pedir("PATCH", f"/api/acervo/{minha['id']}",
                                      {"visibilidade": "qualquer"})
            conferir("visibilidade inválida é recusada", status == 422, f"HTTP {status}")

            # rede sem dono (enviada antes do login): tornar privada não pode escondê-la de todos
            orfa = catalogo.adicionar("hash-orfa-teste", "orfa.mdb", 10, "Alguém", "sem dono",
                                      {"subestacoes": [], "circuitos": [], "numeros": {}},
                                      multi["regiao"], multi["epsg"], catalogo.PUBLICA, "")
            try:
                status, _, _ = dono.pedir("PATCH", f"/api/acervo/{orfa['id']}",
                                          {"visibilidade": "privada"})
                conferir("usuário comum não mexe em rede sem dono", status == 403, f"HTTP {status}")
                status, corpo, _ = chefe.pedir("PATCH", f"/api/acervo/{orfa['id']}",
                                               {"visibilidade": "privada"})
                conferir("admin torna privada uma rede sem dono", status == 200, f"HTTP {status}")
                atual = catalogo.obter_bruto(orfa["id"])
                conferir("a rede ganha dono em vez de sumir para todos",
                         atual["dono"] == "0001", f"dono={atual['dono']!r}")
                conferir("e continua visível para quem a tornou privada",
                         orfa["id"] in {r["id"] for r in chefe.json("/api/acervo")["redes"]})
                conferir("e sumiu para os demais",
                         orfa["id"] not in {r["id"] for r in dono.json("/api/acervo")["redes"]})
            finally:
                catalogo.remover(orfa["id"])

            # --- pastas ---------------------------------------------------------------------
            status, corpo, _ = visitante.post("/api/pastas", corpo={"nome": "Estudos 2027"})
            conferir("visitante não cria pasta", status == 401, f"HTTP {status}")
            pasta = json.loads(dono.post("/api/pastas", corpo={"nome": "Estudos 2027"})[1])
            dentro = json.loads(dono.post("/api/pastas",
                                          corpo={"nome": "Sub", "pai": pasta["id"]})[1])
            conferir("pasta criada", pasta["nome"] == "Estudos 2027")

            status, _, _ = dono.pedir("PATCH", f"/api/acervo/{minha['id']}",
                                      {"pasta": pasta["id"]})
            conferir("rede movida para a pasta", status == 200, f"HTTP {status}")
            conferir("a rede sabe onde está",
                     catalogo.obter_bruto(minha["id"])["pasta"] == pasta["id"])

            status, _, _ = dono.delete(f"/api/pastas/{pasta['id']}")
            conferir("pasta com conteúdo não é excluída", status == 409, f"HTTP {status}")

            status, _, _ = dono.pedir("PATCH", f"/api/pastas/{pasta['id']}",
                                      {"pai": dentro["id"]})
            conferir("pasta não entra dentro de si mesma", status == 409, f"HTTP {status}")

            status, _, _ = dono.pedir("PATCH", f"/api/acervo/{minha['id']}", {"pasta": ""})
            conferir("rede volta para a raiz", status == 200, f"HTTP {status}")
            dono.delete(f"/api/pastas/{dentro['id']}")
            status, _, _ = dono.delete(f"/api/pastas/{pasta['id']}")
            conferir("pasta vazia é excluída", status == 200, f"HTTP {status}")

            # --- rede usada por projeto não pode ser excluída --------------------------------
            # de propósito sobre a rede descartável: o teste não pode apagar rede de verdade
            proj = json.loads(dono.post("/api/projetos",
                                        corpo={"nome": "Projeto que segura",
                                               "regiao": multi["regiao"]})[1])
            try:
                dono.post(f"/api/projetos/{proj['id']}/etapas",
                          corpo={"nome": "Etapa 0", "composicao": f"{minha['id']}:"})
                status, corpo, _ = dono.delete(f"/api/acervo/{minha['id']}")
                conferir("rede usada por projeto não é excluída", status == 409, f"HTTP {status}")
                conferir("o erro diz qual projeto segura",
                         "Projeto que segura" in corpo.decode("utf-8", "replace"))
                conferir("a rede continua no acervo", catalogo.obter_bruto(minha["id"]) is not None)
                etapas = dono.json(f"/api/projetos/{proj['id']}")["etapas"]
                conferir("a etapa guardou a composição",
                         etapas[0]["composicao"] == [{"rede": minha["id"], "subestacoes": []}],
                         str(etapas[0]["composicao"]))
            finally:
                dono.delete(f"/api/projetos/{proj['id']}")
            status, _, _ = dono.delete(f"/api/acervo/{minha['id']}")
            conferir("sem projeto, a exclusão é liberada", status == 200, f"HTTP {status}")
        finally:
            catalogo.remover(minha["id"])
    finally:
        servidor.should_exit = True

    print("\nresultado:", "tudo certo" if not falhas else f"{falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
