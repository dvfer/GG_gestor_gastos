"""Chequeo del ledger SQLite y la proyección de cuotas.

Uso: python -m pytest db/test_db.py (desde la raíz del repo).
"""

import sqlite3
from datetime import date

import pytest

import db


def test_project_installments():
    purchases = [
        (300_000, "2026-09", 3),   # cuotas en oct, nov, dic
        (50_000, "2026-10", 1),    # una cuota en nov
        (120_000, "2026-11", 12),  # dic 2026 .. nov 2027
    ]
    proj = db.project_installments(purchases, date(2026, 11, 5))
    assert "2026-10" not in proj  # meses pasados no se proyectan
    assert proj["2026-11"] == 100_000 + 50_000
    assert proj["2026-12"] == 100_000 + 10_000
    assert proj["2027-11"] == 10_000
    assert list(proj) == sorted(proj)


def test_ledger_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    mid = db.insert_movement(30_000, "****3670", "UBER *TRIP", "05/02/2026 16:23", "s", "b", is_credit=True)

    db.insert_movement(5_000, "****7801", "JUMBO", None, "s", "b")  # sin fecha → mes de created_at
    db.insert_movement(40.5, "****3670", "AMAZON", None, "s", "b", is_credit=True, currency="USD")  # no suma en CLP
    db.upsert_rule("uber", "Transporte")
    cats = {r["category"]: r["total"] for r in db.monthly_by_category(36)}
    assert cats == {"Transporte": 30_000, "Sin categoría": 5_000}
    assert {r["category"]: r["is_credit"] for r in db.monthly_by_category(36)} == {"Transporte": 1, "Sin categoría": 0}
    m = db.list_movements(10, 0)[2]
    assert (m["category"], m["occurred_at"], m["installments"]) == ("Transporte", "2026-02-05 16:23", 1)

    assert db.update_movement(mid, category="Trabajo", installments=3)
    m = db.list_movements(10, 0)[2]
    assert (m["category"], m["installments"]) == ("Trabajo", 3)

    db.update_movement(mid, category="")  # vacío = volver a la regla
    assert db.list_movements(10, 0)[2]["category"] == "Transporte"
    assert not db.update_movement(999, installments=2)

    # Subcategorías: la regla trae el par; una categoría manual no hereda la subcategoría de la regla
    db.upsert_rule("uber", "Transporte", "App")
    uber = lambda: next(m for m in db.list_movements(10, 0) if m["merchant"] == "UBER *TRIP")
    assert (uber()["category"], uber()["subcategory"]) == ("Transporte", "App")
    db.update_movement(mid, category="Ocio")
    assert (uber()["category"], uber()["subcategory"]) == ("Ocio", None)
    db.update_movement(mid, category="Ocio", subcategory="Tocata")
    rows = {(r["category"], r["subcategory"]): r["total"] for r in db.monthly_by_category(36)}
    assert rows == {("Ocio", "Tocata"): 30_000, ("Sin categoría", None): 5_000}
    db.update_movement(mid, category="", subcategory="X")  # vuelve a la regla, se descarta X
    assert (uber()["category"], uber()["subcategory"]) == ("Transporte", "App")

    # Editar regla: cambiar patrón (renombra) y no pisar otra regla existente
    db.upsert_rule("JUMBO", "Super")
    db.upsert_rule("TRIP", "Viajes", None, old_pattern="uber")
    assert {r["pattern"]: r["category"] for r in db.list_rules()} == {"JUMBO": "Super", "TRIP": "Viajes"}
    with pytest.raises(ValueError):
        db.upsert_rule("jumbo", "X", None, old_pattern="TRIP")
    db.upsert_rule("trip", "Viajes", "Uber", old_pattern="TRIP")  # mismo patrón con otra capitalización
    assert [r["subcategory"] for r in db.list_rules() if r["category"] == "Viajes"] == ["Uber"]
    db.delete_rule("TRIP"); db.delete_rule("JUMBO"); db.upsert_rule("uber", "Transporte", "App")

    # La config de la tarjeta manda sobre lo detectado por el parser
    assert {c["card_type"]: (c["is_credit"], c["configured"]) for c in db.list_cards()}["****7801"] == (0, 0)
    db.upsert_card("****7801", "Cuenta corriente", True)
    jumbo = next(m for m in db.list_movements(10, 0) if m["merchant"] == "JUMBO")
    assert (jumbo["is_credit"], jumbo["card_label"]) == (1, "Cuenta corriente")
    db.upsert_card("****3670", None, False)
    uber = next(m for m in db.list_movements(10, 0) if m["merchant"] == "UBER *TRIP")
    assert uber["is_credit"] == 0

    # Mismo correo dos veces (backfill + poller) → un solo movimiento
    assert db.insert_movement(1_000, None, "X", None, "s", "b", message_id="<a@bank>") is not None
    assert db.insert_movement(1_000, None, "X", None, "s", "b", message_id="<a@bank>") is None
    assert db.insert_movement(1_000, None, "X", None, "s", "b") is not None  # sin message_id no se deduplica

    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        with db._connect() as conn:
            conn.execute("UPDATE movements SET amount = 1")


def test_installment_projection_uses_statements(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    db.insert_movement(30_000, "****3670", "LATAM", "23/09/2026 10:00", "s", "b", is_credit=True, installments=3)
    db.insert_movement(5_000, "****9999", "OTRA", "23/09/2026 10:00", "s", "b", is_credit=True)
    today = date(2026, 10, 1)
    # Sin estado de cuenta: regla por mes de compra (cuotas oct, nov, dic)
    assert db.installment_projection(today)[0] == {"2026-10": 15_000, "2026-11": 10_000, "2026-12": 10_000}
    db.save_statement({"card_type": "****3670", "currency": "CLP", "statement_date": "2026-09-22",
                       "period_from": "2026-08-22", "period_to": "2026-09-22", "due_date": "2026-10-06",
                       "next_period_to": "2026-10-22", "total": 0, "previous_total": 0, "lines": []})
    # Con estado: compra del 23/09 entra al corte 22/10 → vence 05/11; luego dic y ene
    proj, basis = db.installment_projection(today)
    assert proj == {"2026-10": 5_000, "2026-11": 10_000, "2026-12": 10_000, "2027-01": 10_000}
    assert basis[0] == {"card_type": "****3670", "next_cut": "2026-10-22",
                        "payment_cut": "2026-09-22", "payment_due": "2026-10-06"}
    # 23/10: el corte del 22/10 ya pasó pero vence el 05/11 → ese es el próximo pago
    basis = db.installment_projection(date(2026, 10, 23))[1]
    assert (basis[0]["next_cut"], basis[0]["payment_cut"], basis[0]["payment_due"]) == \
        ("2026-11-20", "2026-10-22", "2026-11-05")
    # Con el estado del 22/10 cargado manda lo facturado: su total (no los correos previos al corte)
    # y sus cuotas restantes (2/3 → 1 más; 00/2 → 2 más, con interés); el ledger aporta solo lo posterior
    line = {"op_date": "2026-10-07", "reference": None, "description": "X", "amount": 0, "origin_amount": None}
    db.save_statement({"card_type": "****3670", "currency": "CLP", "statement_date": "2026-10-22",
                       "period_from": "2026-09-23", "period_to": "2026-10-22", "due_date": "2026-11-05",
                       "next_period_to": "2026-11-20", "total": 400_000, "previous_total": 0, "lines": [
                           {**line, "kind": "cuotas", "installment_n": 2, "installments": 3, "installment_amount": 10_000},
                           {**line, "kind": "cargo", "installment_n": 0, "installments": 2, "installment_amount": 31_537}]})
    db.insert_movement(9_000, "****3670", "NUEVA", "25/10/2026 10:00", "s", "b", is_credit=True)
    assert db.installment_projection(date(2026, 10, 30))[0] == \
        {"2026-10": 5_000, "2026-11": 400_000, "2026-12": 41_537 + 9_000, "2027-01": 31_537}  # 10: la otra tarjeta


def test_backup_roundtrip(tmp_path, monkeypatch):
    from db import backup

    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    db.init_db()
    mid = db.insert_movement(9_000, "****3670", "BAR", "23/10/2026 22:00", "s", "b", message_id="<m1@x>")
    db.insert_movement(1_000, "****3670", "SIN MSGID", "23/10/2026 22:00", "s", "b")
    db.update_movement(mid, category="Ocio", subcategory="Tocata", installments=3, note="regalo")
    db.upsert_rule("UBER", "Transporte", "App")
    db.upsert_card("****3670", "CRED", True)
    data = backup.export()
    assert [m["message_id"] for m in data["movements"]] == ["<m1@x>"]

    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))  # instalación nueva, mismo correo ingerido
    db.init_db()
    db.insert_movement(9_000, "****3670", "BAR", "23/10/2026 22:00", "s", "b", message_id="<m1@x>")
    for _ in range(2):  # idempotente
        result = backup.import_(data)
    assert result == {"rules": 1, "cards": 1, "movements_applied": 1, "movements_missing": 0}
    m = db.list_movements(10, 0)[0]
    assert (m["category"], m["subcategory"], m["installments"], m["card_label"], m["note"]) == \
        ("Ocio", "Tocata", 3, "CRED", "regalo")
    db.update_movement(m["id"], note="  ")  # vacío borra la nota
    assert db.list_movements(10, 0)[0]["note"] is None


def test_rule_matches_memo(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    db.insert_movement(10_000, "****7801", "Juan Perez", "22/10/2026 00:00", "s", "b", memo="ahorro vivienda")
    db.insert_movement(500_000, "****7801", "Juan Perez", "01/10/2026 11:28", "s", "b")
    db.upsert_rule("ahorro vivienda", "Ahorro/Inversión", "Ahorro")
    cats = {m["amount"]: (m["category"], m["memo"]) for m in db.list_movements(10, 0)}
    assert cats == {10_000: ("Ahorro/Inversión", "ahorro vivienda"), 500_000: ("Sin categoría", None)}
