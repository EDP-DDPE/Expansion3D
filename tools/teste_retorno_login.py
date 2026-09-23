"""Confere o endereço de retorno do login, inclusive com proxy no caminho.

O erro que motivou este teste: com um proxy mandando `X-Forwarded-Proto: https`, o endereço de
retorno saía como `https://host:8010`, mas a 8010 responde em HTTP puro — o navegador mostrava
"a conexão com este site não é segura / resposta inválida". Com `url_publica` no conf.ini o
endereço passa a ser sempre o configurado.

Precisa do serviço no ar. Use: python tools/teste_retorno_login.py
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

BASE = "http://127.0.0.1:8010"
falhas = 0


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    if not condicao:
        falhas += 1
    print(f"{'ok  ' if condicao else 'FALHA'} {rotulo}{f' · {detalhe}' if detalhe else ''}")


def retorno_de(caminho: str, cabecalhos: dict) -> str:
    """Segue /auth/entrar sem redirecionar e devolve o `retorno` mandado ao Atlas."""
    class SemSeguir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    abridor = urllib.request.build_opener(SemSeguir)
    req = urllib.request.Request(f"{BASE}{caminho}", headers=cabecalhos)
    try:
        with abridor.open(req, timeout=20) as resp:
            destino = resp.headers.get("location", "")
    except urllib.error.HTTPError as err:
        destino = err.headers.get("location", "")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(destino).query)
    return query.get("retorno", [""])[0]


def main() -> int:
    esperado = settings.url_publica or "(deduzido da requisição)"
    print(f"url_publica no conf.ini: {esperado}\n")

    casos = [
        ("sem proxy", {}),
        ("proxy dizendo https", {"X-Forwarded-Proto": "https"}),
        ("proxy dizendo https + host", {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "outro.local"}),
        ("host trocado", {"Host": "172.20.70.54:8010"}),
    ]
    for rotulo, cabecalhos in casos:
        retorno = retorno_de("/auth/entrar", cabecalhos)
        conferir(f"{rotulo}: retorno = {settings.url_publica}/auth/retorno",
                 retorno == f"{settings.url_publica}/auth/retorno", retorno or "(vazio)")

    # o esquema precisa bater com a porta, senão o navegador recusa a conexão
    conferir("esquema do retorno é o mesmo do conf.ini",
             retorno_de("/auth/entrar", {"X-Forwarded-Proto": "https"}).startswith(
                 settings.url_publica.split("://")[0] + "://"))

    quadro = retorno_de("/auth/entrar?auto=1&quadro=1", {"X-Forwarded-Proto": "https"})
    conferir("tentativa silenciosa mantém o quadro=1",
             quadro == f"{settings.url_publica}/auth/retorno?quadro=1", quadro)

    # O cookie precisa acompanhar o esquema do endereço público: marcado como Secure numa conexão
    # http, o navegador descarta sem avisar e o login falha em silêncio.
    from app import sessao  # importado aqui para o teste rodar mesmo sem o serviço no ar

    ticket = sessao.assinar({"matricula": "0000", "nome": "Teste de login",
                             "email": "", "admin": False,
                             "ts": int(__import__("time").time())}, settings.atlas_segredo)

    class SemSeguir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    abridor = urllib.request.build_opener(SemSeguir)
    req = urllib.request.Request(f"{BASE}/auth/retorno?ticket={urllib.parse.quote(ticket)}",
                                 headers={"X-Forwarded-Proto": "https"})
    try:
        with abridor.open(req, timeout=20) as resp:
            cookie = resp.headers.get("set-cookie", "")
    except urllib.error.HTTPError as err:
        cookie = err.headers.get("set-cookie", "")

    https = settings.url_publica.startswith("https://")
    tem_secure = "secure" in cookie.lower()
    conferir("cookie de sessão foi criado", sessao.COOKIE in cookie, cookie[:60] or "(nenhum)")
    conferir(f"cookie {'com' if https else 'sem'} Secure, como o endereço público",
             tem_secure == https, f"Secure={'sim' if tem_secure else 'nao'}")
    conferir("cookie é HttpOnly", "httponly" in cookie.lower())

    print("\nresultado:", "tudo certo" if not falhas else f"{falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
