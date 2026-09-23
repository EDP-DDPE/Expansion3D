"""Confere que a volta do Atlas para o Expansion não usa o cabeçalho Location.

Motivo: o nginx à frente do Atlas reescreve `http://` para `https://` em Location
(`proxy_redirect`). Como o Expansion responde em HTTP puro na porta 8010, o endereço reescrito
dava "a conexão com este site não é segura / resposta inválida". A volta passou a ser feita pela
própria página, que o nginx não reescreve.

Precisa do Flask, que está no venv do Atlas. Rode assim (ajuste o caminho se for outro):

    & "C:\\...\\GCIWeb\\.venv\\Scripts\\python.exe" tools\\teste_volta_atlas.py
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ATLAS_PADRAO = Path(r"C:\Users\7034\EDP\O365_Planejamento da Expansao - Documentos"
                    r"\Governança\Desenvolvimentos DDPE\GCIWeb")
falhas = 0


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    if not condicao:
        falhas += 1
    print(f"{'ok  ' if condicao else 'FALHA'} {rotulo}{f' · {detalhe}' if detalhe else ''}")


def carregar_blueprint(raiz: Path):
    """Importa o blueprint real do Atlas, com app.auth trocado por um dublê."""
    auth = types.ModuleType("app.auth")
    auth.get_usuario_logado = lambda: None
    pacote = types.ModuleType("app")
    pacote.__path__ = []
    sys.modules.setdefault("app", pacote)
    sys.modules["app.auth"] = auth

    caminho = raiz / "app" / "expansion" / "routes.py"
    spec = importlib.util.spec_from_file_location("atlas_expansion_routes", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def main() -> int:
    try:
        from flask import Flask
    except ImportError:
        print("Flask não está neste interpretador. Rode com o python do venv do Atlas:")
        print(r'  & "...\GCIWeb\.venv\Scripts\python.exe" tools\teste_volta_atlas.py')
        return 1

    import os

    raiz = Path(sys.argv[1]) if len(sys.argv) > 1 else ATLAS_PADRAO
    if not (raiz / "app" / "expansion" / "routes.py").exists():
        print(f"Não achei o blueprint em {raiz}. Passe o caminho do GCIWeb como argumento.")
        return 1

    os.environ["EXPANSION_URL"] = "http://172.20.70.54:8010"
    os.environ["EXPANSION_SECRET"] = "segredo-de-teste"
    atlas = carregar_blueprint(raiz)

    app = Flask(__name__)
    app.secret_key = "teste"
    app.register_blueprint(atlas.expansion_bp)
    cliente = app.test_client()

    retorno = "http://172.20.70.54:8010/auth/retorno"
    alvo = f"/api/expansion/ticket?retorno={retorno}&auto=0"

    # --- sem sessão no Atlas: volta com ticket=nenhum -------------------------------------
    r = cliente.get(alvo)
    corpo = r.get_data(as_text=True)
    conferir("responde 200 (página), não 302", r.status_code == 200, f"HTTP {r.status_code}")
    conferir("não manda cabeçalho Location", "Location" not in r.headers,
             r.headers.get("Location", "(nenhum)"))
    conferir("página leva o endereço em http", "http://172.20.70.54:8010/auth/retorno" in corpo)
    conferir("não virou https em lugar nenhum", "https://172.20.70.54:8010" not in corpo)
    conferir("sem sessão, ticket=nenhum", "ticket=nenhum" in corpo)

    # --- com sessão: volta com o ticket assinado ------------------------------------------
    with cliente.session_transaction() as sessao:
        sessao["user"] = {"preferred_username": "7034@edp.com", "name": "Jader Kayque"}
    r = cliente.get(alvo)
    corpo = r.get_data(as_text=True)
    conferir("com sessão, emite ticket", "ticket=eyJ" in corpo,
             "ticket presente" if "ticket=eyJ" in corpo else corpo[:120])
    conferir("continua sem Location", "Location" not in r.headers)
    conferir("continua em http", "https://172.20.70.54:8010" not in corpo)

    # --- retorno de fora continua recusado -------------------------------------------------
    r = cliente.get("/api/expansion/ticket?retorno=https://site-de-fora.com/x&auto=0")
    conferir("retorno de fora é recusado", r.status_code == 400, f"HTTP {r.status_code}")

    # --- esquema trocado também é recusado, com mensagem clara ------------------------------
    r = cliente.get("/api/expansion/ticket?retorno=https://172.20.70.54:8010/auth/retorno")
    conferir("esquema trocado é recusado", r.status_code == 400, f"HTTP {r.status_code}")
    conferir("a recusa explica o que esperava",
             "EXPANSION_URL" in r.get_data(as_text=True))

    print("\nresultado:", "tudo certo" if not falhas else f"{falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
