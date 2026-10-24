"""Parser de estados de cuenta y conciliación. Uso: uv run python -m pytest test_statement.py"""

import statement

# Extracto (anonimizado) de `pdftotext -layout` de un estado de cuenta real
SAMPLE = """\
                                                    ESTADO DE CUENTA NACIONAL DE TARJETA DE CRÉDITO
 NOMBRE DEL TITULAR                    JUAN PEREZ
 N° DE TARJETA DE CRÉDITO              XXXX XXX9 9999 3670
 FECHA ESTADO DE CUENTA                22/09/2026
 CAE se calcula sobre un supuesto de gasto mensual de UF 20 y pagadero en 12 cuotas.                            PERÍODO FACTURADO             22/08/2026 22/09/2026
 inscritas en los Registros de Emisores de Tarjetas que lleva la CMF, en www.cmfchile.cl.                       PAGAR HASTA                         06/10/2026
 MONTO FACTURADO A PAGAR (PERÍODO ANTERIOR)                     A     $          447.834
                 31/08/26    010900000000 MONTO CANCELADO                                            $       -447.834 $          -447.834   01/01 $          -447.834
SANTIAGO         28/08/26    310810419428 PAGO ONLINE KUSHKI SANTIAGO               $                           9.765 $            9.765    01/01   $             9.765
SANTIAGO     21/09/26   220953144686 SMART FIT GIMNASIOS SANTIAGO                  $       29.900 $        29.900 01/01 $        29.900
SANTIAGO     02/07/26   220910304593 LATAM.COM XP OTRAS TASA INT. 0,00%            $      477.188 $       477.188 03/03 $       159.064
                                                3.CARGOS, COMISIONES, IMPUESTOS Y ABONOS
             22/09/26   220900000000 COMISION ADMINISTRACION MENSUAL               $        4.919 $         4.919 01/01 $         4.919
 MONTO TOTAL FACTURADO A PAGAR ( A + B + C + D + E + F +G ) $                           203.648            EVOLUCIÓN MONTOS FACTURADOS Y PAGADOS
PRÓXIMO PERÍODO DE FACTURACIÓN                              23/09/2026         22/10/2026
                                                          ESTADO DE CUENTA INTERNACIONAL DE TARJETA DE CREDITO
          FECHA ESTADO DE CUENTA                  22/09/2026
          SALDO ANTERIOR FACTURADO                          US$                573,28                            PERIODO FACTURADO DESDE                                22/08/2026
          ABONO REALIZADO                                   US$                573,28                            PERIODO FACTURADO HASTA                                22/09/2026
          DEUDA TOTAL                                       US$                 13,39
         2508 00000000                         25/08/26    Pago Dolar TEF                                                             CL               -573,28               -573,28
         1509 74007036258920006097034          14/09/26    www.aliexpress.com                        WWW.ALIEXPRE                     GB             12.613,00                 13,39
"""


def test_parse_statement():
    nat, intl = statement.parse_statement(SAMPLE)
    assert (nat["card_type"], nat["statement_date"], nat["period_to"], nat["due_date"], nat["next_period_to"]) == \
        ("****3670", "2026-09-22", "2026-09-22", "2026-10-06", "2026-10-22")
    assert [l["kind"] for l in nat["lines"]] == ["pago", "compra", "compra", "cuotas", "cargo"]
    latam = nat["lines"][3]
    assert (latam["description"], latam["amount"], latam["installment_n"], latam["installments"],
            latam["installment_amount"]) == ("LATAM.COM XP OTRAS", 477188, 3, 3, 159064)
    assert nat["check"]["ok"], nat["check"]  # 447.834 - 447.834 + 9.765 + 29.900 + 159.064 + 4.919
    ali = intl["lines"][1]
    assert (intl["currency"], ali["amount"], ali["origin_amount"], intl["check"]["ok"]) == ("USD", 13.39, 12613, True)


def test_reconcile():
    nat, intl = statement.parse_statement(SAMPLE)
    movements = [
        {"id": 1, "amount": 9765, "currency": "CLP", "occurred_at": "2026-08-28 19:23"},
        {"id": 2, "amount": 29900, "currency": "CLP", "occurred_at": "2026-09-16 03:58"},  # 5 días antes
        {"id": 3, "amount": 477188, "currency": "CLP", "occurred_at": "2026-07-02 10:00"},
        {"id": 4, "amount": 12613, "currency": "CLP", "occurred_at": "2026-09-14 12:00"},  # cobrado en CLP, facturado en US$
        {"id": 5, "amount": 100, "currency": "CLP", "occurred_at": "2026-09-02 17:17"},    # validación anulada
        {"id": 6, "amount": 9765, "currency": "CLP", "occurred_at": "2026-09-20 10:00"},   # mismo monto, lejos: no calza
    ]
    r = statement.reconcile([nat, intl], movements)
    matched = {l["description"]: (l["movement"] or {}).get("id") for s in r["sections"] for l in s["lines"]}
    assert matched == {"MONTO CANCELADO": None, "PAGO ONLINE KUSHKI SANTIAGO": 1, "SMART FIT GIMNASIOS SANTIAGO": 2,
                       "LATAM.COM XP OTRAS": 3, "COMISION ADMINISTRACION MENSUAL": None,
                       "Pago Dolar TEF": None, "www.aliexpress.com": 4}
    assert [m["id"] for m in r["ledger_only"]] == [5, 6]


def test_save_statement_replaces(tmp_path, monkeypatch):
    import db

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    nat, intl = statement.parse_statement(SAMPLE)
    for _ in range(2):  # volver a subir el mismo PDF no duplica
        db.save_statement(nat), db.save_statement(intl)
    group = db.statement_group("****3670", "2026-09-22")
    assert [(s["currency"], len(s["lines"])) for s in group] == [("CLP", 5), ("USD", 2)]
    assert statement.check_totals(group[0])["ok"]


def test_project_by_due():
    from datetime import date

    sched = statement.BillingSchedule([{"statement_date": "2026-09-22", "due_date": "2026-10-06",
                                        "next_period_to": "2026-10-22"}])
    assert sched.next_cut(date(2026, 10, 1)) == (date(2026, 10, 22), date(2026, 11, 5))
    assert sched.next_payment(date(2026, 10, 1)) == (date(2026, 9, 22), date(2026, 10, 6))
    assert sched.next_payment(date(2026, 10, 23)) == (date(2026, 10, 22), date(2026, 11, 5))
    assert date(2026, 11, 20) in sched.cuts  # 22/11/2026 es domingo → viernes 20
    proj = statement.project_by_due([
        (10_000, "2026-09-21", 1),   # corte 22/09 → vence 06/10
        (20_000, "2026-09-23", 1),   # corte 22/10 → vence 05/11
        (30_000, "2026-10-25", 3),   # cortes 20/11, 22/12, 22/01 → vencen dic, ene, feb
        (99_000, "2026-08-01", 1),   # ya vencido: no se proyecta
    ], sched, date(2026, 10, 1))
    assert proj == {"2026-10": 10_000, "2026-11": 20_000, "2026-12": 10_000, "2027-01": 10_000, "2027-02": 10_000}
