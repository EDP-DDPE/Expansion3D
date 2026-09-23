"""Login único com o Atlas (GCIWeb).

O Atlas e o Expansion rodam no mesmo servidor em portas diferentes, mas o cookie de sessão do
Atlas é marcado como Secure e não é compartilhável entre aplicações. Em vez de espiar a sessão
do Flask, a integração usa um salto curto:

    Expansion  ->  GET {atlas}/api/expansion/ticket?retorno=...
    Atlas      ->  redireciona de volta com ?ticket=<token assinado> (ou ticket=nenhum)
    Expansion  ->  confere a assinatura e grava o próprio cookie de sessão

O token é assinado com HMAC-SHA256 usando a frase secreta compartilhada (conf.ini [atlas]
segredo == EXPANSION_SECRET no .env do Atlas). Só o servidor do Atlas conhece o usuário logado,
e nenhuma senha trafega. Sem ticket válido o usuário fica em modo Visualização.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from .config import settings

COOKIE = "expansion_sessao"


@dataclass(frozen=True)
class Usuario:
    matricula: str
    nome: str
    email: str = ""
    admin: bool = False

    def como_dict(self) -> dict:
        return {"matricula": self.matricula, "nome": self.nome, "email": self.email, "admin": self.admin}


class TicketInvalido(Exception):
    """Token ausente, adulterado ou fora do prazo."""


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode().rstrip("=")


def _deb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def assinar(payload: dict, segredo: str) -> str:
    """Token compacto `corpo.assinatura`, sem dependência externa (o Atlas usa o mesmo formato)."""
    corpo = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(segredo.encode(), corpo.encode(), hashlib.sha256).digest()
    return f"{corpo}.{_b64(mac)}"


def conferir(token: str, segredo: str, validade_s: int) -> dict:
    """Devolve o conteúdo do token; levanta TicketInvalido se a assinatura ou o prazo não baterem."""
    if not token or "." not in token:
        raise TicketInvalido("Token ausente ou malformado.")
    corpo, assinatura = token.rsplit(".", 1)
    esperado = hmac.new(segredo.encode(), corpo.encode(), hashlib.sha256).digest()
    # compare_digest evita vazar, pelo tempo de resposta, o quanto da assinatura estava certo
    if not hmac.compare_digest(_deb64(assinatura), esperado):
        raise TicketInvalido("Assinatura não confere.")
    try:
        payload = json.loads(_deb64(corpo))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TicketInvalido("Conteúdo ilegível.") from exc
    emitido = payload.get("ts", 0)
    if not isinstance(emitido, (int, float)) or time.time() - emitido > validade_s:
        raise TicketInvalido("Token fora do prazo. Entre de novo pelo Atlas.")
    if time.time() - emitido < -60:  # relógio do Atlas muito à frente
        raise TicketInvalido("Token emitido no futuro; confira o relógio dos servidores.")
    return payload


def usuario_do_ticket(ticket: str) -> Usuario:
    """Valida um ticket vindo do Atlas e devolve quem é o usuário."""
    payload = conferir(ticket, settings.atlas_segredo, settings.ticket_validade_s)
    matricula = str(payload.get("matricula") or "").strip()
    if not matricula:
        raise TicketInvalido("O Atlas não informou a matrícula.")
    return Usuario(
        matricula=matricula,
        nome=str(payload.get("nome") or matricula).strip(),
        email=str(payload.get("email") or "").strip(),
        admin=bool(payload.get("admin")),
    )


def criar_cookie(usuario: Usuario) -> str:
    return assinar({**usuario.como_dict(), "ts": int(time.time())}, settings.atlas_segredo)


def usuario_do_cookie(cookie: str | None) -> Usuario | None:
    """Usuário da sessão do Expansion, ou None quando não há sessão válida (modo Visualização)."""
    if not cookie:
        return None
    try:
        payload = conferir(cookie, settings.atlas_segredo, settings.sessao_validade_h * 3600)
    except TicketInvalido:
        return None
    matricula = str(payload.get("matricula") or "").strip()
    if not matricula:
        return None
    return Usuario(matricula=matricula, nome=str(payload.get("nome") or matricula),
                   email=str(payload.get("email") or ""), admin=bool(payload.get("admin")))
