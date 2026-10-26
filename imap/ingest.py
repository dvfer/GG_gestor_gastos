"""Ingesta de correos bancarios vía IMAP (Gmail) hacia el ledger.

Revisa periódicamente los correos no leídos de una casilla Gmail (usando una
contraseña de aplicación), los parsea según el remitente (imap/config.json)
y los guarda en la base de datos. Cada correo procesado se marca \\Seen para
no volver a leerlo en el próximo ciclo.
"""

import asyncio
import email
import imaplib
import json
import logging
import os
import re
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr

import db
from parser import PARSERS

logger = logging.getLogger("imap.ingest")

IMAP_HOST = "imap.gmail.com"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
DEFAULT_PARSER = "banco_chile"


def is_enabled() -> bool:
    return bool(os.environ.get("IMAP_USER")) and bool(os.environ.get("IMAP_APP_PASSWORD"))


def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    conn.login(os.environ["IMAP_USER"], os.environ["IMAP_APP_PASSWORD"])
    conn.select(os.environ.get("IMAP_FOLDER", "INBOX"))
    return conn


def _load_senders() -> list[dict]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        logger.warning("No se pudo leer %s, se ingerirán todos los correos no leídos", CONFIG_PATH)
        return []
    return data.get("senders", [])


def _search_criteria(senders: list[dict], base: str = "UNSEEN") -> str:
    addresses = [s["address"] for s in senders if s.get("address")]
    if not addresses:
        return f"({base})"
    if len(addresses) == 1:
        return f'({base} FROM "{addresses[0]}")'
    criteria = f'FROM "{addresses[-1]}"'
    for address in reversed(addresses[:-1]):
        criteria = f'OR FROM "{address}" {criteria}'
    return f"({base} {criteria})"


def _parser_for_sender(senders: list[dict], from_header: str):
    _, from_address = parseaddr(from_header)
    for s in senders:
        if s.get("address", "").lower() == from_address.lower():
            parser_id = s.get("parser", DEFAULT_PARSER)
            parser = PARSERS.get(parser_id)
            if parser is not None:
                return parser
            logger.warning("Parser desconocido %r para %s, se usa %s", parser_id, from_address, DEFAULT_PARSER)
            break
    return PARSERS[DEFAULT_PARSER]


def _decode_subject(raw_subject: str) -> str:
    parts = decode_header(raw_subject or "")
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Texto para los parsers: quita <style>/<script>/<head> completos y luego las etiquetas."""
    html = re.sub(r"(?is)<(style|script|head)\b.*?</\1\s*>", " ", html)
    # ponytail: naive tag strip, swap for html.parser/BeautifulSoup if a bank email breaks it
    return re.sub(r"<[^>]+>", " ", html)


def _extract_body(msg: Message) -> str:
    if not msg.is_multipart():
        body = _decode_part(msg)
        if msg.get_content_type() == "text/html":
            return _strip_html(body)
        return body

    plain = None
    html = None
    for part in msg.walk():
        if "attachment" in str(part.get("Content-Disposition") or ""):
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif part.get_content_type() == "text/html" and html is None:
            html = _decode_part(part)

    if plain:
        return plain
    if html:
        return _strip_html(html)
    return ""


def _process_message(conn: imaplib.IMAP4_SSL, msg_num: bytes, senders: list[dict] | None = None) -> None:
    typ, msg_data = conn.fetch(msg_num, "(RFC822)")
    if typ != "OK" or not msg_data or msg_data[0] is None:
        logger.warning("No se pudo leer el correo %s, se reintentará en el próximo ciclo", msg_num)
        return

    raw = msg_data[0][1]
    msg = email.message_from_bytes(raw)
    subject = _decode_subject(msg.get("Subject", ""))
    body = _extract_body(msg)

    parser = _parser_for_sender(senders if senders is not None else _load_senders(), msg.get("From", ""))
    parsed = parser(body)
    if parsed["amount"] == 0:
        logger.info("Correo sin datos de transacción, se omite: %r", subject)
        conn.store(msg_num, "+FLAGS", "\\Seen")
        return

    try:
        movement_id = db.insert_movement(
            amount=parsed["amount"],
            card_type=parsed["card_type"],
            merchant=parsed["merchant"],
            datetime_str=parsed["datetime"],
            raw_subject=subject,
            raw_body=body,
            is_credit=parsed.get("is_credit", False),
            message_id=(msg.get("Message-ID") or "").strip() or None,
            currency=parsed.get("currency", "CLP"),
            memo=parsed.get("memo"),
        )
    except Exception:
        logger.exception("Error guardando movimiento desde correo %r, se reintentará", subject)
        return

    if movement_id is None:
        logger.info("Correo ya estaba en el ledger, se omite: %r", subject)
    else:
        logger.info("Movimiento guardado desde correo (id=%s): %r", movement_id, subject)
    conn.store(msg_num, "+FLAGS", "\\Seen")


def _poll_once(base: str = "UNSEEN") -> None:
    senders = _load_senders()
    conn = _connect()
    try:
        typ, data = conn.search(None, _search_criteria(senders, base))
        if typ != "OK":
            logger.warning("Búsqueda IMAP falló: %s", typ)
            return
        for msg_num in data[0].split():
            _process_message(conn, msg_num, senders)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


async def poll_loop() -> None:
    interval = int(os.environ.get("IMAP_POLL_INTERVAL_SECONDS", "60"))

    if not is_enabled():
        logger.info("Ingesta IMAP deshabilitada (falta IMAP_USER/IMAP_APP_PASSWORD), en espera")
        while not is_enabled():
            await asyncio.sleep(interval)

    while True:
        try:
            await asyncio.to_thread(_poll_once)
        except Exception:
            logger.exception("Falló el ciclo de sondeo IMAP")
        await asyncio.sleep(interval)
