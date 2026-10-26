"""Exporta/importa lo configurado a mano: reglas, tarjetas y categorías/cuotas de movimientos.

Los movimientos se identifican por el Message-ID de su correo, así se pueden reaplicar en otra
instalación después de ingerir los mismos correos (backfill). Importar es idempotente: se puede
volver a correr tras un nuevo backfill o tras subir estados de cuenta.

Uso (desde la raíz del repo, con DB_PATH):
    python -m db.backup export > gg-config.json
    python -m db.backup import < gg-config.json
"""

import json
import sys
from contextlib import closing

import db


def export() -> dict:
    with closing(db._connect()) as conn:
        q = lambda sql: [dict(r) for r in conn.execute(sql)]
        return {
            "version": 1,
            "rules": q("SELECT pattern, category, subcategory FROM category_rules ORDER BY pattern"),
            "cards": q("SELECT card_type, label, is_credit FROM cards ORDER BY card_type"),
            # Solo lo editado a mano: categoría manual, cuotas > 1 o nota (lo de reglas se recalcula solo)
            "movements": q(
                "SELECT message_id, category, subcategory, installments, note, merchant, occurred_at, amount "
                "FROM movements WHERE message_id IS NOT NULL "
                "AND (category IS NOT NULL OR installments > 1 OR note IS NOT NULL) "
                "ORDER BY occurred_at"
            ),
        }


def import_(data: dict) -> dict:
    for r in data["rules"]:
        db.upsert_rule(r["pattern"], r["category"], r["subcategory"])
    for c in data["cards"]:
        db.upsert_card(c["card_type"], c["label"], bool(c["is_credit"]))
    applied = 0
    with closing(db._connect()) as conn, conn:
        for m in data["movements"]:
            cur = conn.execute(
                "UPDATE movements SET category = ?, subcategory = ?, installments = ?, note = ? WHERE message_id = ?",
                (m["category"], m["subcategory"], m["installments"], m.get("note"), m["message_id"]),
            )
            applied += cur.rowcount
    return {"rules": len(data["rules"]), "cards": len(data["cards"]),
            "movements_applied": applied, "movements_missing": len(data["movements"]) - applied}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export":
        json.dump(export(), sys.stdout, ensure_ascii=False, indent=1)
    elif cmd == "import":
        db.init_db()
        result = import_(json.load(sys.stdin))
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        if result["movements_missing"]:
            print("Faltan movimientos (aún no ingeridos): vuelve a importar después del backfill.", file=sys.stderr)
    else:
        sys.exit(__doc__)
