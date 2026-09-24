"""Acervo compartilhado: bases do Interplan, camadas KML/KMZ e projetos em etapas (SQLite + arquivos).

Visibilidade: "publica" aparece para todo mundo, inclusive em modo Visualização; "privada" só
aparece para a matrícula que enviou. Como o filtro é aplicado no SQL, uma rede privada nunca
chega ao navegador de outro usuário.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .config import settings

PUBLICA, PRIVADA = "publica", "privada"

SCHEMA = """
CREATE TABLE IF NOT EXISTS redes (
    id TEXT PRIMARY KEY,
    arquivo TEXT NOT NULL,
    hash TEXT NOT NULL,
    tamanho INTEGER NOT NULL,
    usuario TEXT NOT NULL,
    descricao TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    resumo TEXT NOT NULL,
    regiao TEXT NOT NULL DEFAULT '',
    epsg INTEGER NOT NULL DEFAULT 0,
    visibilidade TEXT NOT NULL DEFAULT 'publica',
    dono TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS redes_hash ON redes (hash);

CREATE TABLE IF NOT EXISTS camadas (
    id TEXT PRIMARY KEY,
    arquivo TEXT NOT NULL,
    hash TEXT NOT NULL,
    tamanho INTEGER NOT NULL,
    usuario TEXT NOT NULL,
    descricao TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    resumo TEXT NOT NULL,
    regiao TEXT NOT NULL DEFAULT '',
    visibilidade TEXT NOT NULL DEFAULT 'publica',
    dono TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS projetos (
    id TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    descricao TEXT NOT NULL,
    usuario TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    regiao TEXT NOT NULL DEFAULT '',
    visibilidade TEXT NOT NULL DEFAULT 'publica',
    dono TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS etapas (
    id TEXT PRIMARY KEY,
    projeto_id TEXT NOT NULL,
    ordem INTEGER NOT NULL,
    nome TEXT NOT NULL,
    descricao TEXT NOT NULL DEFAULT '',
    rede_id TEXT NOT NULL DEFAULT '',
    camadas TEXT NOT NULL DEFAULT '[]',
    composicao TEXT NOT NULL DEFAULT '[]',
    criado_em TEXT NOT NULL,
    FOREIGN KEY (projeto_id) REFERENCES projetos (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS etapas_projeto ON etapas (projeto_id, ordem);

CREATE TABLE IF NOT EXISTS pastas (
    id TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    pai TEXT NOT NULL DEFAULT '',
    usuario TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pastas_pai ON pastas (pai);
"""

# colunas acrescentadas depois da primeira versão do banco, com o valor usado nas linhas antigas
MIGRACOES = {
    "redes": (("regiao", "TEXT", lambda: f"'{settings.regiao_padrao}'"), ("epsg", "INTEGER", lambda: settings.epsg),
              ("visibilidade", "TEXT", lambda: f"'{PUBLICA}'"), ("dono", "TEXT", lambda: "''"),
              ("pasta", "TEXT", lambda: "''")),
    "etapas": (("composicao", "TEXT", lambda: "'[]'"),),
}


def _conn() -> sqlite3.Connection:
    cn = sqlite3.connect(settings.banco, timeout=15)
    cn.row_factory = sqlite3.Row
    cn.execute("PRAGMA journal_mode=WAL")  # leitura simultânea de vários usuários
    cn.execute("PRAGMA foreign_keys=ON")   # apagar um projeto leva junto as etapas
    cn.executescript(SCHEMA)
    for tabela, colunas in MIGRACOES.items():
        existentes = {linha["name"] for linha in cn.execute(f"PRAGMA table_info({tabela})")}
        for nome, tipo, padrao in colunas:
            if nome not in existentes:
                cn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo} NOT NULL DEFAULT {padrao()}")
    return cn


def caminho(hash_: str, sufixo: str = ".mdb") -> Path:
    """Arquivo em disco; conteúdos idênticos são guardados uma vez só."""
    return settings.redes_dir / f"{hash_}{sufixo}"


def _linha(row: sqlite3.Row) -> dict:
    registro = dict(row)
    if "resumo" in registro:
        registro["resumo"] = json.loads(registro["resumo"])
    if "camadas" in registro:
        registro["camadas"] = json.loads(registro["camadas"])
    if "composicao" in registro:
        registro["composicao"] = json.loads(registro["composicao"] or "[]")
        # etapas antigas guardavam só a rede inteira; viram uma composição de um pedaço só
        if not registro["composicao"] and registro.get("rede_id"):
            registro["composicao"] = [{"rede": registro["rede_id"], "subestacoes": []}]
    return registro


def _filtro(matricula: str | None) -> tuple[str, list]:
    """Cláusula que esconde o que é privado de outra pessoa."""
    if matricula:
        return "(visibilidade = ? OR dono = ?)", [PUBLICA, matricula]
    return "visibilidade = ?", [PUBLICA]


def guardar_arquivo(conteudo: bytes, sufixo: str = ".mdb") -> tuple[str, Path]:
    hash_ = hashlib.sha1(conteudo).hexdigest()
    destino = caminho(hash_, sufixo)
    if not destino.exists():
        destino.parent.mkdir(parents=True, exist_ok=True)
        provisorio = destino.with_suffix(".parte")
        provisorio.write_bytes(conteudo)
        provisorio.replace(destino)
    return hash_, destino


# --- redes do Interplan -------------------------------------------------------------------------
def listar(matricula: str | None = None) -> list[dict]:
    onde, args = _filtro(matricula)
    with closing(_conn()) as cn:
        return [_linha(r) for r in cn.execute(f"SELECT * FROM redes WHERE {onde} ORDER BY criado_em DESC", args)]


def obter(id_: str, matricula: str | None = None) -> dict | None:
    """Uma rede privada de outra pessoa responde como inexistente."""
    onde, args = _filtro(matricula)
    with closing(_conn()) as cn:
        row = cn.execute(f"SELECT * FROM redes WHERE id = ? AND {onde}", [id_, *args]).fetchone()
    return _linha(row) if row else None


def obter_bruto(id_: str) -> dict | None:
    """Sem filtro de visibilidade: só para uso interno, depois de conferido o acesso."""
    with closing(_conn()) as cn:
        row = cn.execute("SELECT * FROM redes WHERE id = ?", (id_,)).fetchone()
    return _linha(row) if row else None


def adicionar(hash_: str, arquivo: str, tamanho: int, usuario: str, descricao: str, resumo: dict,
              regiao: str, epsg: int, visibilidade: str = PUBLICA, dono: str = "") -> dict:
    registro = {
        "id": uuid.uuid4().hex[:12],
        "arquivo": arquivo,
        "hash": hash_,
        "tamanho": tamanho,
        "usuario": usuario,
        "descricao": descricao,
        "criado_em": datetime.now().astimezone().isoformat(timespec="seconds"),
        "resumo": resumo,
        "regiao": regiao,
        "epsg": epsg,
        "visibilidade": visibilidade if visibilidade in (PUBLICA, PRIVADA) else PUBLICA,
        "dono": dono,
    }
    with closing(_conn()) as cn, cn:
        cn.execute(
            "INSERT INTO redes (id, arquivo, hash, tamanho, usuario, descricao, criado_em, resumo, regiao,"
            " epsg, visibilidade, dono) VALUES (:id, :arquivo, :hash, :tamanho, :usuario, :descricao,"
            " :criado_em, :resumo, :regiao, :epsg, :visibilidade, :dono)",
            {**registro, "resumo": json.dumps(resumo, ensure_ascii=False)},
        )
    return registro


def mudar_visibilidade(id_: str, visibilidade: str, dono: str = "") -> bool:
    """Troca entre pública e privada.

    `dono` preenche o campo quando a rede ainda não tem um. Redes enviadas antes do login do
    Atlas ficaram sem dono; sem isso, torná-las privadas as esconderia de todo mundo, já que
    o filtro compara a matrícula e nenhuma bate com vazio.
    """
    if visibilidade not in (PUBLICA, PRIVADA):
        return False
    with closing(_conn()) as cn, cn:
        if dono:
            cur = cn.execute(
                "UPDATE redes SET visibilidade = ?, dono = CASE WHEN dono = '' THEN ? ELSE dono END"
                " WHERE id = ?", (visibilidade, dono, id_))
        else:
            cur = cn.execute("UPDATE redes SET visibilidade = ? WHERE id = ?", (visibilidade, id_))
    return cur.rowcount > 0


def mover(id_: str, pasta: str) -> bool:
    """Muda a rede de pasta. Os projetos referenciam a rede pelo id, então nada mais muda."""
    with closing(_conn()) as cn, cn:
        cur = cn.execute("UPDATE redes SET pasta = ? WHERE id = ?", (pasta or "", id_))
    return cur.rowcount > 0


def projetos_que_usam(rede_id: str) -> list[dict]:
    """Projetos cujas etapas dependem desta rede — usado para barrar a exclusão."""
    with closing(_conn()) as cn:
        linhas = cn.execute(
            "SELECT p.id AS projeto, p.nome AS projeto_nome, p.visibilidade, p.dono,"
            "       e.id AS etapa, e.nome AS etapa_nome, e.rede_id, e.composicao"
            "  FROM etapas e JOIN projetos p ON p.id = e.projeto_id").fetchall()
    usos = []
    for linha in linhas:
        pedacos = json.loads(linha["composicao"] or "[]")
        redes = {p.get("rede") for p in pedacos} or {linha["rede_id"]}
        if rede_id in redes:
            usos.append({"projeto": linha["projeto"], "projeto_nome": linha["projeto_nome"],
                         "etapa": linha["etapa"], "etapa_nome": linha["etapa_nome"],
                         "visibilidade": linha["visibilidade"], "dono": linha["dono"]})
    return usos


def remover(id_: str) -> bool:
    """Remove o registro; o arquivo só sai do disco quando nenhum outro registro o usa."""
    with closing(_conn()) as cn, cn:
        row = cn.execute("SELECT hash FROM redes WHERE id = ?", (id_,)).fetchone()
        if row is None:
            return False
        cn.execute("DELETE FROM redes WHERE id = ?", (id_,))
        restantes = cn.execute("SELECT COUNT(*) FROM redes WHERE hash = ?", (row["hash"],)).fetchone()[0]
    if not restantes:
        caminho(row["hash"]).unlink(missing_ok=True)
    return True


# --- pastas do acervo ----------------------------------------------------------------------------
def listar_pastas() -> list[dict]:
    """Pastas são compartilhadas, como o próprio acervo."""
    with closing(_conn()) as cn:
        return [dict(r) for r in cn.execute("SELECT * FROM pastas ORDER BY nome")]


def criar_pasta(nome: str, pai: str, usuario: str) -> dict:
    registro = {"id": uuid.uuid4().hex[:12], "nome": nome, "pai": pai or "",
                "usuario": usuario,
                "criado_em": datetime.now().astimezone().isoformat(timespec="seconds")}
    with closing(_conn()) as cn, cn:
        cn.execute("INSERT INTO pastas (id, nome, pai, usuario, criado_em)"
                   " VALUES (:id, :nome, :pai, :usuario, :criado_em)", registro)
    return registro


def conteudo_da_pasta(id_: str) -> dict:
    """O que impede a pasta de ser apagada: subpastas e redes dentro dela."""
    with closing(_conn()) as cn:
        subpastas = [dict(r) for r in cn.execute("SELECT id, nome FROM pastas WHERE pai = ?", (id_,))]
        redes = [dict(r) for r in cn.execute("SELECT id, arquivo FROM redes WHERE pasta = ?", (id_,))]
    return {"subpastas": subpastas, "redes": redes}


def remover_pasta(id_: str) -> bool:
    with closing(_conn()) as cn, cn:
        cur = cn.execute("DELETE FROM pastas WHERE id = ?", (id_,))
    return cur.rowcount > 0


def renomear_pasta(id_: str, nome: str) -> bool:
    with closing(_conn()) as cn, cn:
        cur = cn.execute("UPDATE pastas SET nome = ? WHERE id = ?", (nome, id_))
    return cur.rowcount > 0


def descendentes(id_: str) -> set:
    """Todas as pastas abaixo desta, para impedir que uma pasta seja movida para dentro de si."""
    pastas = listar_pastas()
    filhos: dict = {}
    for p in pastas:
        filhos.setdefault(p["pai"], []).append(p["id"])
    achados: set = set()
    fila = [id_]
    while fila:
        atual = fila.pop()
        for filho in filhos.get(atual, []):
            if filho not in achados:
                achados.add(filho)
                fila.append(filho)
    return achados


def mover_pasta(id_: str, pai: str) -> bool:
    pai = pai or ""
    if pai and (pai == id_ or pai in descendentes(id_)):
        return False  # viraria um ciclo, e a pasta sumiria da árvore
    with closing(_conn()) as cn, cn:
        cur = cn.execute("UPDATE pastas SET pai = ? WHERE id = ?", (pai, id_))
    return cur.rowcount > 0


# --- camadas importadas de KML/KMZ --------------------------------------------------------------
def listar_camadas(matricula: str | None = None) -> list[dict]:
    onde, args = _filtro(matricula)
    with closing(_conn()) as cn:
        return [_linha(r) for r in cn.execute(f"SELECT * FROM camadas WHERE {onde} ORDER BY criado_em DESC", args)]


def obter_camada(id_: str, matricula: str | None = None) -> dict | None:
    onde, args = _filtro(matricula)
    with closing(_conn()) as cn:
        row = cn.execute(f"SELECT * FROM camadas WHERE id = ? AND {onde}", [id_, *args]).fetchone()
    return _linha(row) if row else None


def adicionar_camada(hash_: str, arquivo: str, tamanho: int, usuario: str, descricao: str,
                     resumo: dict, regiao: str, visibilidade: str = PUBLICA, dono: str = "") -> dict:
    registro = {
        "id": uuid.uuid4().hex[:12],
        "arquivo": arquivo,
        "hash": hash_,
        "tamanho": tamanho,
        "usuario": usuario,
        "descricao": descricao,
        "criado_em": datetime.now().astimezone().isoformat(timespec="seconds"),
        "resumo": resumo,
        "regiao": regiao,
        "visibilidade": visibilidade if visibilidade in (PUBLICA, PRIVADA) else PUBLICA,
        "dono": dono,
    }
    with closing(_conn()) as cn, cn:
        cn.execute(
            "INSERT INTO camadas (id, arquivo, hash, tamanho, usuario, descricao, criado_em, resumo,"
            " regiao, visibilidade, dono) VALUES (:id, :arquivo, :hash, :tamanho, :usuario, :descricao,"
            " :criado_em, :resumo, :regiao, :visibilidade, :dono)",
            {**registro, "resumo": json.dumps(resumo, ensure_ascii=False)},
        )
    return registro


def remover_camada(id_: str) -> bool:
    with closing(_conn()) as cn, cn:
        row = cn.execute("SELECT hash FROM camadas WHERE id = ?", (id_,)).fetchone()
        if row is None:
            return False
        cn.execute("DELETE FROM camadas WHERE id = ?", (id_,))
        restantes = cn.execute("SELECT COUNT(*) FROM camadas WHERE hash = ?", (row["hash"],)).fetchone()[0]
    if not restantes:
        caminho(row["hash"], ".kml.json").unlink(missing_ok=True)
    return True


# --- projetos em etapas -------------------------------------------------------------------------
def listar_projetos(matricula: str | None = None) -> list[dict]:
    onde, args = _filtro(matricula)
    with closing(_conn()) as cn:
        projetos = [_linha(r) for r in
                    cn.execute(f"SELECT * FROM projetos WHERE {onde} ORDER BY criado_em DESC", args)]
        for p in projetos:
            p["etapas"] = [_linha(r) for r in cn.execute(
                "SELECT * FROM etapas WHERE projeto_id = ? ORDER BY ordem", (p["id"],))]
    return projetos


def obter_projeto(id_: str, matricula: str | None = None) -> dict | None:
    onde, args = _filtro(matricula)
    with closing(_conn()) as cn:
        row = cn.execute(f"SELECT * FROM projetos WHERE id = ? AND {onde}", [id_, *args]).fetchone()
        if row is None:
            return None
        projeto = _linha(row)
        projeto["etapas"] = [_linha(r) for r in cn.execute(
            "SELECT * FROM etapas WHERE projeto_id = ? ORDER BY ordem", (id_,))]
    return projeto


def criar_projeto(nome: str, descricao: str, usuario: str, regiao: str,
                  visibilidade: str = PUBLICA, dono: str = "") -> dict:
    registro = {
        "id": uuid.uuid4().hex[:12],
        "nome": nome,
        "descricao": descricao,
        "usuario": usuario,
        "criado_em": datetime.now().astimezone().isoformat(timespec="seconds"),
        "regiao": regiao,
        "visibilidade": visibilidade if visibilidade in (PUBLICA, PRIVADA) else PUBLICA,
        "dono": dono,
    }
    with closing(_conn()) as cn, cn:
        cn.execute(
            "INSERT INTO projetos (id, nome, descricao, usuario, criado_em, regiao, visibilidade, dono)"
            " VALUES (:id, :nome, :descricao, :usuario, :criado_em, :regiao, :visibilidade, :dono)", registro)
    registro["etapas"] = []
    return registro


def remover_projeto(id_: str) -> bool:
    with closing(_conn()) as cn, cn:
        cur = cn.execute("DELETE FROM projetos WHERE id = ?", (id_,))
    return cur.rowcount > 0


def adicionar_etapa(projeto_id: str, nome: str, rede_id: str, descricao: str = "",
                    camadas: list[str] | None = None, ordem: int | None = None,
                    composicao: list[dict] | None = None) -> dict:
    """Guarda a etapa. `composicao` é a visualização inteira: que redes e que subestações de cada."""
    composicao = composicao or ([{"rede": rede_id, "subestacoes": []}] if rede_id else [])
    with closing(_conn()) as cn, cn:
        if ordem is None:
            atual = cn.execute("SELECT MAX(ordem) FROM etapas WHERE projeto_id = ?", (projeto_id,)).fetchone()[0]
            ordem = 0 if atual is None else atual + 1
        registro = {
            "id": uuid.uuid4().hex[:12],
            "projeto_id": projeto_id,
            "ordem": ordem,
            "nome": nome,
            "descricao": descricao,
            # a primeira rede continua gravada à parte: é por ela que as telas antigas abrem a etapa
            "rede_id": rede_id or (composicao[0].get("rede") if composicao else ""),
            "camadas": json.dumps(camadas or []),
            "composicao": json.dumps(composicao, ensure_ascii=False),
            "criado_em": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        cn.execute(
            "INSERT INTO etapas (id, projeto_id, ordem, nome, descricao, rede_id, camadas,"
            " composicao, criado_em) VALUES (:id, :projeto_id, :ordem, :nome, :descricao,"
            " :rede_id, :camadas, :composicao, :criado_em)", registro)
    registro["camadas"] = json.loads(registro["camadas"])
    registro["composicao"] = json.loads(registro["composicao"])
    return registro


def remover_etapa(id_: str) -> str | None:
    """Devolve o projeto da etapa removida (para conferir quem pode apagar), ou None."""
    with closing(_conn()) as cn, cn:
        row = cn.execute("SELECT projeto_id FROM etapas WHERE id = ?", (id_,)).fetchone()
        if row is None:
            return None
        cn.execute("DELETE FROM etapas WHERE id = ?", (id_,))
    return row["projeto_id"]
