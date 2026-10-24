"""Chequeo mínimo (sin conexión IMAP real) de imap/ingest.py.

Uso: uv run python -m pytest imap/test_ingest.py
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from imap import ingest as imap_ingest


def _multipart(plain=None, html=None):
    msg = MIMEMultipart("alternative")
    if plain is not None:
        msg.attach(MIMEText(plain, "plain", "utf-8"))
    if html is not None:
        msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


class FakeConn:
    """Stand-in para imaplib.IMAP4_SSL: sirve un correo fijo y registra los STORE."""

    def __init__(self, raw_bytes):
        self._raw = raw_bytes
        self.stored = []

    def fetch(self, msg_num, _spec):
        return "OK", [(b"1 (RFC822 {123})", self._raw)]

    def store(self, msg_num, flag_op, flags):
        self.stored.append((msg_num, flag_op, flags))


def test_extract_body_prefers_plain():
    msg = _multipart(plain="hola $1.000 mundo", html="<b>hola</b>")
    assert imap_ingest._extract_body(msg) == "hola $1.000 mundo"


def test_extract_body_strips_html_when_only_html():
    msg = _multipart(html="<p>Compra por <b>$5.000</b></p>")
    body = imap_ingest._extract_body(msg)
    assert "<" not in body and "$5.000" in body


def test_process_message_marks_seen_on_unparseable_email():
    msg = _multipart(plain="correo sin datos de transaccion")
    conn = FakeConn(msg.as_bytes())
    imap_ingest._process_message(conn, b"1")
    assert conn.stored == [(b"1", "+FLAGS", "\\Seen")]


def test_process_message_marks_seen_after_successful_parse(monkeypatch):
    inserted = {}

    def fake_insert_movement(**kwargs):
        inserted.update(kwargs)
        return 42

    monkeypatch.setattr(imap_ingest.db, "insert_movement", fake_insert_movement)

    msg = _multipart(
        plain="Se ha realizado una compra por $10.000 con Tarjeta de Crédito "
        "****1234 en TIENDA XYZ el 01/03/2026 12:00."
    )
    conn = FakeConn(msg.as_bytes())
    imap_ingest._process_message(conn, b"2")

    assert inserted["amount"] == 10000.0
    assert conn.stored == [(b"2", "+FLAGS", "\\Seen")]


def test_search_criteria_no_senders():
    assert imap_ingest._search_criteria([]) == "(UNSEEN)"


def test_search_criteria_one_sender():
    senders = [{"address": "a@bank.cl", "parser": "banco_chile"}]
    assert imap_ingest._search_criteria(senders) == '(UNSEEN FROM "a@bank.cl")'


def test_search_criteria_multiple_senders():
    senders = [
        {"address": "a@bank.cl", "parser": "banco_chile"},
        {"address": "b@bank.cl", "parser": "banco_chile"},
    ]
    assert imap_ingest._search_criteria(senders) == '(UNSEEN OR FROM "a@bank.cl" FROM "b@bank.cl")'


def test_load_senders_missing_file(monkeypatch):
    monkeypatch.setattr(imap_ingest, "CONFIG_PATH", "/nonexistent/config.json")
    assert imap_ingest._load_senders() == []


def test_load_senders_malformed_json(tmp_path, monkeypatch):
    bad_config = tmp_path / "config.json"
    bad_config.write_text("{not valid json")
    monkeypatch.setattr(imap_ingest, "CONFIG_PATH", str(bad_config))
    assert imap_ingest._load_senders() == []


def test_parser_for_sender_matched():
    senders = [{"address": "a@bank.cl", "parser": "banco_chile"}]
    parser = imap_ingest._parser_for_sender(senders, "Banco <a@bank.cl>")
    assert parser is imap_ingest.PARSERS["banco_chile"]


def test_parser_for_sender_unmatched_falls_back():
    senders = [{"address": "a@bank.cl", "parser": "banco_chile"}]
    parser = imap_ingest._parser_for_sender(senders, "Otro <otro@bank.cl>")
    assert parser is imap_ingest.PARSERS[imap_ingest.DEFAULT_PARSER]


def test_parser_for_sender_unknown_parser_id_falls_back():
    senders = [{"address": "a@bank.cl", "parser": "no_existe"}]
    parser = imap_ingest._parser_for_sender(senders, "Banco <a@bank.cl>")
    assert parser is imap_ingest.PARSERS[imap_ingest.DEFAULT_PARSER]

def test_backfill_criteria():
    from datetime import date

    from imap.backfill import imap_date

    since = imap_date(date(2026, 7, 5))
    assert since == "05-Jul-2026"
    senders = [{"address": "a@bank.cl"}]
    assert imap_ingest._search_criteria(senders, f"SINCE {since}") == '(SINCE 05-Jul-2026 FROM "a@bank.cl")'



def test_html_to_text():
    html = """<html><head><title>Banco</title><style>body, table, td { color: red } .ExternalClass {}</style></head>
    <body><img src="x"><p>Estimado(a)\r\n   Juan</p>\r\n\r\n<div> \r\n </div><table><tr><td>Nombre</td><td>Medio De Pago&nbsp;Fintoc</td></tr>
    <tr><td>Monto</td><td>$21.431</td></tr></table><script>alert(1)</script></body></html>"""
    assert imap_ingest.html_to_text(html) == "Estimado(a) Juan\nNombre · Medio De Pago Fintoc\nMonto · $21.431"
