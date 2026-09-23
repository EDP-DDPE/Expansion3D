"""Ponto de entrada do serviço (NSSM). Lê host e porta do conf.ini."""
from __future__ import annotations

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.server:app",
        host=settings.host,
        port=settings.porta,
        workers=1,          # processo único: o cache em memória é compartilhado
        timeout_keep_alive=120,
        log_level="info",
    )
