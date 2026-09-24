"""Recorte por subestação e junção de redes numa visualização só.

Uma rede grande do Interplan (a do ES tem 55 MB e quatro subestações) demora para abrir inteira,
e quase sempre o estudo é de uma subestação só. Aqui o modelo é recortado pelas subestações
escolhidas e, quando o usuário acrescenta partes de outras redes, os pedaços são juntados num
**modelo único** — com uma origem, uma lista de postes e um conjunto de circuitos.

Isso importa porque tudo que vem depois (relevo, vegetação, prédios, ruas, exportação em KMZ)
trabalha em cima de um modelo; juntar aqui evita ter que ensinar cada uma dessas etapas a lidar
com várias redes ao mesmo tempo.

Os índices são o ponto delicado: vãos e equipamentos apontam para postes por posição na lista, e
os identificadores de circuito e de subestação se repetem entre bases diferentes. Tudo é
renumerado na junção.
"""
from __future__ import annotations

from .config import settings
from .interplan import CIRCUIT_COLORS, _map_tiles


class ComposicaoError(Exception):
    """Composição impossível de montar (vazia, ou misturando regiões diferentes)."""


def _postes_mantidos(model: dict, ids_circ: set, vaos: list[dict]) -> set:
    """Índices dos postes que ficam num recorte.

    Além dos postes do próprio circuito, entram as pontas dos vãos mantidos: um vão pode terminar
    num poste de outro circuito, e sem ele o vão ficaria apontando para o vazio.
    """
    manter = {v["a"] for v in vaos} | {v["b"] for v in vaos}
    manter |= {i for i, p in enumerate(model["postes"]) if p.get("c") in ids_circ}
    for chave in ("trafos", "chaves", "reguladores", "capacitores", "geradores"):
        manter |= {it["p"] for it in model[chave] if it.get("c") in ids_circ}
    return manter


def subestacoes_do_modelo(model: dict) -> list[dict]:
    """Subestações com o peso exato de cada uma, para o usuário escolher sabendo o que pesa.

    O número de postes é o mesmo que o recorte produz — contado pela mesma regra, para a
    interface não prometer um tamanho e entregar outro.
    """
    circ_da_se: dict = {}
    for c in model["circuitos"]:
        circ_da_se.setdefault(c["se"], set()).add(c["id"])
    km_por_circ = {c["id"]: c["km"] for c in model["circuitos"]}

    saida = []
    for se in model["subestacoes"]:
        circs = circ_da_se.get(se["id"], set())
        vaos = [v for v in model["vaos"] if v.get("c") in circs]
        saida.append({
            "cod": se["cod"],
            "nome": se["nome"],
            "circuitos": len(circs),
            "postes": len(_postes_mantidos(model, circs, vaos)),
            "vaos": len(vaos),
            "km": round(sum(km_por_circ.get(c, 0) for c in circs), 1),
        })
    return sorted(saida, key=lambda s: s["cod"])


def recortar(model: dict, codigos: list[str] | None) -> dict:
    """Devolve só as subestações pedidas (codigos vazio ou None = a rede inteira).

    Um poste entra se pertence a um circuito mantido ou se é ponta de um vão mantido — assim
    nenhum vão fica apontando para poste inexistente.
    """
    if not codigos:
        return model
    alvo = {c.strip().upper() for c in codigos if c and c.strip()}
    if not alvo:
        return model

    ses = [se for se in model["subestacoes"] if (se["cod"] or "").upper() in alvo]
    if not ses:
        raise ComposicaoError(
            f"Nenhuma subestação {', '.join(sorted(alvo))} nesta rede. "
            f"Disponíveis: {', '.join(se['cod'] for se in model['subestacoes'])}.")
    if len(ses) == len(model["subestacoes"]):
        return model

    ids_se = {se["id"] for se in ses}
    circuitos = [c for c in model["circuitos"] if c["se"] in ids_se]
    ids_circ = {c["id"] for c in circuitos}

    vaos = [v for v in model["vaos"] if v.get("c") in ids_circ]
    ordem = sorted(_postes_mantidos(model, ids_circ, vaos))
    novo_indice = {velho: i for i, velho in enumerate(ordem)}
    postes = [model["postes"][i] for i in ordem]

    recortado = dict(model)
    recortado["postes"] = postes
    recortado["subestacoes"] = ses
    recortado["circuitos"] = circuitos
    recortado["vaos"] = [{**v, "a": novo_indice[v["a"]], "b": novo_indice[v["b"]]} for v in vaos]
    for chave in ("trafos", "chaves", "reguladores", "capacitores", "geradores"):
        recortado[chave] = [{**it, "p": novo_indice[it["p"]]}
                            for it in model[chave]
                            if it.get("c") in ids_circ and it["p"] in novo_indice]
    recortado["circuitos"] = [
        {**c, "poste0": novo_indice.get(c["poste0"]) if c["poste0"] is not None else None}
        for c in circuitos]
    # interligações são pontos soltos: ficam as que caem dentro do recorte
    recortado["interligacoes"] = _interligacoes_no_recorte(model, postes)

    recortado["meta"] = {**model["meta"], "bbox": _bbox(postes, ses),
                         "resumo": _resumo(recortado)}
    recortado["mapa"] = _map_tiles(model["meta"]["epsg"], model["meta"]["origem"]["x"],
                                   model["meta"]["origem"]["y"], recortado["meta"]["bbox"])
    return recortado


def _interligacoes_no_recorte(model: dict, postes: list[dict], margem: float = 300.0) -> list[dict]:
    if not postes:
        return []
    x0 = min(p["x"] for p in postes) - margem
    x1 = max(p["x"] for p in postes) + margem
    y0 = min(p["y"] for p in postes) - margem
    y1 = max(p["y"] for p in postes) + margem
    return [t for t in model["interligacoes"] if x0 <= t["x"] <= x1 and y0 <= t["y"] <= y1]


def _bbox(postes: list[dict], ses: list[dict]) -> list[float]:
    xs = [p["x"] for p in postes] + [se["x"] for se in ses]
    ys = [p["y"] for p in postes] + [se["y"] for se in ses]
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]


def _resumo(model: dict) -> dict:
    trafos = model["trafos"]
    return {
        "circuitos": len(model["circuitos"]),
        "postes": len(model["postes"]),
        "vaos": len(model["vaos"]),
        "km": round(sum(v.get("l") or 0 for v in model["vaos"]) / 1000, 2),
        "trafos": sum(1 for t in trafos if t["tipo"] == "ET"),
        "kva": round(sum(t["snom"] or 0 for t in trafos if t["tipo"] == "ET"), 1),
        "cabines": sum(1 for t in trafos if t["tipo"] == "EP"),
        "chaves": len(model["chaves"]),
        "chaves_abertas": sum(1 for c in model["chaves"] if c["estado"] == 0),
        "reguladores": len(model["reguladores"]),
        "capacitores": len(model["capacitores"]),
        "geradores": len(model["geradores"]),
        "gd_kva": round(sum(g["snom"] or 0 for g in model["geradores"]), 1),
    }


def juntar(pedacos: list[dict]) -> dict:
    """Junta vários recortes num modelo só.

    `pedacos` é uma lista de {"model": <modelo recortado>, "rede": <id no acervo>,
    "nome": <rótulo>}. O primeiro manda na origem; os demais são deslocados para o
    referencial dele.
    """
    if not pedacos:
        raise ComposicaoError("Escolha ao menos uma subestação para montar a visualização.")
    if len(pedacos) == 1:
        unico = pedacos[0]["model"]
        return {**unico, "meta": {**unico["meta"], "composicao": _descricao(pedacos)}}

    epsgs = {p["model"]["meta"]["epsg"] for p in pedacos}
    if len(epsgs) > 1:
        raise ComposicaoError(
            "Não dá para juntar redes de regiões diferentes na mesma visualização: os fusos "
            f"são incompatíveis (EPSG {', '.join(str(e) for e in sorted(epsgs))}). "
            "Monte uma visualização por região.")

    base = pedacos[0]["model"]
    ox, oy = base["meta"]["origem"]["x"], base["meta"]["origem"]["y"]

    postes: list[dict] = []
    vaos: list[dict] = []
    circuitos: list[dict] = []
    subestacoes: list[dict] = []
    interligacoes: list[dict] = []
    equipamentos: dict = {c: [] for c in ("trafos", "chaves", "reguladores", "capacitores", "geradores")}
    proximo_circ = 1
    proxima_se = 1

    for pedaco in pedacos:
        m = pedaco["model"]
        # deslocamento para o referencial do primeiro pedaço
        dx = m["meta"]["origem"]["x"] - ox
        dy = m["meta"]["origem"]["y"] - oy
        base_poste = len(postes)
        circ_novo: dict = {}
        se_nova: dict = {}

        for se in m["subestacoes"]:
            se_nova[se["id"]] = proxima_se
            proxima_se += 1
        for c in m["circuitos"]:
            circ_novo[c["id"]] = proximo_circ
            proximo_circ += 1

        for p in m["postes"]:
            postes.append({**p, "x": round(p["x"] + dx, 2), "y": round(p["y"] + dy, 2),
                           "c": circ_novo.get(p.get("c"))})
        for v in m["vaos"]:
            vaos.append({**v, "a": v["a"] + base_poste, "b": v["b"] + base_poste,
                         "c": circ_novo.get(v.get("c"))})
        for chave, lista in equipamentos.items():
            for it in m[chave]:
                lista.append({**it, "p": it["p"] + base_poste, "c": circ_novo.get(it.get("c"))})
        for c in m["circuitos"]:
            circuitos.append({
                **c, "id": circ_novo[c["id"]], "se": se_nova.get(c["se"]),
                "poste0": c["poste0"] + base_poste if c["poste0"] is not None else None,
                "rede": pedaco.get("rede"),
            })
        for se in m["subestacoes"]:
            subestacoes.append({
                **se, "id": se_nova[se["id"]],
                "x": round(se["x"] + dx, 2), "y": round(se["y"] + dy, 2),
                "alimentadores": [{**a, "circ": circ_novo.get(a["circ"])} for a in se["alimentadores"]],
                "rede": pedaco.get("rede"), "origem_nome": pedaco.get("nome"),
            })
        for t in m["interligacoes"]:
            interligacoes.append({**t, "x": round(t["x"] + dx, 2), "y": round(t["y"] + dy, 2)})

    # cores redistribuídas no conjunto todo, senão dois circuitos vizinhos saem iguais
    for i, c in enumerate(circuitos):
        c["cor"] = CIRCUIT_COLORS[i % len(CIRCUIT_COLORS)]

    juntado = {
        **base,
        "postes": postes, "vaos": vaos, "circuitos": circuitos, "subestacoes": subestacoes,
        "interligacoes": interligacoes, **equipamentos,
    }
    bbox = _bbox(postes, subestacoes)
    _conferir_extensao(bbox, pedacos)
    juntado["meta"] = {
        **base["meta"], "bbox": bbox, "resumo": _resumo(juntado),
        "composicao": _descricao(pedacos),
        "nome": " + ".join(dict.fromkeys(p.get("nome") or "rede" for p in pedacos)),
    }
    juntado["mapa"] = _map_tiles(base["meta"]["epsg"], ox, oy, bbox)
    return juntado


def _conferir_extensao(bbox: list[float], pedacos: list[dict]) -> None:
    """Barra junções espalhadas demais.

    O relevo, o dossel e os prédios cobrem toda a área entre os pedaços, não só onde há rede.
    Juntar duas subestações a 90 km uma da outra pediria uma grade de relevo de milhões de
    pontos e mais de mil ladrilhos de dossel — o pedido ficaria pendurado sem explicação.
    Uma rede sozinha não passa por aqui: abre do tamanho que for.
    """
    limite = settings.extensao_max_km
    if limite <= 0:
        return
    largura = (bbox[2] - bbox[0]) / 1000
    altura = (bbox[3] - bbox[1]) / 1000
    if max(largura, altura) <= limite:
        return
    quais = ", ".join(
        f"{p.get('nome') or 'rede'} ({', '.join(se['cod'] for se in p['model']['subestacoes']) or 'inteira'})"
        for p in pedacos)
    raise ComposicaoError(
        f"Os pedaços escolhidos ficam longe demais entre si: juntos ocupam "
        f"{largura:.0f} x {altura:.0f} km, acima do limite de {limite:.0f} km. "
        f"O relevo e os prédios teriam de cobrir todo o vazio entre eles. "
        f"Escolhidos: {quais}. "
        f"Junte subestações da mesma área, ou aumente extensao_max_km no conf.ini.")


def _descricao(pedacos: list[dict]) -> list[dict]:
    """O que foi carregado, para a etapa guardar e a interface mostrar."""
    return [{"rede": p.get("rede"), "nome": p.get("nome"),
             "subestacoes": [se["cod"] for se in p["model"]["subestacoes"]]} for p in pedacos]
