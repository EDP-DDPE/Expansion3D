"""Inspeciona a tabela de postes do GIS (Oracle) e grava estrutura + amostra em data/cache/gis/.

Credenciais nunca ficam no código. Antes de rodar, no seu terminal (PowerShell):
    $env:GIS_USER = "seu_usuario"
    $env:GIS_PASSWORD = "sua_senha"
    python tools\\inspect_gis_postes.py
"""
import os
import sys
from pathlib import Path

import oracledb

DSN = os.environ.get("GIS_DSN", "pbandeo1.world")
TABLE = os.environ.get("GIS_TABLE", "USU_GSA.POSTE")
TNS_DIR = os.environ.get("TNS_ADMIN", r"C:\Oracle\product\19.0.0\client_1\network\admin")
OUT = Path(__file__).resolve().parent.parent / "data" / "cache" / "gis" / "poste_inspecao.txt"

user, password = os.environ.get("GIS_USER"), os.environ.get("GIS_PASSWORD")
if not user or not password:
    sys.exit("Defina GIS_USER e GIS_PASSWORD no terminal antes de rodar.")

owner, name = TABLE.upper().split(".")
# modo thin: dispensa o Oracle Client (o instalado é 32-bit e o Python é 64-bit)
with oracledb.connect(user=user, password=password, dsn=DSN, config_dir=TNS_DIR) as cn:
    cur = cn.cursor()
    lines = [f"Tabela {TABLE} em {DSN}"]
    cur.execute(
        "SELECT column_name, data_type, data_length, data_precision, data_scale, nullable "
        "FROM all_tab_columns WHERE owner = :o AND table_name = :t ORDER BY column_id",
        o=owner, t=name,
    )
    cols = cur.fetchall()
    lines.append(f"\n{len(cols)} colunas:")
    lines += [f"  {c[0]:<32} {c[1]}({c[2]},{c[3]},{c[4]}) null={c[5]}" for c in cols]

    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    lines.append(f"\nTotal de linhas: {cur.fetchone()[0]}")

    cur.execute(
        "SELECT index_name, column_name FROM all_ind_columns WHERE table_owner = :o AND table_name = :t "
        "ORDER BY index_name, column_position", o=owner, t=name,
    )
    lines.append("\nÍndices: " + ", ".join(f"{i}({c})" for i, c in cur.fetchall()))

    geo = [c[0] for c in cols if c[1] == "SDO_GEOMETRY"]
    simple = [c[0] for c in cols if c[1] not in ("SDO_GEOMETRY", "BLOB", "CLOB", "LONG", "RAW")]
    extra = [f"t.{g}.sdo_srid AS {g}_SRID, t.{g}.sdo_point.x AS {g}_X, t.{g}.sdo_point.y AS {g}_Y" for g in geo]
    cur.execute(f"SELECT {', '.join(['t.' + c for c in simple] + extra)} FROM {TABLE} t WHERE ROWNUM <= 5")
    names = [d[0] for d in cur.description]
    lines.append("\nAmostra (5 linhas):")
    for row in cur.fetchall():
        lines.append("  " + " | ".join(f"{n}={v}" for n, v in zip(names, row)))

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
print(f"\nGravado em {OUT}")
