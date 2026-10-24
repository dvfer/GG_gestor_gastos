"""Recolecta los movimientos de los últimos N días desde Gmail, incluidos correos ya leídos.

Los correos que ya estaban en el ledger se omiten (dedupe por Message-ID), así que se
puede correr más de una vez sin duplicar.

Uso (desde la raíz del repo, con IMAP_USER/IMAP_APP_PASSWORD/DB_PATH en el entorno):
    python -m imap.backfill            # últimos 90 días (~3 meses)
    python -m imap.backfill --days 30
En docker compose:
    docker compose exec backend python -m imap.backfill
"""

import argparse
import logging
from datetime import date, timedelta

import db
from imap import ingest

# IMAP exige meses en inglés; strftime('%b') depende del locale.
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=90, help="días hacia atrás (default 90)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not ingest.is_enabled():
        raise SystemExit("Faltan IMAP_USER / IMAP_APP_PASSWORD en el entorno")

    db.init_db()
    since = imap_date(date.today() - timedelta(days=args.days))
    logging.info("Buscando correos desde %s", since)
    ingest._poll_once(f"SINCE {since}")


if __name__ == "__main__":
    main()
