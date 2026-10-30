"""Casos reales (anonimizados) de correos del Banco de Chile. Uso: uv run python -m pytest test_parser.py"""

from parser import parse_banco_chile_email as parse


def test_usd_credit_html_entities():
    r = parse("<p>Te informamos que se ha realizado una compra por US$47,98 con Tarjeta de "
              "Cr&eacute;dito ****3670 en Uniqlo Canada POS_CA_TEC_ el 29/07/2026 11:09. Revisa Saldos</p>")
    assert (r["amount"], r["currency"], r["is_credit"]) == (47.98, "USD", True)
    assert r["merchant"] == "Uniqlo Canada POS_CA_TEC_"


def test_clp_credit_merchant_with_odd_chars():
    r = parse("compra por $1.215.000 con Tarjeta de Cr&eacute;dito ****3670 en QUEEN?S CROSS el 29/07/2026 11:42.")
    assert (r["amount"], r["currency"], r["is_credit"], r["merchant"]) == (1215000, "CLP", True, "QUEEN?S CROSS")


def test_debit_and_atm():
    r = parse("compra por $6.800 con cargo a Cuenta ****7801 en VENTAS EN RUTA el 29/05/2026 12:26. Revisa Saldos")
    assert (r["amount"], r["is_credit"], r["merchant"], r["datetime"]) == (6800, False, "VENTAS EN RUTA", "29/05/2026 12:26")
    r = parse("giro en Cajero por $195.183 con cargo a Cuenta ****7801 el 27/07/2026 08:59.")
    assert (r["amount"], r["merchant"], r["card_type"]) == (195183, "Cajero Automático", "****7801")


from parser import parse_banco_chile_transferencias as parse_tef


def test_transferencia_a_terceros():
    r = parse_tef("Te informamos que has realizado una Transferencia a terceros en forma exitosa con el siguiente detalle: "
                  "Origen Tipo de Cuenta Cuenta Corriente Nº de Cuenta 00-123-04567-01 Destino Nombre y Apellido Juan Perez "
                  "Rut 11111111-1 Tipo de Cuenta Cuenta Vista Nº de Cuenta 00-002-09999-69 Banco Banco Estado Email "
                  "Monto $150.000 Mensaje Fecha y Hora: jueves 22 de octubre de 2026 17:59 Transacción TEF123")
    assert (r["amount"], r["merchant"], r["card_type"], r["datetime"]) == (150000, "Juan Perez", "****6701", "22/10/2026 17:59")
    assert r["memo"] is None  # "Mensaje" vacío: no se traga el "Fecha y Hora" que sigue


def test_transferencia_memo():
    r = parse_tef("Le informamos que usted ha efectuado una transferencia de fondos a Juan Perez con el siguiente detalle. "
                  "Transferencia realizada Datos de cuenta Fecha: 22/10/2026 Asunto: ahorro vivienda Cuenta de origen: "
                  "Cuenta Corriente 00000000001234567801 Datos de destinatario Nombre y Apellido: Juan Perez Monto: $10.000")
    assert (r["merchant"], r["memo"], r["amount"], r["card_type"]) == ("Juan Perez", "ahorro vivienda", 10000, "****7801")
    r = parse_tef("Transferencia a terceros ... Nº de Cuenta 00-123-04567-01 Destino Nombre y Apellido Ana Rut 1-9 "
                  "Monto $4.000 Mensaje PAGO arriendo Fecha y Hora: lunes 05 de octubre de 2026 21:58")
    assert r["memo"] == "PAGO arriendo"


def test_transferencia_a_empresa_sin_rut():
    r = parse_tef("Transferencia a terceros ... Origen Tipo de Cuenta Cuenta Corriente Nº de Cuenta 00-123-04567-01 Destino "
                  "Nombre y Apellido Fundacion Educacional X Monto $10.000 Mensaje Fecha y Hora: viernes 04 de septiembre de 2026 15:56")
    assert (r["merchant"], r["amount"]) == ("Fundacion Educacional X", 10000)


def test_transferencia_a_medio_de_pago():
    r = parse_tef("Le informamos que usted ha efectuado una transferencia de fondos a Medio De Pago Fintoc, el día 23 de octubre "
                  "de 2026, desde su Cuenta Corriente 1234567801. Datos del Destinatario Nombre Medio De Pago Fintoc Rut 7-5 "
                  "Cuenta 225663706148 Datos de la Transferencia Fecha 23/10/2026 Cuenta 1234567801 Monto $21.431 ID TEF_X")
    assert (r["amount"], r["merchant"], r["card_type"], r["datetime"]) == (21431, "Medio De Pago Fintoc", "****7801", "23/10/2026 00:00")


def test_omite_pagos_comprobantes_y_recibidas():
    for body in [
        # pago de tarjeta: duplicaría las compras ya registradas
        "se ha efectuado el pago de la tarjeta de crédito nacional en forma exitosa ... Monto $430.872",
        # comprobante de compra: ya llega por enviodigital
        "Comprobante de pago exitoso ... Comercio BUSCALIBRE COM Monto $ 44.170 Medio de pago Tarjeta de Débito ****0202",
        # transferencia recibida
        "Te informamos que nuestro(a) cliente Ana ha efectuado una transferencia de fondos a tu cuenta ... Monto $5.000",
    ]:
        assert parse_tef(body)["amount"] == 0


def test_pago_de_tarjeta():
    nac = ("Te informamos que se ha efectuado el pago de la tarjeta de cr&eacute;dito nacional en forma exitosa "
           "con el siguiente detalle: N&deg; de cuenta 00-123-04567-01 Destino Tipo de tarjeta Tarjeta de Cr&eacute;dito "
           "N&deg; de tarjeta ************3670 Utilizado $234.621 Monto $430.872 "
           "Fecha y Hora: viernes 23 de octubre de 2026 11:34 Transacci&oacute;n: X")
    r = parse_tef(nac)
    assert r["amount"] == 0  # no es gasto
    assert r["card_payment"] == {"card_type": "****3670", "currency": "CLP", "amount": 430872, "datetime": "23/10/2026 11:34"}
    intl = ("se ha efectuado el pago de la tarjeta de cr&eacute;dito internacional en forma exitosa ... "
            "N&ordm; de tarjeta ************3670 Utilizado USD$0,00 Detalle Monto pagado USD$23,82 Tipo de cambio $999 "
            "Monto $23.798 Fecha y Hora: viernes 23 de octubre de 2026 11:34")
    assert parse_tef(intl)["card_payment"] == {"card_type": "****3670", "currency": "USD", "amount": 23.82, "datetime": "23/10/2026 11:34"}
