"""Estados de cuenta de Tarjeta de Crédito Banco de Chile (PDF) y su conciliación con el ledger.

El PDF trae dos estados: nacional (CLP) e internacional (US$). Cada uno se parsea a una
cabecera (período, vencimiento, total) y líneas (compras, cuotas, pagos y cargos del banco).

Uso manual: python statement.py archivo.pdf   (clave en PDF_PASSWORD)
"""

import calendar
import os
import re
import subprocess
import tempfile
from bisect import bisect_left
from datetime import date, datetime, timedelta
from typing import Optional


class StatementError(ValueError):
    pass


def pdf_to_text(data: bytes, password: str) -> str:
    """Descifra y extrae el texto conservando columnas (pdftotext -layout de poppler-utils)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        proc = subprocess.run(
            ["pdftotext", "-layout", "-upw", password, f.name, "-"],
            capture_output=True, timeout=30,
        )
    if proc.returncode != 0:
        msg = proc.stderr.decode(errors="replace").strip()
        if "password" in msg.lower():
            raise StatementError("Clave del PDF incorrecta (revisa PDF_PASSWORD)")
        raise StatementError(f"No se pudo leer el PDF: {msg}")
    return proc.stdout.decode("utf-8", errors="replace")


def _date(s: str) -> str:
    """'22/09/2026' o '22/09/26' → '2026-09-22'."""
    fmt = "%d/%m/%Y" if len(s) == 10 else "%d/%m/%y"
    return datetime.strptime(s, fmt).strftime("%Y-%m-%d")


def _clp(s: str) -> float:
    return float(s.replace(".", ""))


def _usd(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def _find(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(1) if m else None


# Línea nacional: LUGAR  FECHA  CÓDIGO  DESCRIPCIÓN  $ MONTO_OP  $ MONTO_TOTAL  NN/MM  $ VALOR_CUOTA
_NAT_LINE = re.compile(
    r"^(?P<place>.*?)\s*(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<ref>\d{12})\s+(?P<desc>.+?)\s*"
    r"\$\s*(?P<amount>-?[\d.]+)\s+\$\s*-?[\d.]+\s+(?P<n>\d{2})/(?P<m>\d{2})\s+\$\s*(?P<value>-?[\d.]+)\s*$"
)
# Línea internacional: REF  FECHA  DESCRIPCIÓN  [CIUDAD]  PAÍS  MONTO_ORIGEN  MONTO_US$
_INT_LINE = re.compile(
    r"^\s*(?P<ref>\d{4}\s+\d+)\s+(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<desc>.+?)\s{2,}.*?"
    r"(?P<country>[A-Z]{2})\s+(?P<origin>-?[\d.,]+)\s+(?P<amount>-?[\d.,]+)\s*$"
)


def _parse_national(text: str) -> dict:
    head = {
        "currency": "CLP",
        "statement_date": _date(_find(r"FECHA ESTADO DE CUENTA\s+(\d{2}/\d{2}/\d{4})", text)),
        "period_from": _find(r"PERÍODO FACTURADO\s+(\d{2}/\d{2}/\d{4})", text),
        "period_to": _find(r"PERÍODO FACTURADO\s+\d{2}/\d{2}/\d{4}\s+(\d{2}/\d{2}/\d{4})", text),
        "due_date": _find(r"PAGAR HASTA\s+(\d{2}/\d{2}/\d{4})", text),
        "next_period_to": _find(r"PRÓXIMO PERÍODO DE FACTURACIÓN\s+\d{2}/\d{2}/\d{4}\s+(\d{2}/\d{2}/\d{4})", text),
        "total": _clp(_find(r"MONTO TOTAL FACTURADO A PAGAR \([^)]*\)\s*\$\s*([\d.]+)", text) or "0"),
        "previous_total": _clp(_find(r"MONTO FACTURADO A PAGAR \(PERÍODO ANTERIOR\)\s+A\s+\$\s*(-?[\d.]+)", text) or "0"),
    }
    lines, section = [], "compra"
    for raw in text.splitlines():
        if "CARGOS, COMISIONES, IMPUESTOS Y ABONOS" in raw or "VOLUNTARIAMENTE CONTRATADOS" in raw:
            section = "cargo"  # comisiones, impuestos, seguros: los cobra el banco, no llegan por correo
        if "INFORMACIÓN COMPRAS EN CUOTAS" in raw:
            section = "compra"  # compras en cuotas nuevas (00/NN): se empiezan a cobrar el mes siguiente
        m = _NAT_LINE.match(raw)
        if not m:
            continue
        desc = re.sub(r"\s*TASA INT\.?\s.*$", "", m["desc"]).strip()
        n, total_n = int(m["n"]), int(m["m"])
        kind = ("pago" if "MONTO CANCELADO" in desc else
                "devolucion" if section == "cargo" and m["amount"].startswith("-") else
                section if section == "cargo" else
                "cuotas" if total_n > 1 else "compra")
        lines.append({
            "kind": kind, "op_date": _date(m["date"]), "reference": m["ref"],
            "description": " ".join(desc.split()), "amount": _clp(m["amount"]),
            "installment_n": n, "installments": total_n, "installment_amount": _clp(m["value"]),
        })
    return {**head, "lines": lines}


def _parse_international(text: str) -> dict:
    head = {
        "currency": "USD",
        "statement_date": _date(_find(r"FECHA ESTADO DE CUENTA\s+(\d{2}/\d{2}/\d{4})", text)),
        "period_from": _find(r"PERIODO FACTURADO DESDE\s+(\d{2}/\d{2}/\d{4})", text),
        "period_to": _find(r"PERIODO FACTURADO HASTA\s+(\d{2}/\d{2}/\d{4})", text),
        "due_date": _find(r"PAGAR HASTA\s+(\d{2}/\d{2}/\d{4})", text),
        "next_period_to": None,
        "total": _usd(_find(r"DEUDA TOTAL\s+US\$\s*(-?[\d.,]+)", text) or "0"),
        "previous_total": _usd(_find(r"SALDO ANTERIOR FACTURADO\s+US\$\s*(-?[\d.,]+)", text) or "0"),
    }
    lines = []
    for raw in text.splitlines():
        m = _INT_LINE.match(raw)
        if not m:
            continue
        amount = _usd(m["amount"])
        lines.append({
            "kind": "pago" if amount < 0 else "compra", "op_date": _date(m["date"]),
            "reference": " ".join(m["ref"].split()), "description": " ".join(m["desc"].split()),
            "amount": amount, "installment_n": 1, "installments": 1, "installment_amount": amount,
            # Monto en moneda de origen: si el comercio cobró en CLP (ej. AliExpress), el correo
            # trae ese monto en pesos y sirve para conciliar.
            "origin_amount": _usd(m["origin"]),
        })
    return {**head, "lines": lines}


def parse_statement(text: str) -> list[dict]:
    """Devuelve los estados (nacional y/o internacional) contenidos en el texto del PDF."""
    card = _find(r"(?m)N° DE TARJETA DE CR[ÉE]DITO\s+[X\d ]*?(\d{4})\s*$", text)
    if not card:
        raise StatementError("No parece un estado de cuenta de Tarjeta de Crédito Banco de Chile")
    national, _, international = text.partition("ESTADO DE CUENTA INTERNACIONAL")
    out = []
    if "ESTADO DE CUENTA NACIONAL" in national:
        out.append(_parse_national(national))
    if international:
        out.append(_parse_international(international))
    for st in out:
        st["card_type"] = f"****{card}"
        for k in ("period_from", "period_to", "due_date", "next_period_to"):
            st[k] = _date(st[k]) if st[k] else None
        st["check"] = check_totals(st)
    return out


def check_totals(st: dict) -> dict:
    """Total facturado = saldo anterior + líneas (cuota del mes). Si no cuadra, faltó parsear algo.

    Las cuotas 00/NN son informativas: la primera se cobra en el estado siguiente.
    """
    parsed = round(st["previous_total"] + sum(l["installment_amount"] for l in st["lines"]
                                               if l["installment_n"] >= 1), 2)
    return {"parsed": parsed, "total": st["total"], "ok": abs(parsed - st["total"]) < 0.01}


def _amounts(line: dict, currency: str) -> set[tuple[str, float]]:
    out = {(currency, round(line["amount"], 2))}
    if line.get("origin_amount"):
        out.add(("CLP", round(line["origin_amount"], 2)))
    return out


def match_lines(lines: list[dict], movements: list[dict], currency: str, tolerance_days: int = 7,
                taken: Optional[set] = None) -> dict[int, int]:
    """Concilia compras/cuotas del estado con movimientos del ledger de la misma tarjeta.

    Calza por monto exacto (en la moneda del estado, o el monto de origen en CLP) y fecha a
    ≤ tolerance_days (las suscripciones se registran días después del correo); uno a uno, el
    más cercano. `taken` excluye movimientos ya conciliados. Devuelve {índice de línea: id}.
    """
    taken = taken if taken is not None else set()
    out = {}
    for i, line in enumerate(lines):
        if line["kind"] not in ("compra", "cuotas", "devolucion"):
            continue
        d = date.fromisoformat(line["op_date"])
        amounts = _amounts(line, currency)
        best = min(
            ((abs((date.fromisoformat(m["occurred_at"][:10]) - d).days), m["id"])
             for m in movements
             if m["id"] not in taken and m["occurred_at"]
             and (m["currency"], round(float(m["amount"]), 2)) in amounts),
            default=None,
        )
        if best is not None and best[0] <= tolerance_days:
            out[i] = best[1]
            taken.add(best[1])
    return out



def reconcile(statements: list[dict], movements: list[dict]) -> dict:
    """Cruza los estados de un PDF (CLP primero, luego USD) con los movimientos de la tarjeta.

    - sections: cada estado con sus líneas y el movimiento conciliado (o None).
    - ledger_only: movimientos del período que no aparecen en el estado (validaciones de
      tarjeta anuladas, compras que quedaron para el corte siguiente, etc.).
    """
    taken: set = set()
    by_id = {m["id"]: m for m in movements}
    sections = []
    for st in statements:
        matches = match_lines(st["lines"], movements, st["currency"], taken=taken)
        lines = [{**line, "movement": by_id.get(matches.get(i))} for i, line in enumerate(st["lines"])]
        sections.append({**st, "lines": lines, "check": check_totals(st)})
    period = next((s for s in statements if s["period_from"]), None)
    ledger_only = [
        m for m in movements
        if period and m["id"] not in taken and m["occurred_at"]
        and period["period_from"] <= m["occurred_at"][:10] <= period["period_to"]
    ] if period else []
    return {"sections": sections, "ledger_only": sorted(ledger_only, key=lambda m: m["occurred_at"])}


def _shift_weekend(d: date) -> date:
    # Sábado/domingo → viernes anterior (visto: corte 21/08/2026 viernes en vez del sábado 22)
    return d - timedelta(days=max(0, d.weekday() - 4))


class BillingSchedule:
    """Fechas de corte y vencimiento de una tarjeta, a partir de sus estados de cuenta (CLP).

    Usa los cortes reales (y el próximo corte que informa el último estado); fuera de ese rango
    los extrapola mes a mes con el mismo día de corte. El vencimiento estimado es el corte más
    los mismos días que en el último estado (ej. 22/09 → 06/10 = 14 días).
    """

    def __init__(self, statements: list[dict]):
        statements = sorted(statements, key=lambda s: s["statement_date"])
        known = {date.fromisoformat(s["statement_date"]): s["due_date"] and date.fromisoformat(s["due_date"])
                 for s in statements}
        latest = statements[-1]
        last_cut = date.fromisoformat(latest["statement_date"])
        offset = (known[last_cut] - last_cut).days if known[last_cut] else 14
        if latest.get("next_period_to"):
            known.setdefault(date.fromisoformat(latest["next_period_to"]), None)
        # ponytail: día de corte fijo + ajuste de fin de semana; los feriados los corrige el estado real al subirlo
        cut_day = last_cut.day
        cuts = dict(known)
        months_with_cut = {(c.year, c.month) for c in known}
        first = min(known)
        for i in range(-36, 12 * 6):
            y, m = divmod(first.year * 12 + first.month - 1 + i, 12)
            if (y, m + 1) not in months_with_cut:
                cuts[_shift_weekend(date(y, m + 1, min(cut_day, calendar.monthrange(y, m + 1)[1])))] = None
        self.cuts = sorted(cuts)
        self.due = {c: cuts[c] or c + timedelta(days=offset) for c in self.cuts}

    def cut_index(self, d: date) -> int:
        """Índice del corte en que se factura una compra hecha el día d (primer corte >= d)."""
        return bisect_left(self.cuts, d)

    def next_cut(self, today: date) -> tuple[date, date]:
        c = self.cuts[self.cut_index(today)]
        return c, self.due[c]

    def next_payment(self, today: date) -> tuple[date, date]:
        """(corte, vencimiento) del próximo pago: puede ser de un corte que ya pasó y aún no vence."""
        c = next(c for c in self.cuts if self.due[c] >= today)
        return c, self.due[c]


def project_by_due(purchases: list[tuple[float, str, int]], schedule: BillingSchedule, today: date) -> dict[str, float]:
    """Cuotas por pagar agrupadas por mes de vencimiento. purchases = [(monto, 'YYYY-MM-DD', cuotas)]."""
    out: dict[str, float] = {}
    for amount, day, n in purchases:
        i = schedule.cut_index(date.fromisoformat(day[:10]))
        for k in range(n):
            if i + k >= len(schedule.cuts):
                break
            due = schedule.due[schedule.cuts[i + k]]
            if due >= today:
                key = due.strftime("%Y-%m")
                out[key] = out.get(key, 0) + float(amount) / n
    return dict(sorted(out.items()))


if __name__ == "__main__":
    import json
    import sys

    with open(sys.argv[1], "rb") as f:
        for st in parse_statement(pdf_to_text(f.read(), os.environ["PDF_PASSWORD"])):
            print(json.dumps({k: v for k, v in st.items() if k != "lines"}, ensure_ascii=False))
            for line in st["lines"]:
                print("   ", line)
