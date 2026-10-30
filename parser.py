"""Parsers de correos del Banco de Chile hacia datos de transacción (usados por imap/ingest.py).

Cada parser recibe el texto del correo y devuelve amount=0 si el correo no es un movimiento.
"""

import html
import re


def parse_banco_chile_email(body: str) -> dict:
    """
    Parsea correos del Banco de Chile para extraer información de transacciones.

    Soporta 3 tipos de transacciones:
    1. Compra con Tarjeta de Crédito
    2. Cargo en Cuenta (débito)
    3. Giro en Cajero

    Extrae:
    - amount: Monto de la transacción
    - card_type: Número de tarjeta/cuenta (****XXXX)
    - merchant: Comercio o "Cajero Automático"
    - datetime: Fecha y hora de la transacción
    - currency: "CLP" o "USD" (compras internacionales, ej. "US$47,98")

    Args:
        body: Cuerpo del correo

    Returns:
        Dict con los 4 campos parseados
    """
    result = {
        "amount": 0.0,
        "card_type": None,  # Ahora guarda el número de tarjeta
        "merchant": None,
        "datetime": None,
        "is_credit": False,
        "currency": "CLP",
    }

    # Normalizar whitespace: reemplazar múltiples espacios/saltos de línea por un espacio
    # Esto maneja emails con formato de plaintext que tienen saltos de línea
    # Los correos HTML traen entidades ("Cr&eacute;dito"); unescape antes de buscar.
    normalized_body = ' '.join(html.unescape(body).split())

    # 1. Extraer MONTO. CLP: "$122.000" (punto = miles). USD: "US$47,98" (coma = decimales).
    amount_match = re.search(r'(US)?\$([\d\.,]+)', normalized_body)
    if amount_match:
        digits = amount_match.group(2).replace('.', '')
        if amount_match.group(1):
            result["currency"] = "USD"
            digits = digits.replace(',', '.')
        else:
            digits = digits.replace(',', '')
        try:
            result["amount"] = float(digits)
        except ValueError:
            result["amount"] = 0.0

    # 2. Extraer NÚMERO DE TARJETA/CUENTA (ej: "****3670", "****7801")
    # Busca patrones como: "Tarjeta de Crédito ****3670" o "Cuenta ****7801"
    card_number_match = re.search(r'\*{4}(\d{4})', normalized_body)
    if card_number_match:
        # Guardar como ****XXXX
        last_digits = card_number_match.group(1)
        result["card_type"] = f"****{last_digits}"

    # 3. Extraer COMERCIO
    # Patrón: "****XXXX en COMERCIO el DD/MM/YYYY". Anclado al número de tarjeta, el
    # comercio puede traer cualquier carácter (ej. "POS_CA_TEC_", "QUEEN?S").
    merchant_match = re.search(r'\*{4}\d{4}\s+en\s+(.+?)\s+el\s+\d{2}/\d{2}/\d{4}', normalized_body) or re.search(
        r'\ben\s+([A-Za-z0-9\s\*\.\-/]+?)\s+el\s+\d{2}/',
        normalized_body
    )

    if merchant_match:
        merchant_raw = merchant_match.group(1).strip()
        # Limpiar espacios extra y normalizar
        result["merchant"] = ' '.join(merchant_raw.split())
    elif 'giro' in normalized_body.lower() or 'cajero' in normalized_body.lower():
        # Es un giro en cajero
        result["merchant"] = "Cajero Automático"

    # 4. Extraer FECHA Y HORA (formato: "el 05/02/2026 16:23")
    datetime_match = re.search(r'el\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})', normalized_body)
    if datetime_match:
        date_str = datetime_match.group(1)  # 05/02/2026
        time_str = datetime_match.group(2)  # 16:23
        result["datetime"] = f"{date_str} {time_str}"

    # 5. ¿Compra con tarjeta de crédito? (solo estas entran en la proyección de cuotas)
    result["is_credit"] = bool(re.search(r'tarjeta de cr[ée]dito', normalized_body, re.IGNORECASE))

    return result


_MESES = {m: i for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
     "agosto", "septiembre", "octubre", "noviembre", "diciembre"], 1)}


def parse_banco_chile_transferencias(body: str) -> dict:
    """
    Parsea correos de serviciodetransferencias@bancochile.cl.

    Solo genera movimiento para transferencias que hiciste tú:
    - "Transferencia a Terceros" (ej. a un amigo)
    - "Transferencias de Fondos a <X>" (Fintoc, Ticketplus, Kupo…)
    - "Aviso de transferencia de fondos" cuando dice "usted ha efectuado" (programadas)

    Devuelve amount=0 (se omite) para el resto:
    - Pagos de tarjeta / línea de crédito: no son gasto y duplicarían las compras.
    - "Comprobante de Pago": la misma compra ya llega por enviodigital@bancochile.cl.
    - Avisos de transferencias recibidas ("ha efectuado una transferencia de fondos a tu cuenta").
    """
    result = {"amount": 0.0, "card_type": None, "merchant": None, "datetime": None,
              "is_credit": False, "currency": "CLP", "memo": None}
    t = ' '.join(html.unescape(body).split())

    # Pago de tarjeta: no es gasto (duplicaría las compras) pero marca el estado como pagado
    if (payment := parse_card_payment(t)):
        return {**result, "card_payment": payment}

    if re.search(r'Transferencia a terceros', t, re.IGNORECASE):
        name = re.search(r'Destino Nombre y Apellido:? (.+?) (?:Rut|Tipo de Cuenta|Email|Monto)\b', t, re.IGNORECASE)
    else:
        name = re.search(r'usted ha efectuado una transferencia de fondos a (.+?)(?:, el d[ií]a| con el siguiente detalle)', t)
    amount = re.search(r'Monto:? \$ ?([\d.]+)', t)
    if not (name and amount):
        return result

    result["merchant"] = name.group(1).strip()
    result["amount"] = float(amount.group(1).replace('.', ''))

    # Detalle que escribiste al transferir: "Asunto" (programadas) o "Mensaje" (a terceros).
    # Las reglas también buscan aquí, ej. "ahorro vivienda" cuando el destinatario eres tú.
    memo = (re.search(r'Asunto:?\s*(.*?)\s*(?:Cuenta de origen|Datos de destinatario)', t)
            or re.search(r'Mensaje\s*(.*?)\s*(?:Fecha y Hora|Sigue estos|Si tienes)', t))
    result["memo"] = (memo.group(1).strip()[:120] or None) if memo else None

    # Cuenta de origen (aparece antes que la de destino). Se guarda como ****XXXX para que
    # coincida con la cuenta de los correos de enviodigital (ej. 00-123-04567-01 → ****6701).
    origin = re.search(r'(?:N[º°] de Cuenta|Cuenta Corriente)\s*:?\s*(\d[\d-]{6,})', t, re.IGNORECASE)
    if origin:
        digits = re.sub(r'\D', '', origin.group(1))
        result["card_type"] = f"****{digits[-4:]}"

    # "Fecha y Hora: jueves 22 de octubre de 2026 17:59"; si no hay hora, "Fecha: 23/10/2026".
    dt = re.search(r'(\d{1,2}) de ([a-záé]+) de (\d{4}) (\d{2}:\d{2})', t, re.IGNORECASE)
    if dt and dt.group(2).lower() in _MESES:
        result["datetime"] = f"{int(dt.group(1)):02d}/{_MESES[dt.group(2).lower()]:02d}/{dt.group(3)} {dt.group(4)}"
    elif (d := re.search(r'Fecha:? (\d{2}/\d{2}/\d{4})', t)):
        # ponytail: estos correos no traen hora; 00:00 basta para agrupar por mes
        result["datetime"] = f"{d.group(1)} 00:00"
    return result


def parse_card_payment(t: str) -> dict | None:
    """Comprobante de pago de tarjeta de crédito (nacional en $, internacional en USD$)."""
    if not re.search(r'efectuado el pago de la tarjeta de cr[ée]dito', t, re.IGNORECASE):
        return None
    card = re.search(r'\*{4,}(\d{4})', t)
    usd = re.search(r'Monto pagado USD\$([\d.,]+)', t)
    clp = re.search(r'Monto \$([\d.]+)', t)
    dt = re.search(r'(\d{1,2}) de ([a-záé]+) de (\d{4}) (\d{2}:\d{2})', t, re.IGNORECASE)
    if not (card and (usd or clp) and dt and dt.group(2).lower() in _MESES):
        return None
    return {
        "card_type": f"****{card.group(1)}",
        "currency": "USD" if usd else "CLP",
        "amount": float(usd.group(1).replace('.', '').replace(',', '.')) if usd else float(clp.group(1).replace('.', '')),
        "datetime": f"{int(dt.group(1)):02d}/{_MESES[dt.group(2).lower()]:02d}/{dt.group(3)} {dt.group(4)}",
    }


PARSERS = {
    "banco_chile": parse_banco_chile_email,
    "banco_chile_transferencias": parse_banco_chile_transferencias,
}
