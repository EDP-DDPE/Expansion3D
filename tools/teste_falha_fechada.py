"""Confere que uma configuração de login incompleta NÃO libera o sistema.

O risco: `[atlas] ativo = sim` com `segredo` em branco é um erro de configuração — um conf.ini
novo, ou a frase secreta esquecida. Se isso caísse no mesmo caminho de "integração desligada",
todo visitante viraria administrador e poderia excluir redes. Aqui o esperado é o contrário:
modo Visualização, e as alterações recusadas com uma mensagem que diz o que falta.

Roda com um conf.ini temporário, sem tocar no do projeto.
Use: python tools/teste_falha_fechada.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# O que roda no subprocesso, já com o conf.ini temporário apontado por EXPANSION3D_CONF.
SONDA = r'''
import json, sys
sys.path.insert(0, r"{raiz}")
from app.config import settings
from app.server import usuario_atual, exigir_login
from fastapi import HTTPException

class Req:
    cookies = {{}}

u = usuario_atual(Req())
try:
    exigir_login(u)
    erro = None
except HTTPException as exc:
    erro = {{"status": exc.status_code, "detalhe": exc.detail}}

print(json.dumps({{
    "ativo": settings.atlas_ativo,
    "sso_pronto": settings.sso_pronto,
    "usuario": None if u is None else u.matricula,
    "admin": bool(u and u.admin),
    "erro": erro,
}}, ensure_ascii=False))
'''

falhas = 0


def conferir(rotulo: str, condicao: bool, detalhe: str = "") -> None:
    global falhas
    if not condicao:
        falhas += 1
    print(f"{'ok  ' if condicao else 'FALHA'} {rotulo}{f' · {detalhe}' if detalhe else ''}")


def sondar(conteudo: str) -> dict:
    """Sobe o app com um conf.ini temporário e pergunta quem é o usuário sem cookie nenhum."""
    import json

    with tempfile.TemporaryDirectory() as pasta:
        conf = Path(pasta) / "conf.ini"
        conf.write_text(conteudo, encoding="utf-8")
        ambiente = {**os.environ, "EXPANSION3D_CONF": str(conf)}
        saida = subprocess.run([sys.executable, "-c", SONDA.format(raiz=RAIZ)],
                               capture_output=True, text=True, env=ambiente, cwd=RAIZ, timeout=180)
        if saida.returncode != 0:
            raise RuntimeError(saida.stderr[-800:])
        return json.loads(saida.stdout.strip().splitlines()[-1])


def main() -> int:
    base = "[dados]\npasta = data\n\n[atlas]\n"

    # 1) integração ligada, sem segredo: erro de configuração
    r = sondar(base + "ativo = sim\nsegredo =\n")
    conferir("ligado sem segredo: ninguém é admin", not r["admin"], str(r))
    conferir("ligado sem segredo: fica em modo Visualização", r["usuario"] is None)
    conferir("ligado sem segredo: alteração é recusada", r["erro"] is not None
             and r["erro"]["status"] == 503, str(r["erro"]))
    conferir("e a recusa explica o que falta",
             r["erro"] and "segredo" in r["erro"]["detalhe"], (r["erro"] or {}).get("detalhe", "")[:70])

    # 2) integração desligada de propósito: libera, como antes
    r = sondar(base + "ativo = nao\nsegredo =\n")
    conferir("desligado de propósito: libera o uso local", r["usuario"] == "local" and r["admin"],
             str(r))

    # 3) integração ligada e configurada: exige login
    r = sondar(base + "ativo = sim\nsegredo = frase-de-teste-que-nao-vai-para-o-git\n")
    conferir("ligado e configurado: exige login", r["usuario"] is None and not r["admin"])
    conferir("ligado e configurado: recusa com 401", r["erro"] and r["erro"]["status"] == 401,
             str(r["erro"]))

    # 4) o modelo embutido no código não pode trazer segredo nenhum
    from app.config import MODELO_CONF  # noqa: PLC0415

    linha = next((l for l in MODELO_CONF.splitlines() if l.strip().startswith("segredo")), "")
    valor = linha.partition("=")[2].strip()
    conferir("o modelo do conf.ini não traz segredo embutido", valor == "",
             f"segredo padrão = {valor!r}")

    print("\nresultado:", "tudo certo" if not falhas else f"{falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
