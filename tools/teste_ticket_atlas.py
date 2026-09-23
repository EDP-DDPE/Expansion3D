"""Confere que o ticket emitido pelo Atlas é aceito pelo Expansion (e só com o segredo certo).

Importa o arquivo de rotas real do Atlas, com Flask trocado por dublês, para testar a função de
assinatura de verdade — e não uma cópia que poderia sair do lugar depois.

Use: python tools/teste_ticket_atlas.py [caminho do GCIWeb]
"""
from __future__ import annotations

import html
import importlib.util
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import sessao  # noqa: E402

ATLAS_PADRAO = Path(r"C:\Users\7034\EDP\O365_Planejamento da Expansao - Documentos"
                    r"\Governança\Desenvolvimentos DDPE\GCIWeb")
SEGREDO = "segredo-compartilhado-de-teste"
falhas = 0


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    if not condicao:
        falhas += 1
    print(f"{'ok  ' if condicao else 'FALHA'} {rotulo}{f' · {detalhe}' if detalhe else ''}")


def carregar_rotas_do_atlas(raiz: Path):
    """Carrega app/expansion/routes.py do Atlas com dublês no lugar do Flask."""
    flask = types.ModuleType("flask")
    flask.Blueprint = lambda *a, **k: types.SimpleNamespace(route=lambda *a, **k: (lambda f: f))
    flask.current_app = types.SimpleNamespace(logger=types.SimpleNamespace(
        warning=lambda *a, **k: None, error=lambda *a, **k: None))
    flask.redirect = lambda destino: destino
    # a montagem da página de volta é testada com Flask de verdade em teste_volta_atlas.py;
    # aqui o dublê só precisa existir para o import do módulo funcionar
    flask.render_template_string = lambda modelo, **ctx: modelo
    flask.request = types.SimpleNamespace(args={})
    flask.session = {}
    # o markupsafe vive no venv do Atlas; aqui o dublê usa o escape da biblioteca padrão, que
    # faz a mesma coisa para texto — quem confere a segurança da página é o teste_volta_atlas.py
    markupsafe = types.ModuleType("markupsafe")
    markupsafe.escape = lambda valor: html.escape(str(valor), quote=True)
    sys.modules.setdefault("markupsafe", markupsafe)

    auth = types.ModuleType("app.auth")
    auth.get_usuario_logado = lambda: None
    pacote = types.ModuleType("app")
    pacote.__path__ = []
    sys.modules.update({"flask": flask, "app": pacote, "app.auth": auth})

    caminho = raiz / "app" / "expansion" / "routes.py"
    if not caminho.exists():
        raise FileNotFoundError(caminho)
    spec = importlib.util.spec_from_file_location("atlas_expansion_routes", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def main() -> int:
    raiz = Path(sys.argv[1]) if len(sys.argv) > 1 else ATLAS_PADRAO
    try:
        atlas = carregar_rotas_do_atlas(raiz)
    except FileNotFoundError as exc:
        print(f"Não achei o arquivo do Atlas: {exc}")
        print("Passe o caminho do GCIWeb como argumento.")
        return 1

    payload = {"matricula": "7034", "nome": "Jader", "email": "jader@edp.com",
               "admin": False, "ts": int(time.time())}
    ticket = atlas._assinar(payload, SEGREDO)

    lido = sessao.conferir(ticket, SEGREDO, 120)
    conferir("Expansion aceita o ticket do Atlas", lido["matricula"] == "7034", f"nome={lido['nome']}")
    conferir("os dois lados geram o mesmo token", ticket == sessao.assinar(payload, SEGREDO))

    conferir("ticket carrega nome e perfil", lido["nome"] == "Jader" and lido["admin"] is False)

    for rotulo, tok, seg in (
        ("segredo diferente", ticket, "outro-segredo"),
        ("token adulterado", ticket[:-4] + "AAAA", SEGREDO),
    ):
        try:
            sessao.conferir(tok, seg, 120)
            conferir(f"recusa {rotulo}", False, "aceitou")
        except sessao.TicketInvalido as exc:
            conferir(f"recusa {rotulo}", True, str(exc))

    velho = atlas._assinar({**payload, "ts": int(time.time()) - 3600}, SEGREDO)
    try:
        sessao.conferir(velho, SEGREDO, 120)
        conferir("recusa ticket vencido", False, "aceitou")
    except sessao.TicketInvalido as exc:
        conferir("recusa ticket vencido", True, str(exc))

    # o retorno só pode apontar para o endereço configurado do Expansion
    import os
    os.environ["EXPANSION_URL"] = "http://172.20.70.54:8010"
    casos = [
        ("http://172.20.70.54:8010/auth/retorno", True),
        ("http://172.20.70.54:8010/qualquer/caminho", True),
        ("https://site-de-fora.com/roubar", False),
        ("http://172.20.70.54:9999/outra-porta", False),
        # esquema trocado: a 8010 responde em http, e mandar o ticket para https dava
        # "resposta inválida" no navegador — melhor recusar aqui, com mensagem clara
        ("https://172.20.70.54:8010/auth/retorno", False),
        ("", False),
    ]
    for url, esperado in casos:
        conferir(f"retorno {'aceito' if esperado else 'recusado'}: {url or '(vazio)'}",
                 atlas._retorno_confiavel(url) is esperado)

    print("\nresultado:", "tudo certo" if not falhas else f"{falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
