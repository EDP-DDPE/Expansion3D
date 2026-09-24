"""Servidor do Expansion 3D: acervo compartilhado de bases do Interplan e modelo 3D em JSON.

Quem entra sem sessão do Atlas fica em modo Visualização: enxerga as redes públicas e não pode
enviar, importar nem excluir nada. As redes privadas só aparecem para a matrícula que as enviou.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pyproj import Transformer

from . import catalogo, compor, context3d, interplan, kml, sessao
from .config import CONF_PATH, settings

PROJECT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_DIR / "web"
THREE_DIR = PROJECT_DIR / "node_modules" / "three"

app = FastAPI(title=settings.titulo)
ID_Q = Query(..., description="Identificador da rede no acervo")

# Usuário usado quando a integração com o Atlas está desligada no conf.ini (máquina de desenvolvimento).
LIVRE = sessao.Usuario(matricula="local", nome="Usuário local", admin=True)


# --- sessão ------------------------------------------------------------------------------------
def usuario_atual(request: Request) -> sessao.Usuario | None:
    """Quem está usando o sistema agora, ou None para o modo Visualização.

    A integração desligada no conf.ini (`ativo = nao`) é uma escolha explícita e libera tudo,
    para rodar numa máquina de desenvolvimento. Já a integração ligada **sem segredo** é um erro
    de configuração, e aí o sistema fica em modo Visualização: é melhor ninguém conseguir alterar
    nada do que todo mundo entrar como administrador sem querer.
    """
    if not settings.atlas_ativo:
        return LIVRE
    if not settings.sso_pronto:
        return None
    return sessao.usuario_do_cookie(request.cookies.get(sessao.COOKIE))


def exigir_login(usuario: sessao.Usuario | None = Depends(usuario_atual)) -> sessao.Usuario:
    if usuario is None:
        if settings.atlas_ativo and not settings.sso_pronto:
            raise HTTPException(503, "O login com o Atlas está ligado mas sem a frase secreta: "
                                     "preencha [atlas] segredo no conf.ini, igual ao "
                                     "EXPANSION_SECRET do .env do Atlas.")
        raise HTTPException(401, "Entre com sua conta do Atlas para fazer esta alteração.")
    return usuario


@app.on_event("startup")
def avisar_configuracao() -> None:
    if settings.atlas_ativo and not settings.sso_pronto:
        print("ATENÇÃO: [atlas] ativo = sim, mas 'segredo' está vazio no conf.ini.\n"
              "         Ninguém consegue entrar e o sistema fica em modo Visualização.\n"
              f"         Gere uma frase com: python -c \"import secrets; "
              f"print(secrets.token_urlsafe(48))\"\n"
              f"         e use a mesma nos dois lados ({CONF_PATH} e o .env do Atlas).")
    elif not settings.atlas_ativo:
        print("ATENÇÃO: [atlas] ativo = nao — sem login, qualquer visitante pode enviar e "
              "excluir redes. Use assim só em máquina de desenvolvimento.")


def _matricula(usuario: sessao.Usuario | None) -> str | None:
    return usuario.matricula if usuario else None


def _pode_mexer(registro: dict, usuario: sessao.Usuario) -> bool:
    """Item privado é só do dono; item público qualquer usuário logado pode excluir."""
    if registro.get("visibilidade") == catalogo.PRIVADA:
        return registro.get("dono") == usuario.matricula
    return True


@app.get("/api/sessao")
def get_sessao(usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return {
        "usuario": usuario.como_dict() if usuario else None,
        "sso": settings.sso_pronto,
        "login_automatico": settings.login_automatico and settings.sso_pronto,
        "atlas": settings.atlas_url,
    }


MOLDURA_PRONTA = ("<!doctype html><meta charset=utf-8><title>Login</title>"
                  "<p style='font:13px system-ui;color:#8a97a4'>{texto}</p>")


def _endereco_publico(request: Request) -> str:
    """Endereço por onde o navegador enxerga o Expansion.

    Vem do conf.ini quando configurado. Deduzir da requisição erra o esquema sempre que houver
    proxy ou balanceador no caminho: um X-Forwarded-Proto: https faz o endereço virar
    `https://host:8010`, e a 8010 responde em HTTP puro — o navegador recebe "resposta inválida".
    """
    if settings.url_publica:
        return settings.url_publica
    return str(request.base_url).rstrip("/")


@app.get("/auth/entrar")
def entrar(request: Request, auto: int = 0, quadro: int = 0) -> RedirectResponse:
    """Manda o usuário ao Atlas, que devolve um ticket assinado se houver sessão por lá."""
    if not settings.sso_pronto:
        return RedirectResponse("/", status_code=303)
    retorno = _endereco_publico(request) + "/auth/retorno"
    if quadro:  # tentativa silenciosa: a volta é uma página mínima, não a aplicação inteira
        retorno += "?quadro=1"
    destino = f"{settings.atlas_url}/api/expansion/ticket?{urlencode({'retorno': retorno, 'auto': auto})}"
    return RedirectResponse(destino, status_code=303)


@app.get("/auth/retorno")
def retorno(request: Request, ticket: str = "", auto: int = 0, quadro: int = 0):
    """Volta do Atlas: confere o ticket e grava o cookie de sessão do Expansion."""
    if not settings.sso_pronto:
        return RedirectResponse("/", status_code=303)

    def responder(destino: str, texto: str):
        # no quadro escondido devolvemos uma página mínima: recarregar a aplicação inteira
        # dentro de um iframe subiria um segundo WebGL à toa
        if quadro:
            return Response(MOLDURA_PRONTA.format(texto=texto), media_type="text/html")
        return RedirectResponse(destino, status_code=303)

    # `auto=1` marca a tentativa silenciosa: se não havia sessão no Atlas, o usuário fica em
    # modo Visualização, sem repetir o salto (evita laço de redirecionamento).
    if not ticket or ticket == "nenhum":
        return responder("/?login=ausente" if auto else "/?login=falhou", "Sem sessão no Atlas.")
    try:
        usuario = sessao.usuario_do_ticket(ticket)
    except sessao.TicketInvalido as exc:
        return responder(f"/?login=erro&motivo={quote(str(exc))}", f"Ticket recusado: {exc}")

    resposta = responder("/?login=ok", "Sessão criada.")
    resposta.set_cookie(
        sessao.COOKIE, sessao.criar_cookie(usuario),
        max_age=settings.sessao_validade_h * 3600, httponly=True, samesite="lax",
        # o Secure segue o endereço público: marcado numa conexão http, o navegador descarta o
        # cookie sem avisar, e o login falharia em silêncio
        secure=_endereco_publico(request).startswith("https://"),
    )
    return resposta


@app.post("/auth/sair")
def sair() -> Response:
    resposta = Response(content='{"saiu":true}', media_type="application/json")
    resposta.delete_cookie(sessao.COOKIE)
    return resposta


# --- modelo 3D ---------------------------------------------------------------------------------
# A visualização é sempre uma "composição": uma ou mais redes, de cada uma as subestações
# escolhidas. Uma rede sozinha é o caso de um pedaço só, sem filtro — assim há um caminho único,
# e relevo, vegetação, prédios e KMZ continuam trabalhando em cima de um modelo.
#
# Formato na URL: `rede:SE1,SE2;outra_rede:` (sem códigos = a rede inteira).
def _ler_composicao(texto: str) -> tuple:
    pedacos = []
    for parte in (texto or "").split(";"):
        parte = parte.strip()
        if not parte:
            continue
        rede, _, ses = parte.partition(":")
        rede = rede.strip()
        if not rede:
            continue
        codigos = tuple(sorted({s.strip().upper() for s in ses.split(",") if s.strip()}))
        pedacos.append((rede, codigos))
    if not pedacos:
        raise HTTPException(422, "Informe ao menos uma rede na composição.")
    return tuple(pedacos)


def _escrever_composicao(pedacos) -> str:
    return ";".join(f"{rede}:{','.join(ses)}" for rede, ses in pedacos)


@lru_cache(maxsize=6)
def _rede_bruta(id_: str) -> dict:
    """Modelo completo de uma rede, sem relevo. É o que custa caro, então fica em cache."""
    registro = catalogo.obter_bruto(id_)
    if registro is None:
        raise HTTPException(404, "Rede não encontrada no acervo (pode ter sido excluída).")
    arquivo = catalogo.caminho(registro["hash"])
    if not arquivo.exists():
        raise HTTPException(410, "O arquivo desta rede não está mais no servidor.")
    model = interplan.build_model(str(arquivo), password=settings.senha,
                                  epsg=registro.get("epsg") or settings.epsg)
    model["meta"]["nome"] = registro["arquivo"]
    model["meta"]["registro"] = {k: registro[k] for k in
                                 ("id", "usuario", "descricao", "criado_em", "regiao",
                                  "visibilidade", "dono")}
    return model


@lru_cache(maxsize=4)
def _modelo(chave: str) -> dict:
    """Modelo da composição, com o relevo já calculado sobre a extensão final."""
    pedacos = _ler_composicao(chave)
    try:
        montados = []
        for rede, ses in pedacos:
            registro = catalogo.obter_bruto(rede)
            montados.append({
                "model": compor.recortar(_rede_bruta(rede), list(ses)),
                "rede": rede,
                "nome": (registro or {}).get("arquivo") or rede,
            })
        model = compor.juntar(montados)
    except compor.ComposicaoError as exc:
        raise HTTPException(422, str(exc)) from exc
    model["terreno"] = context3d.terrain(model)
    return model


def _cache_disco(tema: str, id_: str, assinatura: str, calcular):
    """Guarda em disco o resultado por rede: evita recalcular a cada reinício do serviço."""
    chave = hashlib.sha1(f"{id_}|{assinatura}".encode()).hexdigest()[:16]
    arquivo = settings.cache_dir / "contexto" / tema / f"{chave}.json"
    if arquivo.exists():
        return json.loads(arquivo.read_text(encoding="utf-8"))
    resultado = calcular()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    return resultado


@lru_cache(maxsize=8)
def _vegetacao(id_: str) -> dict:
    # a assinatura invalida o cache se os parâmetros de vegetação mudarem no conf.ini
    assinatura = f"{settings.limiares}|{settings.corredor_m}|{settings.faixa_m}|{settings.altura_min_arvore_m}"
    return _cache_disco("vegetacao", id_, assinatura, lambda: context3d.vegetation(_modelo(id_)))


@lru_cache(maxsize=8)
def _predios(id_: str) -> dict:
    return context3d.buildings(_modelo(id_))


@lru_cache(maxsize=8)
def _ruas(id_: str) -> dict:
    return context3d.roads(_modelo(id_))


def _limpar_caches() -> None:
    for cache in (_rede_bruta, _modelo, _vegetacao, _predios, _ruas):
        cache.cache_clear()


def _conferir_acesso(id_: str, usuario: sessao.Usuario | None) -> dict:
    """404 tanto para rede inexistente quanto para rede privada de outra pessoa."""
    registro = catalogo.obter(id_, _matricula(usuario))
    if registro is None:
        raise HTTPException(404, "Rede não encontrada no acervo.")
    return registro


def _chave_conferida(composicao: str, usuario: sessao.Usuario | None) -> str:
    """Normaliza a composição e confere o acesso a cada rede dela."""
    pedacos = _ler_composicao(composicao)
    for rede, _ in pedacos:
        _conferir_acesso(rede, usuario)
    return _escrever_composicao(pedacos)


def _executar(fn, chave: str, usuario: sessao.Usuario | None) -> dict:
    chave = _chave_conferida(chave, usuario)
    try:
        return fn(chave)
    except HTTPException:
        raise
    except interplan.InterplanError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(502, f"Falha ao obter dados externos: {exc}") from exc


@app.get("/api/config")
def get_config() -> dict:
    return {
        "titulo": settings.titulo,
        "provedor_mapa": settings.provedor_mapa,
        "epsg": settings.epsg,
        "tamanho_max_mb": settings.tamanho_max_mb,
        "kml_max_mb": settings.kml_max_mb,
        "regioes": [{"sigla": r.sigla, "epsg": r.epsg} for r in settings.regioes],
        "regiao_padrao": settings.regiao_padrao,
        "conf": str(CONF_PATH),
    }


# --- acervo de redes ---------------------------------------------------------------------------
@app.get("/api/acervo")
def listar_acervo(usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return {"redes": catalogo.listar(_matricula(usuario))}


def _regiao_das_coordenadas(model: dict, escolhida):
    """Confere o CRS pelas coordenadas: devolve a região cuja área contém o centro da rede."""
    ox, oy = model["meta"]["origem"]["x"], model["meta"]["origem"]["y"]
    candidatas = [escolhida] + [r for r in settings.regioes if r.sigla != escolhida.sigla]
    for regiao in candidatas:
        lon, lat = _para_geo(regiao.epsg).transform(ox, oy)
        if regiao.contem(lon, lat):
            return regiao
    return None


@app.post("/api/acervo")
async def enviar_rede(
    arquivo: UploadFile = File(..., description="Base rede.mdb do Interplan"),
    descricao: str = Form(""),
    regiao: str = Form(""),
    visibilidade: str = Form(catalogo.PUBLICA),
    usuario: sessao.Usuario = Depends(exigir_login),
) -> dict:
    nome = Path(arquivo.filename or "rede.mdb").name
    if Path(nome).suffix.lower() not in (".mdb", ".accdb"):
        raise HTTPException(415, "Envie um arquivo .mdb ou .accdb do Interplan.")
    conteudo = await arquivo.read()
    if len(conteudo) > settings.tamanho_max_mb * 1024 * 1024:
        raise HTTPException(413, f"Arquivo maior que o limite de {settings.tamanho_max_mb} MB.")

    hash_, caminho = catalogo.guardar_arquivo(conteudo)
    escolhida = settings.regiao(regiao)
    try:
        model = interplan.build_model(str(caminho), password=settings.senha, epsg=escolhida.epsg)
        detectada = _regiao_das_coordenadas(model, escolhida)
        aviso = None
        if detectada is None:
            lon, lat = model["meta"]["lonlat"]
            aviso = (f"As coordenadas caem em {lon:.3f}, {lat:.3f}, fora da área de todas as regiões "
                     f"configuradas. A rede foi registrada como {escolhida.sigla}; confira o conf.ini.")
        elif detectada.sigla != escolhida.sigla:
            aviso = (f"As coordenadas são de {detectada.sigla} (EPSG {detectada.epsg}), e não de "
                     f"{escolhida.sigla}. A rede foi registrada em {detectada.sigla}.")
            escolhida = detectada
            model = interplan.build_model(str(caminho), password=settings.senha, epsg=escolhida.epsg)
    except interplan.InterplanError as exc:
        if not any(r["hash"] == hash_ for r in catalogo.listar()):
            caminho.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc

    resumo = {
        "subestacoes": [{"cod": s["cod"], "nome": s["nome"]} for s in model["subestacoes"]],
        "circuitos": [{"id": c["id"], "codigo": c["codigo"], "km": c["km"]} for c in model["circuitos"]],
        "numeros": model["meta"]["resumo"],
        "lonlat": model["meta"]["lonlat"],
        "epsg": model["meta"]["epsg"],
        "regiao": escolhida.sigla,
    }
    registro = catalogo.adicionar(hash_, nome, len(conteudo), usuario.nome, descricao.strip(), resumo,
                                  escolhida.sigla, escolhida.epsg, visibilidade, usuario.matricula)
    return {**registro, "aviso": aviso}


@app.delete("/api/acervo/{id_}")
def excluir_rede(id_: str, usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    registro = catalogo.obter(id_, usuario.matricula)
    if registro is None:
        raise HTTPException(404, "Rede não encontrada no acervo.")
    if not _pode_mexer(registro, usuario):
        raise HTTPException(403, "Esta rede é privada de outro usuário.")
    # uma etapa guarda as redes que a compõem: apagar a rede deixaria o projeto sem como abrir
    usos = catalogo.projetos_que_usam(id_)
    if usos:
        nomes = sorted({f"{u['projeto_nome']} ({u['etapa_nome']})" for u in usos})
        raise HTTPException(409, "Esta rede é usada por " + ("; ".join(nomes[:5]))
                            + (f" e mais {len(nomes) - 5}" if len(nomes) > 5 else "")
                            + ". Exclua as etapas ou o projeto antes de excluir a rede.")
    catalogo.remover(id_)
    _limpar_caches()
    return {"removido": id_}


@app.patch("/api/acervo/{id_}")
def alterar_rede(id_: str, dados: dict = Body(...),
                 usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    """Muda a visibilidade (só o dono) ou a pasta (mesma regra da exclusão)."""
    registro = catalogo.obter(id_, usuario.matricula)
    if registro is None:
        raise HTTPException(404, "Rede não encontrada no acervo.")

    if "visibilidade" in dados:
        dono = registro.get("dono") or ""
        if dono != usuario.matricula and not usuario.admin:
            quem = f" Ela foi enviada por {registro['usuario']}." if registro.get("usuario") else ""
            raise HTTPException(403, "Só quem enviou a rede pode torná-la pública ou privada." + quem)
        # rede sem dono (enviada antes do login) passa a ser de quem a tornou privada, senão
        # ela sumiria para todos: nenhuma matrícula casa com o campo vazio
        if not catalogo.mudar_visibilidade(id_, str(dados["visibilidade"]), usuario.matricula):
            raise HTTPException(422, "Visibilidade deve ser 'publica' ou 'privada'.")

    if "pasta" in dados:
        if not _pode_mexer(registro, usuario):
            raise HTTPException(403, "Esta rede é privada de outro usuário.")
        pasta = str(dados["pasta"] or "")
        if pasta and not any(p["id"] == pasta for p in catalogo.listar_pastas()):
            raise HTTPException(404, "Pasta não encontrada.")
        catalogo.mover(id_, pasta)

    # leitura sem filtro: o acesso já foi conferido no começo, e depois de tornar privada uma
    # rede de outra pessoa a leitura filtrada devolveria nada
    return catalogo.obter_bruto(id_)


# --- pastas do acervo ----------------------------------------------------------------------------
@app.get("/api/pastas")
def listar_pastas() -> dict:
    return {"pastas": catalogo.listar_pastas()}


@app.post("/api/pastas")
def criar_pasta(dados: dict = Body(...), usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    nome = str(dados.get("nome") or "").strip()
    if not nome:
        raise HTTPException(422, "Dê um nome à pasta.")
    pai = str(dados.get("pai") or "")
    if pai and not any(p["id"] == pai for p in catalogo.listar_pastas()):
        raise HTTPException(404, "Pasta de destino não encontrada.")
    return catalogo.criar_pasta(nome[:80], pai, usuario.nome)


@app.patch("/api/pastas/{id_}")
def alterar_pasta(id_: str, dados: dict = Body(...),
                  usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    if not any(p["id"] == id_ for p in catalogo.listar_pastas()):
        raise HTTPException(404, "Pasta não encontrada.")
    if "nome" in dados:
        nome = str(dados["nome"] or "").strip()
        if not nome:
            raise HTTPException(422, "Dê um nome à pasta.")
        catalogo.renomear_pasta(id_, nome[:80])
    if "pai" in dados:
        destino = str(dados["pai"] or "")
        if destino and not any(p["id"] == destino for p in catalogo.listar_pastas()):
            raise HTTPException(404, "Pasta de destino não encontrada.")
        if not catalogo.mover_pasta(id_, destino):
            raise HTTPException(409, "Uma pasta não pode ser movida para dentro dela mesma.")
    return {"pastas": catalogo.listar_pastas()}


@app.delete("/api/pastas/{id_}")
def excluir_pasta(id_: str, usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    if not any(p["id"] == id_ for p in catalogo.listar_pastas()):
        raise HTTPException(404, "Pasta não encontrada.")
    dentro = catalogo.conteudo_da_pasta(id_)
    if dentro["subpastas"] or dentro["redes"]:
        partes = []
        if dentro["redes"]:
            partes.append(f"{len(dentro['redes'])} rede(s)")
        if dentro["subpastas"]:
            partes.append(f"{len(dentro['subpastas'])} subpasta(s)")
        raise HTTPException(409, f"A pasta ainda tem {' e '.join(partes)}. "
                                 "Mova o conteúdo antes de excluí-la.")
    catalogo.remover_pasta(id_)
    return {"removido": id_}


COMP_Q = Query("", description="Composição: rede:SE1,SE2;outra_rede: (sem códigos = rede inteira)")


def _chave(id_: str, composicao: str) -> str:
    """Aceita tanto `id=<rede>` (rede inteira) quanto `c=<composição>`."""
    if composicao:
        return composicao
    if id_:
        return f"{id_}:"
    raise HTTPException(422, "Informe a rede (id) ou a composição (c).")


@app.get("/api/rede")
def get_rede(id: str = Query("", description="Rede do acervo"), c: str = COMP_Q,
             usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return _executar(_modelo, _chave(id, c), usuario)


@app.get("/api/subestacoes")
def get_subestacoes(id: str = ID_Q, usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    """Subestações da rede, com o peso de cada uma — para escolher antes de abrir a rede toda."""
    registro = _conferir_acesso(id, usuario)
    try:
        model = _rede_bruta(id)
    except interplan.InterplanError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"rede": id, "arquivo": registro["arquivo"], "regiao": registro["regiao"],
            "subestacoes": compor.subestacoes_do_modelo(model)}


@app.get("/api/contexto/vegetacao")
def get_vegetacao(id: str = Query(""), c: str = COMP_Q,
                  usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return _executar(_vegetacao, _chave(id, c), usuario)


@app.get("/api/contexto/predios")
def get_predios(id: str = Query(""), c: str = COMP_Q,
                usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return _executar(_predios, _chave(id, c), usuario)


@app.get("/api/contexto/ruas")
def get_ruas(id: str = Query(""), c: str = COMP_Q,
             usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return _executar(_ruas, _chave(id, c), usuario)


# --- camadas importadas de KML/KMZ --------------------------------------------------------------
@app.get("/api/camadas")
def listar_camadas(usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return {"camadas": catalogo.listar_camadas(_matricula(usuario))}


@app.post("/api/camadas")
async def enviar_camada(
    arquivo: UploadFile = File(..., description="Arquivo KML ou KMZ"),
    descricao: str = Form(""),
    regiao: str = Form(""),
    visibilidade: str = Form(catalogo.PUBLICA),
    usuario: sessao.Usuario = Depends(exigir_login),
) -> dict:
    nome = Path(arquivo.filename or "camada.kml").name
    if Path(nome).suffix.lower() not in (".kml", ".kmz"):
        raise HTTPException(415, "Envie um arquivo .kml ou .kmz.")
    conteudo = await arquivo.read()
    try:
        lido = kml.ler(conteudo, nome)
    except kml.KmlError as exc:
        raise HTTPException(422, str(exc)) from exc

    hash_, destino = catalogo.guardar_arquivo(
        json.dumps(lido, ensure_ascii=False).encode("utf-8"), ".kml.json")
    resumo = {k: lido[k] for k in ("n", "pontos", "bbox", "tipos", "cortado")}
    registro = catalogo.adicionar_camada(hash_, nome, len(conteudo), usuario.nome, descricao.strip(),
                                         resumo, settings.regiao(regiao).sigla, visibilidade,
                                         usuario.matricula)
    aviso = (f"O arquivo tem mais de {settings.kml_max_feicoes} feições; só as primeiras foram lidas."
             if lido["cortado"] else None)
    return {**registro, "aviso": aviso, "caminho": destino.name}


@app.get("/api/camadas/{id_}")
def get_camada(id_: str,
               # Query próprio: o FastAPI fixa o nome do parâmetro no objeto, então reaproveitar
               # o ID_Q aqui faria esta rota exigir "id" em vez de "rede"
               rede: str = Query("", description="Rede onde a camada será posicionada"),
               c: str = COMP_Q,
               usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    """Feições convertidas para o referencial da visualização aberta."""
    registro = catalogo.obter_camada(id_, _matricula(usuario))
    if registro is None:
        raise HTTPException(404, "Camada não encontrada.")
    chave = _chave_conferida(_chave(rede, c), usuario)
    dados = _ler_camada(registro["hash"])
    return {**kml.para_rede(dados, _modelo(chave)), "nome": registro["arquivo"],
            "descricao": registro["descricao"], "id": id_}


def _ler_camada(hash_: str) -> dict:
    arquivo = catalogo.caminho(hash_, ".kml.json")
    if not arquivo.exists():
        raise HTTPException(410, "O arquivo desta camada não está mais no servidor.")
    return json.loads(arquivo.read_text(encoding="utf-8"))


@app.delete("/api/camadas/{id_}")
def excluir_camada(id_: str, usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    registro = catalogo.obter_camada(id_, usuario.matricula)
    if registro is None:
        raise HTTPException(404, "Camada não encontrada.")
    if not _pode_mexer(registro, usuario):
        raise HTTPException(403, "Esta camada é privada de outro usuário.")
    catalogo.remover_camada(id_)
    return {"removido": id_}


# --- projetos em etapas -------------------------------------------------------------------------
@app.get("/api/projetos")
def listar_projetos(usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    return {"projetos": catalogo.listar_projetos(_matricula(usuario))}


@app.post("/api/projetos")
def criar_projeto(dados: dict = Body(...), usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    nome = str(dados.get("nome") or "").strip()
    if not nome:
        raise HTTPException(422, "Dê um nome ao projeto.")
    return catalogo.criar_projeto(nome, str(dados.get("descricao") or "").strip(), usuario.nome,
                                  settings.regiao(dados.get("regiao")).sigla,
                                  str(dados.get("visibilidade") or catalogo.PUBLICA), usuario.matricula)


@app.delete("/api/projetos/{id_}")
def excluir_projeto(id_: str, usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    projeto = catalogo.obter_projeto(id_, usuario.matricula)
    if projeto is None:
        raise HTTPException(404, "Projeto não encontrado.")
    if not _pode_mexer(projeto, usuario):
        raise HTTPException(403, "Este projeto é privado de outro usuário.")
    catalogo.remover_projeto(id_)
    return {"removido": id_}


@app.get("/api/projetos/{id_}")
def get_projeto(id_: str, usuario: sessao.Usuario | None = Depends(usuario_atual)) -> dict:
    projeto = catalogo.obter_projeto(id_, _matricula(usuario))
    if projeto is None:
        raise HTTPException(404, "Projeto não encontrado.")
    return projeto


@app.post("/api/projetos/{id_}/etapas")
def criar_etapa(id_: str, dados: dict = Body(...),
                usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    projeto = catalogo.obter_projeto(id_, usuario.matricula)
    if projeto is None:
        raise HTTPException(404, "Projeto não encontrado.")
    if not _pode_mexer(projeto, usuario):
        raise HTTPException(403, "Este projeto é privado de outro usuário.")
    # salvar a etapa é salvar a visualização: todas as redes e subestações que a compõem
    bruta = str(dados.get("composicao") or "").strip() or f"{str(dados.get('rede_id') or '').strip()}:"
    if bruta == ":":
        raise HTTPException(422, "Abra uma visualização antes de salvar a etapa.")
    pedacos = _ler_composicao(_chave_conferida(bruta, usuario))
    composicao = [{"rede": rede, "subestacoes": list(ses)} for rede, ses in pedacos]
    nome = str(dados.get("nome") or "").strip() or f"Etapa {len(projeto['etapas'])}"
    camadas = [c for c in (dados.get("camadas") or []) if isinstance(c, str)]
    return catalogo.adicionar_etapa(id_, nome, composicao[0]["rede"],
                                    str(dados.get("descricao") or "").strip(), camadas,
                                    composicao=composicao)


@app.delete("/api/etapas/{id_}")
def excluir_etapa(id_: str, usuario: sessao.Usuario = Depends(exigir_login)) -> dict:
    projeto_id = catalogo.remover_etapa(id_)
    if projeto_id is None:
        raise HTTPException(404, "Etapa não encontrada.")
    return {"removido": id_, "projeto": projeto_id}


# --- exportação para KMZ -------------------------------------------------------------------------
@app.post("/api/exportar/kmz")
def exportar_kmz(dados: dict = Body(...), usuario: sessao.Usuario | None = Depends(usuario_atual)):
    """Monta o KMZ do que está visível: camadas e circuitos desmarcados ficam de fora."""
    chave = _chave_conferida(_chave(str(dados.get("id") or ""), str(dados.get("c") or "")), usuario)
    visiveis = {str(c) for c in (dados.get("camadas") or [])}
    if not visiveis:
        raise HTTPException(422, "Nenhuma camada visível para exportar.")
    circuitos = dados.get("circuitos")
    circuitos = None if circuitos is None else {c for c in circuitos}
    model = _modelo(chave)

    veg = None
    if "folga" in visiveis:
        try:
            veg = _vegetacao(chave)
        except (HTTPException, OSError, RuntimeError):
            veg = None  # sem a análise de vegetação o resto do KMZ continua valendo

    camadas = []
    for camada_id in (dados.get("importadas") or []):
        reg = catalogo.obter_camada(str(camada_id), _matricula(usuario))
        if reg is not None:
            camadas.append({"nome": reg["arquivo"], **_ler_camada(reg["hash"])})

    conteudo = kml.exportar_kmz(model, visiveis, circuitos, veg, camadas)
    nome = "".join(ch for ch in (model["meta"].get("nome") or "expansion3d")
                   if ch.isalnum() or ch in " -_") .strip() or "expansion3d"
    return Response(
        content=conteudo, media_type="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": f'attachment; filename="{nome}.kmz"'},
    )


@lru_cache(maxsize=4)
def _para_geo(epsg: int) -> Transformer:
    return Transformer.from_crs(epsg, 4326, always_xy=True)


@app.get("/api/geo")
def get_geo(e: float, n: float, epsg: int | None = None) -> dict:
    """Converte coordenada UTM em lat/lon (usado para abrir o ponto no Street View)."""
    lon, lat = _para_geo(epsg or settings.epsg).transform(e, n)
    return {"lat": round(lat, 7), "lon": round(lon, 7)}


class SemCache(StaticFiles):
    """Arquivos da aplicação sempre revalidados: sem isso um navegador pode ficar com o JS antigo."""

    def file_response(self, *args, **kwargs):  # o Starlette passa o scope posicionalmente
        resposta = super().file_response(*args, **kwargs)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


app.mount("/vendor/three", StaticFiles(directory=THREE_DIR), name="three")
app.mount("/static", SemCache(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})
