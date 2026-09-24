"""Teste do recorte por subestação e da junção de redes.

Roda contra as redes que estiverem no acervo. O que mais importa aqui são os índices: vãos e
equipamentos apontam para postes por posição, e circuitos e subestações se repetem entre bases —
um erro de renumeração aparece como vão ligando o poste errado, que é difícil de notar na tela.

Use: python tools/teste_compor.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import catalogo, compor, interplan  # noqa: E402
from app.config import settings  # noqa: E402

falhas = 0


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    if not condicao:
        falhas += 1
    print(f"{'ok  ' if condicao else 'FALHA'} {rotulo}{f' · {detalhe}' if detalhe else ''}")


def integridade(rotulo: str, m: dict) -> None:
    """Todo índice tem de cair dentro da lista de postes, e todo circuito tem de existir."""
    n = len(m["postes"])
    ids_circ = {c["id"] for c in m["circuitos"]}
    ids_se = {se["id"] for se in m["subestacoes"]}

    fora = [v["id"] for v in m["vaos"] if not (0 <= v["a"] < n and 0 <= v["b"] < n)]
    conferir(f"{rotulo}: vãos apontam para postes existentes", not fora,
             f"{len(fora)} fora do intervalo" if fora else f"{len(m['vaos'])} vãos")

    for chave in ("trafos", "chaves", "reguladores", "capacitores", "geradores"):
        ruins = [it.get("cod") for it in m[chave] if not (0 <= it["p"] < n)]
        conferir(f"{rotulo}: {chave} em postes existentes", not ruins, f"{len(ruins)} ruins")

    orfaos = [v["id"] for v in m["vaos"] if v.get("c") is not None and v["c"] not in ids_circ]
    conferir(f"{rotulo}: vãos sem circuito órfão", not orfaos, f"{len(orfaos)} órfãos")

    p0 = [c["codigo"] for c in m["circuitos"]
          if c["poste0"] is not None and not (0 <= c["poste0"] < n)]
    conferir(f"{rotulo}: poste inicial dos circuitos", not p0, f"{len(p0)} ruins")

    se_orfa = [c["codigo"] for c in m["circuitos"] if c["se"] is not None and c["se"] not in ids_se]
    conferir(f"{rotulo}: circuitos apontam para SE existente", not se_orfa, f"{len(se_orfa)} órfãos")

    repetidos = len(ids_circ) != len(m["circuitos"])
    conferir(f"{rotulo}: nenhum id de circuito repetido", not repetidos,
             f"{len(m['circuitos'])} circuitos, {len(ids_circ)} ids")

    alim = [a["circ"] for se in m["subestacoes"] for a in se["alimentadores"]]
    conferir(f"{rotulo}: alimentadores das SEs existem",
             all(c in ids_circ for c in alim if c is not None), f"{len(alim)} alimentadores")


def modelo_de(registro: dict) -> dict:
    return interplan.build_model(str(catalogo.caminho(registro["hash"])),
                                 password=settings.senha, epsg=registro["epsg"])


def main() -> int:
    redes = catalogo.listar()
    if not redes:
        print("Acervo vazio: este teste precisa de ao menos uma rede.")
        return 1

    # prefere uma rede com várias subestações, que é o caso interessante
    redes = sorted(redes, key=lambda r: -len(r["resumo"].get("subestacoes", [])))
    principal = redes[0]
    print(f"rede principal: {principal['arquivo']} ({principal['regiao']}) "
          f"· {len(principal['resumo']['subestacoes'])} SE(s)\n")

    t0 = time.perf_counter()
    inteiro = modelo_de(principal)
    print(f"modelo inteiro montado em {time.perf_counter() - t0:.1f} s: "
          f"{inteiro['meta']['resumo']['postes']} postes\n")

    ses = compor.subestacoes_do_modelo(inteiro)
    conferir("lista de subestações com peso", all(s["postes"] > 0 for s in ses),
             ", ".join(f"{s['cod']}={s['postes']}" for s in ses))
    integridade("inteiro", inteiro)
    print()

    if len(ses) < 2:
        print("(a rede tem uma SE só; o recorte é testado com ela mesma)\n")

    # --- recorte de uma subestação ---------------------------------------------------------
    escolhida = min(ses, key=lambda s: s["postes"])
    t0 = time.perf_counter()
    recorte = compor.recortar(inteiro, [escolhida["cod"]])
    print(f"recorte de {escolhida['cod']} em {time.perf_counter() - t0:.2f} s")
    conferir("recorte tem só a SE pedida",
             [se["cod"] for se in recorte["subestacoes"]] == [escolhida["cod"]])
    conferir("recorte bate com o peso previsto",
             recorte["meta"]["resumo"]["postes"] == escolhida["postes"],
             f"{recorte['meta']['resumo']['postes']} vs {escolhida['postes']} previstos")
    if len(ses) > 1:
        conferir("recorte é menor que o inteiro",
                 recorte["meta"]["resumo"]["postes"] < inteiro["meta"]["resumo"]["postes"],
                 f"{recorte['meta']['resumo']['postes']} de {inteiro['meta']['resumo']['postes']} postes")
    integridade("recorte", recorte)
    conferir("bbox do recorte cabe no da rede inteira",
             recorte["meta"]["bbox"][0] >= inteiro["meta"]["bbox"][0] - 1
             and recorte["meta"]["bbox"][2] <= inteiro["meta"]["bbox"][2] + 1)
    print()

    # --- recorte pedindo tudo ---------------------------------------------------------------
    todas = compor.recortar(inteiro, [s["cod"] for s in ses])
    conferir("pedir todas as SEs devolve a rede inteira",
             todas["meta"]["resumo"]["postes"] == inteiro["meta"]["resumo"]["postes"])
    conferir("lista vazia devolve a rede inteira",
             compor.recortar(inteiro, [])["meta"]["resumo"]["postes"]
             == inteiro["meta"]["resumo"]["postes"])
    try:
        compor.recortar(inteiro, ["NAOEXISTE"])
        conferir("SE inexistente é recusada", False, "aceitou")
    except compor.ComposicaoError as exc:
        conferir("SE inexistente é recusada", True, str(exc)[:70])
    print()

    # --- junção de duas partes ---------------------------------------------------------------
    if len(ses) >= 2:
        a, b = ses[0], ses[1]
        pa = compor.recortar(inteiro, [a["cod"]])
        pb = compor.recortar(inteiro, [b["cod"]])
        t0 = time.perf_counter()
        junto = compor.juntar([
            {"model": pa, "rede": principal["id"], "nome": "parte A"},
            {"model": pb, "rede": principal["id"], "nome": "parte B"},
        ])
        print(f"junção de {a['cod']} + {b['cod']} em {time.perf_counter() - t0:.2f} s")
        conferir("junção soma os postes",
                 junto["meta"]["resumo"]["postes"] == a["postes"] + b["postes"],
                 f"{junto['meta']['resumo']['postes']} = {a['postes']} + {b['postes']}")
        conferir("junção soma os circuitos",
                 junto["meta"]["resumo"]["circuitos"] == a["circuitos"] + b["circuitos"])
        conferir("junção traz as duas SEs", len(junto["subestacoes"]) == 2)
        integridade("junção", junto)
        conferir("junção anota do que é feita", len(junto["meta"]["composicao"]) == 2,
                 str(junto["meta"]["composicao"]))
        print()

    # --- junção entre redes diferentes, se houver outra na mesma região -----------------------
    outra = next((r for r in redes[1:] if r["epsg"] == principal["epsg"]), None)
    if outra:
        m2 = modelo_de(outra)
        junto2 = compor.juntar([
            {"model": compor.recortar(inteiro, [escolhida["cod"]]), "rede": principal["id"],
             "nome": principal["arquivo"]},
            {"model": m2, "rede": outra["id"], "nome": outra["arquivo"]},
        ])
        print(f"junção entre redes: {principal['arquivo']} + {outra['arquivo']}")
        conferir("postes somam",
                 junto2["meta"]["resumo"]["postes"]
                 == escolhida["postes"] + m2["meta"]["resumo"]["postes"])
        integridade("entre redes", junto2)
        larguras = junto2["meta"]["bbox"]
        conferir("bbox cresce para caber as duas",
                 larguras[2] - larguras[0] >= inteiro["meta"]["bbox"][2] - inteiro["meta"]["bbox"][0] - 1
                 or larguras[3] - larguras[1] > 0,
                 f"{larguras[2] - larguras[0]:.0f} x {larguras[3] - larguras[1]:.0f} m")
        conferir("cada SE sabe de que rede veio",
                 all(se.get("rede") for se in junto2["subestacoes"]))
        print()

    # --- pedaços longe demais são recusados ----------------------------------------------------
    # O relevo e os prédios cobrem todo o vazio entre os pedaços: juntar coisas distantes vira
    # um download enorme, e antes do limite o pedido ficava pendurado sem explicação.
    # O caso é montado aqui deslocando a origem de uma cópia, para não depender do acervo.
    from app.config import settings as cfg  # noqa: PLC0415

    recorte_a = compor.recortar(inteiro, [escolhida["cod"]])
    afastado = {**recorte_a, "meta": {**recorte_a["meta"],
                                      "origem": {"x": recorte_a["meta"]["origem"]["x"]
                                                      + (cfg.extensao_max_km + 20) * 1000,
                                                 "y": recorte_a["meta"]["origem"]["y"]}}}
    try:
        compor.juntar([{"model": recorte_a, "rede": principal["id"], "nome": "aqui"},
                       {"model": afastado, "rede": principal["id"], "nome": "a 60 km daqui"}])
        conferir("junção espalhada demais é recusada", False, "aceitou")
    except compor.ComposicaoError as exc:
        conferir("junção espalhada demais é recusada", True, str(exc)[:95])

    # e logo abaixo do limite continua passando
    dentro = {**recorte_a, "meta": {**recorte_a["meta"],
                                    "origem": {"x": recorte_a["meta"]["origem"]["x"] + 5000,
                                               "y": recorte_a["meta"]["origem"]["y"]}}}
    try:
        vizinho = compor.juntar([{"model": recorte_a, "rede": principal["id"], "nome": "aqui"},
                                 {"model": dentro, "rede": principal["id"], "nome": "5 km ao lado"}])
        lado = (vizinho["meta"]["bbox"][2] - vizinho["meta"]["bbox"][0]) / 1000
        conferir("vizinho a 5 km continua passando", True, f"{lado:.1f} km de largura")
    except compor.ComposicaoError as exc:
        conferir("vizinho a 5 km continua passando", False, str(exc)[:80])

    # pedaços da mesma área continuam passando
    if len(ses) >= 2:
        perto = compor.juntar([
            {"model": compor.recortar(inteiro, [ses[0]["cod"]]), "rede": principal["id"], "nome": "a"},
            {"model": compor.recortar(inteiro, [ses[1]["cod"]]), "rede": principal["id"], "nome": "b"},
        ])
        lx = (perto["meta"]["bbox"][2] - perto["meta"]["bbox"][0]) / 1000
        ly = (perto["meta"]["bbox"][3] - perto["meta"]["bbox"][1]) / 1000
        conferir("SEs da mesma área continuam passando", True, f"{lx:.1f} x {ly:.1f} km")

    # --- misturar regiões tem de ser recusado -------------------------------------------------
    diferente = next((r for r in redes if r["epsg"] != principal["epsg"]), None)
    if diferente:
        try:
            compor.juntar([{"model": inteiro, "rede": principal["id"], "nome": "a"},
                           {"model": modelo_de(diferente), "rede": diferente["id"], "nome": "b"}])
            conferir("misturar regiões é recusado", False, "aceitou")
        except compor.ComposicaoError as exc:
            conferir("misturar regiões é recusado", True, str(exc)[:80])
    else:
        print("(sem rede de outra região no acervo para testar a recusa)")

    print("\nresultado:", "tudo certo" if not falhas else f"{falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
