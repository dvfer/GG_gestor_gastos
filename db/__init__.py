"""Acceso a la base de datos (SQLite) para el ledger de movimientos."""

import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from typing import Any, Optional

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Categoría/subcategoría efectivas: las asignadas a mano, o las de la regla de patrón más
# largo. Van siempre en par (una categoría manual no hereda la subcategoría de la regla).
_FROM_SQL = """
    movements m LEFT JOIN category_rules r ON r.pattern = (
        SELECT r2.pattern FROM category_rules r2
        WHERE instr(upper(COALESCE(m.merchant, '') || ' ' || COALESCE(m.memo, '')), upper(r2.pattern)) > 0
        ORDER BY length(r2.pattern) DESC LIMIT 1
    )
"""
_CATEGORY_SQL = "COALESCE(m.category, r.category, 'Sin categoría')"
_SUBCATEGORY_SQL = "CASE WHEN m.category IS NOT NULL THEN m.subcategory ELSE r.subcategory END"
# Crédito efectivo: lo configurado para la tarjeta; si no está configurada, lo que detectó el parser.
_CREDIT_SQL = "COALESCE((SELECT c.is_credit FROM cards c WHERE c.card_type = m.card_type), m.is_credit)"
_MONTH_SQL = "strftime('%Y-%m', COALESCE(m.occurred_at, m.created_at))"


def _connect() -> sqlite3.Connection:
    # ponytail: una conexión por llamada, es barato en SQLite y evita compartir entre hilos
    conn = sqlite3.connect(os.environ.get("DB_PATH", "gg.db"), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # lectores (dashboard) no bloquean al writer (IMAP)
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            conn.executescript(f.read())
        # Migración para DBs creadas antes de estas columnas
        for table, col, ddl in [
            ("movements", "message_id", "TEXT"),
            ("movements", "currency", "TEXT NOT NULL DEFAULT 'CLP'"),
            ("movements", "subcategory", "TEXT"),
            ("category_rules", "subcategory", "TEXT"),
            ("movements", "notified_at", "TEXT"),
            ("movements", "memo", "TEXT"),
            ("movements", "note", "TEXT"),
        ]:
            if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_movements_message_id ON movements (message_id)")


def _parse_occurred_at(datetime_str: Optional[str]) -> Optional[str]:
    if not datetime_str:
        return None
    try:
        return datetime.strptime(datetime_str, "%d/%m/%Y %H:%M").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None


def insert_movement(
    amount: float,
    card_type: Optional[str],
    merchant: Optional[str],
    datetime_str: Optional[str],
    raw_subject: str,
    raw_body: str,
    is_credit: bool = False,
    message_id: Optional[str] = None,
    currency: str = "CLP",
    installments: int = 1,
    memo: Optional[str] = None,
) -> Optional[int]:
    """Inserta el movimiento. Retorna None si el correo (message_id) ya estaba en el ledger."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            """
            INSERT INTO movements
                (amount, card_type, merchant, occurred_at, is_credit, raw_subject, raw_body, message_id, currency,
                 installments, memo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO NOTHING
            """,
            (amount, card_type, merchant, _parse_occurred_at(datetime_str), int(is_credit),
             raw_subject, raw_body, message_id, currency, installments, memo),
        )
        return cur.lastrowid if cur.rowcount else None


def list_movements(limit: int, offset: int, movement_id: Optional[int] = None) -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT m.id, m.amount, m.card_type, m.merchant, m.memo, m.note, m.occurred_at, {_CREDIT_SQL} AS is_credit,
                   (SELECT c.label FROM cards c WHERE c.card_type = m.card_type) AS card_label,
                   m.installments, m.currency, {_CATEGORY_SQL} AS category,
                   {_SUBCATEGORY_SQL} AS subcategory, m.message_id, (m.message_id IS NOT NULL AND m.message_id NOT LIKE 'eecc:%') AS has_email, m.created_at
            FROM {_FROM_SQL}
            WHERE ? IS NULL OR m.id = ?
            ORDER BY COALESCE(m.occurred_at, m.created_at) DESC, m.id DESC
            LIMIT ? OFFSET ?
            """,
            (movement_id, movement_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_movement(movement_id: int) -> Optional[dict[str, Any]]:
    rows = list_movements(1, 0, movement_id)
    return rows[0] if rows else None


def pending_notifications(today: date) -> list[dict[str, Any]]:
    """Movimientos de hoy y ayer sin categoría (ni manual ni por regla) que aún no se avisaron."""
    with closing(_connect()) as conn:
        ids = [r[0] for r in conn.execute(
            f"""
            SELECT m.id FROM {_FROM_SQL}
            WHERE m.category IS NULL AND r.category IS NULL AND m.notified_at IS NULL
              AND date(COALESCE(m.occurred_at, m.created_at)) BETWEEN date(?, '-1 day') AND date(?)
            ORDER BY COALESCE(m.occurred_at, m.created_at)
            """,
            (today.isoformat(), today.isoformat()),
        )]
    return [get_movement(i) for i in ids]


def mark_notified(movement_id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE movements SET notified_at = datetime('now', 'localtime') WHERE id = ?", (movement_id,))


def category_counts() -> list[str]:
    """Categorías en uso (manuales o por regla), de más a menos usada."""
    with closing(_connect()) as conn:
        return [r[0] for r in conn.execute(
            f"""
            SELECT c FROM (
                SELECT {_CATEGORY_SQL} AS c, count(*) AS n FROM {_FROM_SQL} GROUP BY 1
                UNION ALL SELECT category, 0 FROM category_rules
            ) WHERE c != 'Sin categoría'
            GROUP BY c ORDER BY max(n) DESC, c
            """
        )]


def subcategories(category: str) -> list[str]:
    with closing(_connect()) as conn:
        return [r[0] for r in conn.execute(
            f"""
            SELECT DISTINCT {_SUBCATEGORY_SQL} FROM {_FROM_SQL}
            WHERE {_CATEGORY_SQL} = ? COLLATE NOCASE AND {_SUBCATEGORY_SQL} IS NOT NULL
            UNION SELECT subcategory FROM category_rules
            WHERE category = ? COLLATE NOCASE AND subcategory IS NOT NULL
            ORDER BY 1
            """,
            (category, category),
        )]


def update_movement(
    movement_id: int,
    category: Optional[str] = None,
    installments: Optional[int] = None,
    subcategory: Optional[str] = None,
    note: Optional[str] = None,
) -> bool:
    """Actualiza categoría+subcategoría (categoría vacía = volver a las reglas), cuotas y/o nota."""
    sets, params = [], []
    if category is not None:
        category = category.strip() or None
        sets += ["category = ?", "subcategory = ?"]
        params += [category, ((subcategory or "").strip() or None) if category else None]
    if installments is not None:
        sets.append("installments = ?")
        params.append(installments)
    if note is not None:
        sets.append("note = ?")
        params.append(note.strip()[:500] or None)
    if not sets:
        return True
    with closing(_connect()) as conn, conn:
        cur = conn.execute(f"UPDATE movements SET {', '.join(sets)} WHERE id = ?", (*params, movement_id))
        return cur.rowcount == 1


def list_rules() -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT pattern, category, subcategory FROM category_rules ORDER BY category, subcategory, pattern"
        )]


def upsert_rule(
    pattern: str, category: str, subcategory: Optional[str] = None, old_pattern: Optional[str] = None
) -> None:
    """Crea o actualiza una regla. Con `old_pattern` edita esa regla (permite cambiarle el patrón).

    Lanza ValueError si el patrón nuevo ya pertenece a otra regla, para no pisarla en silencio.
    """
    pattern = pattern.strip()
    with closing(_connect()) as conn, conn:
        if old_pattern is not None and old_pattern.lower() != pattern.lower():
            if conn.execute("SELECT 1 FROM category_rules WHERE pattern = ?", (pattern,)).fetchone():
                raise ValueError(f"Ya existe una regla con el patrón {pattern!r}")
            conn.execute("DELETE FROM category_rules WHERE pattern = ?", (old_pattern,))
        conn.execute(
            "INSERT INTO category_rules (pattern, category, subcategory) VALUES (?, ?, ?) "
            "ON CONFLICT(pattern) DO UPDATE SET category = excluded.category, subcategory = excluded.subcategory",
            (pattern, category.strip(), (subcategory or "").strip() or None),
        )


def delete_rule(pattern: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM category_rules WHERE pattern = ?", (pattern,))


def list_cards() -> list[dict[str, Any]]:
    """Tarjetas vistas en movimientos + configuradas, con su tipo efectivo."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT k.card_type, c.label,
                   COALESCE(c.is_credit, (SELECT max(is_credit) FROM movements WHERE card_type = k.card_type), 0) AS is_credit,
                   c.card_type IS NOT NULL AS configured,
                   (SELECT count(*) FROM movements WHERE card_type = k.card_type) AS movements
            FROM (SELECT card_type FROM movements WHERE card_type IS NOT NULL UNION SELECT card_type FROM cards) k
            LEFT JOIN cards c ON c.card_type = k.card_type
            ORDER BY k.card_type
            """
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_card(card_type: str, label: Optional[str], is_credit: bool) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO cards (card_type, label, is_credit) VALUES (?, ?, ?) "
            "ON CONFLICT(card_type) DO UPDATE SET label = excluded.label, is_credit = excluded.is_credit",
            (card_type, (label or "").strip() or None, int(is_credit)),
        )


def monthly_by_category(months: int) -> list[dict[str, Any]]:
    """Gasto por mes de compra, categoría y subcategoría, últimos `months` meses (monto completo, no cuotas)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT {_MONTH_SQL} AS month, {_CATEGORY_SQL} AS category, {_SUBCATEGORY_SQL} AS subcategory,
                   {_CREDIT_SQL} AS is_credit, SUM(m.amount) AS total
            FROM {_FROM_SQL}
            WHERE m.currency = 'CLP'
              AND {_MONTH_SQL} >= strftime('%Y-%m', 'now', 'localtime', 'start of month', ?)
            GROUP BY 1, 2, 3, 4  -- por posición: los alias chocan con las columnas m.category/m.subcategory
            ORDER BY month, total DESC
            """,
            (f"-{months - 1} months",),
        ).fetchall()
        return [dict(r) for r in rows]


_STATEMENT_COLS = ("card_type", "currency", "statement_date", "period_from", "period_to",
                   "due_date", "next_period_to", "total", "previous_total")
_LINE_COLS = ("kind", "op_date", "reference", "description", "amount", "origin_amount",
              "installment_n", "installments", "installment_amount")


def save_statement(st: dict) -> int:
    """Guarda un estado de cuenta parseado (statement.parse_statement); reemplaza si ya existía."""
    with closing(_connect()) as conn, conn:
        old = conn.execute(
            "SELECT id FROM statements WHERE card_type = ? AND currency = ? AND statement_date = ?",
            (st["card_type"], st["currency"], st["statement_date"]),
        ).fetchone()
        if old:
            conn.execute("DELETE FROM statement_lines WHERE statement_id = ?", (old["id"],))
            conn.execute("DELETE FROM statements WHERE id = ?", (old["id"],))
        sid = conn.execute(
            f"INSERT INTO statements ({', '.join(_STATEMENT_COLS)}) VALUES ({', '.join('?' * len(_STATEMENT_COLS))})",
            [st[c] for c in _STATEMENT_COLS],
        ).lastrowid
        conn.executemany(
            f"INSERT INTO statement_lines (statement_id, {', '.join(_LINE_COLS)}) "
            f"VALUES (?, {', '.join('?' * len(_LINE_COLS))})",
            [(sid, *[line.get(c) for c in _LINE_COLS]) for line in st["lines"]],
        )
        return sid


def list_statements() -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM statements ORDER BY statement_date DESC, card_type, currency = 'USD'"
        )]


def statement_group(card_type: str, statement_date: str) -> list[dict[str, Any]]:
    """Estados (CLP y USD) de un mismo PDF, con sus líneas."""
    with closing(_connect()) as conn:
        sts = [dict(r) for r in conn.execute(
            "SELECT * FROM statements WHERE card_type = ? AND statement_date = ? ORDER BY currency = 'USD'",
            (card_type, statement_date),
        )]
        for st in sts:
            st["lines"] = [dict(r) for r in conn.execute(
                "SELECT * FROM statement_lines WHERE statement_id = ? ORDER BY id", (st["id"],)
            )]
        return sts


def card_movements(card_type: str) -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(
            f"""
            SELECT m.id, m.amount, m.currency, m.occurred_at, m.merchant, m.installments,
                   {_CATEGORY_SQL} AS category
            FROM {_FROM_SQL}
            WHERE m.card_type = ?
            """,
            (card_type,),
        )]


def get_statement_line(line_id: int) -> Optional[dict[str, Any]]:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT l.*, s.card_type, s.currency FROM statement_lines l "
            "JOIN statements s ON s.id = l.statement_id WHERE l.id = ?",
            (line_id,),
        ).fetchone()
        return dict(row) if row else None


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + n
    return total // 12, total % 12 + 1


def project_installments(purchases: list[tuple[float, str, int]], today: date) -> dict[str, float]:
    """Cuotas por pagar desde el mes actual. purchases = [(monto, 'YYYY-MM', cuotas)].

    La cuota k (1..n) se cobra k meses después del mes de compra.
    """
    # ponytail: ignora fecha de corte de la tarjeta y intereses; agregar si la proyección se desvía del estado de cuenta
    out: dict[str, float] = {}
    current = (today.year, today.month)
    for amount, month, n in purchases:
        y, m = int(month[:4]), int(month[5:7])
        for k in range(1, n + 1):
            ym = _add_months(y, m, k)
            if ym >= current:
                key = f"{ym[0]:04d}-{ym[1]:02d}"
                out[key] = out.get(key, 0) + float(amount) / n
    return dict(sorted(out.items()))


def installment_projection(today: Optional[date] = None) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Cuotas por pagar por mes y, por tarjeta, en qué se basa la proyección.

    Tarjetas con estados de cuenta cargados: por mes de vencimiento real/estimado (corte y
    vencimiento del estado). Sin estados: la cuota k cae k meses después del mes de compra.
    """
    from statement import BillingSchedule, project_by_due  # import local: statement no depende de db

    today = today or date.today()
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"""
            SELECT m.card_type, m.amount, COALESCE(m.occurred_at, m.created_at) AS day, m.installments
            FROM movements m WHERE {_CREDIT_SQL} = 1 AND m.currency = 'CLP'
            """
        ).fetchall()
        statements = [dict(r) for r in conn.execute(
            "SELECT card_type, statement_date, due_date, next_period_to FROM statements WHERE currency = 'CLP'"
        )]
    total: dict[str, float] = {}
    basis = []
    for card in sorted({r["card_type"] for r in rows}, key=str):
        purchases = [(r["amount"], r["day"], r["installments"]) for r in rows if r["card_type"] == card]
        card_statements = [s for s in statements if s["card_type"] == card]
        if card_statements:
            schedule = BillingSchedule(card_statements)
            proj = project_by_due(purchases, schedule, today)
            pay_cut, pay_due = schedule.next_payment(today)
            basis.append({"card_type": card, "next_cut": schedule.next_cut(today)[0].isoformat(),
                          "payment_cut": pay_cut.isoformat(), "payment_due": pay_due.isoformat()})
        else:
            proj = project_installments([(a, d[:7], n) for a, d, n in purchases], today)
            basis.append({"card_type": card, "next_cut": None, "payment_cut": None, "payment_due": None})
        for k, v in proj.items():
            total[k] = total.get(k, 0) + v
    return dict(sorted(total.items())), basis


def check_connection() -> bool:
    try:
        with closing(_connect()) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
