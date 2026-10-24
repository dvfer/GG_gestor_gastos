"""
GG - Gestor de Gastos
=====================

Ledger de movimientos en SQLite alimentado por correos del banco (IMAP, en segundo plano),
con dashboard web, conciliación de estados de cuenta y bot de Telegram para categorizar.

Desarrollo local:
    DB_PATH=gg.db uvicorn main:app --reload --port 8080
"""

import asyncio
import html
import logging
import os
import re
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import statement
import telegram_bot
from imap import ingest

app = FastAPI(title="GG - Gestor de Gastos")


@app.on_event("startup")
async def _startup():
    db.init_db()
    # La ingesta IMAP corre en este mismo proceso: un solo writer sobre el archivo SQLite.
    app.state.imap_task = asyncio.create_task(ingest.poll_loop())
    app.state.telegram_task = asyncio.create_task(telegram_bot.run())


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/dashboard")


@app.get("/health")
async def health_check():
    db_ok = db.check_connection()
    return {"status": "healthy" if db_ok else "unhealthy", "database": db_ok}


@app.get("/expenses")
async def get_expenses(limit: int = 100, offset: int = 0):
    expenses = db.list_movements(limit=limit, offset=offset)
    return {"status": "success", "count": len(expenses), "data": expenses}


class MovementUpdate(BaseModel):
    category: Optional[str] = Field(None, max_length=60, description="Vacío = usar regla automática")
    subcategory: Optional[str] = Field(None, max_length=60, description="Se guarda junto a category")
    installments: Optional[int] = Field(None, ge=1, le=48)


class CategoryRule(BaseModel):
    pattern: str = Field(..., min_length=2, max_length=60)
    category: str = Field(..., min_length=1, max_length=60)
    subcategory: Optional[str] = Field(None, max_length=60)
    old_pattern: Optional[str] = Field(None, description="Al editar: patrón actual de la regla")


class CardConfig(BaseModel):
    card_type: str = Field(..., pattern=r"^\*{4}\d{4}$", description="****XXXX")
    label: Optional[str] = Field(None, max_length=40)
    is_credit: bool


@app.patch("/movements/{movement_id}")
async def update_movement(movement_id: int, update: MovementUpdate):
    if not db.update_movement(movement_id, update.category, update.installments, update.subcategory):
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    return {"status": "success"}


@app.get("/movements/{movement_id}/email")
async def movement_email(movement_id: int):
    """Correo original del movimiento: asunto, texto y enlace para abrirlo en Gmail."""
    row = db.get_movement_email(movement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    # ponytail: limpieza mínima (entidades + bloques CSS de los correos HTML) solo para leerlo
    text = re.sub(r"\{[^{}]*\}", " ", html.unescape(row["raw_body"] or ""))
    text = re.sub(r"\s*\n\s*", "\n", re.sub(r"[ \t\u00a0]+", " ", text)).strip()
    gmail_url = None
    if row["message_id"]:
        # Gmail busca por Message-ID con rfc822msgid:; authuser elige la cuenta correcta si hay varias
        gmail_url = (f"https://mail.google.com/mail/?authuser={quote(os.environ.get('IMAP_USER', ''))}"
                     f"#search/rfc822msgid%3A{quote(row['message_id'].strip('<>'), safe='')}")
    # Preferir el correo original desde Gmail convertido a texto limpio; si no, el texto guardado
    if row["message_id"] and not row["message_id"].startswith("eecc:") and ingest.is_enabled():
        try:
            text = await asyncio.to_thread(ingest.fetch_original_text, row["message_id"]) or text
        except Exception:
            logging.getLogger("gg").warning("No se pudo traer el correo original desde Gmail", exc_info=True)
    return {"subject": row["raw_subject"], "body": text, "message_id": row["message_id"], "gmail_url": gmail_url}


@app.get("/rules")
async def get_rules():
    return db.list_rules()


@app.put("/rules")
async def put_rule(rule: CategoryRule):
    try:
        db.upsert_rule(rule.pattern, rule.category, rule.subcategory, rule.old_pattern)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "success"}


@app.delete("/rules/{pattern}")
async def delete_rule(pattern: str):
    db.delete_rule(pattern)
    return {"status": "success"}


@app.get("/cards")
async def get_cards():
    return db.list_cards()


@app.put("/cards")
async def put_card(card: CardConfig):
    db.upsert_card(card.card_type, card.label, card.is_credit)
    return {"status": "success"}


MAX_PDF_BYTES = 10 * 1024 * 1024


@app.post("/statements")
async def upload_statement(request: Request):
    """Sube el PDF del estado de cuenta (cuerpo crudo, application/pdf), lo guarda y lo concilia.

    Las compras en cuotas que calzan con un movimiento actualizan sus cuotas en el ledger.
    """
    data = await request.body()
    if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Sube el PDF del estado de cuenta (máx. 10 MB)")
    password = os.environ.get("PDF_PASSWORD")
    if not password:
        raise HTTPException(status_code=500, detail="Falta PDF_PASSWORD en el entorno del servidor")
    try:
        text = await asyncio.to_thread(statement.pdf_to_text, data, password)
        parsed = statement.parse_statement(text)
    except statement.StatementError as e:
        raise HTTPException(status_code=400, detail=str(e))
    for st in parsed:
        db.save_statement(st)

    card, date_ = parsed[0]["card_type"], parsed[0]["statement_date"]
    result = statement.reconcile(db.statement_group(card, date_), db.card_movements(card))
    updated = 0
    for section in result["sections"]:
        for line in section["lines"]:
            m = line["movement"]
            if line["kind"] == "cuotas" and m and m["installments"] != line["installments"]:
                db.update_movement(m["id"], installments=line["installments"])
                updated += 1
    return {"card_type": card, "statement_date": date_, "installments_updated": updated,
            "checks": [{"currency": s["currency"], **s["check"]} for s in parsed]}


@app.get("/statements")
async def get_statements():
    return db.list_statements()


@app.get("/statements/reconciliation")
async def statement_reconciliation(card_type: str, statement_date: str):
    group = db.statement_group(card_type, statement_date)
    if not group:
        raise HTTPException(status_code=404, detail="Estado de cuenta no encontrado")
    return statement.reconcile(group, db.card_movements(card_type))


@app.post("/statement-lines/{line_id}/movement")
async def add_statement_line_to_ledger(line_id: int):
    """Agrega al ledger una línea del estado que no llegó por correo (ej. cargos PAC, comisiones)."""
    line = db.get_statement_line(line_id)
    if line is None or line["kind"] == "pago":
        raise HTTPException(status_code=404, detail="Línea no encontrada")
    y, m, d = line["op_date"].split("-")
    movement_id = db.insert_movement(
        amount=line["amount"], card_type=line["card_type"], merchant=line["description"],
        datetime_str=f"{d}/{m}/{y} 00:00", raw_subject="Estado de cuenta", raw_body="",
        is_credit=True, currency=line["currency"], installments=line["installments"],
        # Dedupe: la misma línea no se agrega dos veces aunque se vuelva a subir el PDF
        message_id=f"eecc:{line['card_type']}:{line['reference']}:{line['op_date']}:{line['amount']}",
    )
    return {"status": "success", "movement_id": movement_id}


@app.get("/summary")
async def summary(months: int = 12):
    """Datos del dashboard: gasto mensual por categoría y proyección de cuotas."""
    projection, basis = db.installment_projection()
    return {
        "monthly": db.monthly_by_category(max(1, min(months, 36))),
        "projection": projection,
        "projection_basis": basis,
    }


@app.middleware("http")
async def _no_stale_ui(request, call_next):
    # Que el navegador revalide HTML/JS/CSS en cada carga (ETag → 304 si no cambió); evita mezclar
    # una página nueva con un common.js viejo tras actualizar el contenedor.
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in ("/dashboard", "/movements", "/config", "/statements-view"):
        response.headers["Cache-Control"] = "no-cache"
    return response


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/movements", include_in_schema=False)
async def movements_page():
    return FileResponse(os.path.join(STATIC_DIR, "movements.html"))


@app.get("/statements-view", include_in_schema=False)
async def statements_page():
    return FileResponse(os.path.join(STATIC_DIR, "statements.html"))


@app.get("/config", include_in_schema=False)
async def config_page():
    return FileResponse(os.path.join(STATIC_DIR, "config.html"))
