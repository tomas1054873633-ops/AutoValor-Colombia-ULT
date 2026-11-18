"""Completa el motor de las versiones publicadas sin cilindrada (ver engine_audit.py).

Uso:
    python scripts/auditar_motores.py            # simulación: muestra qué cambiaría
    python scripts/auditar_motores.py --aplicar  # aplica (con copia de seguridad previa)
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import DEFAULT_DB, connect_db, init_db  # noqa: E402
from engine_audit import run_engine_audit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--aplicar", action="store_true")
    args = ap.parse_args()
    db = Path(args.db)
    init_db(db, seed=False, auto_audit=False)
    if args.aplicar:
        backups = db.parent / "backups"
        backups.mkdir(exist_ok=True)
        shutil.copy2(db, backups / f"autovalor_pre_auditoria_motores_{datetime.now():%Y%m%d_%H%M%S}.db")
    conn = connect_db(db)
    try:
        res = run_engine_audit(conn, apply=args.aplicar)
        by_rule = collections.Counter(t["rule"].split(":")[0] if ":" in t["rule"] else t["rule"] for _, t in res["changes"])
        print(json.dumps({
            "aplicado": args.aplicar,
            "versiones_cambiadas": res["changed"],
            "versiones_fusionadas": res["merged"],
            "por_regla": dict(by_rule),
            "conflictos_entre_fichas": len(res["conflicts"]),
            "colisiones": len(res["collisions"]),
            "versiones_aun_sin_motor": len(res["missing"]),
            "fichas_sin_uso": res["unused_specs"],
        }, ensure_ascii=False, indent=1))
        for c in res["conflicts"][:20]:
            print("CONFLICTO", c)
        for c in res["collisions"][:20]:
            print("COLISION", c)
        for r, tgt, keep in res["merges"][:40]:
            print("FUSION", r["id"], "->", keep, r["model_year"], r["raw_description"], "|", tgt["engine_label"])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
