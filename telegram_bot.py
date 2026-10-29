"""Bot de Telegram para categorizar movimientos.

Cada día a TELEGRAM_DAILY_AT (hora local, default 21:00) envía un mensaje por cada movimiento
de hoy/ayer sin categoría, con botones de categoría → subcategoría → "📌 Siempre" (crea regla).
"/pendientes" los envía al momento. Responder a un mensaje con "Categoría / Subcategoría"
asigna esa categoría escrita a mano; "🗒 Nota" pide una nota libre (qué se pagó, contexto).

Solo atiende a TELEGRAM_CHAT_ID; sin él configurado, responde a quien le escriba con su chat id
para configurarlo. Usa long polling: no expone ningún puerto.
"""

import asyncio
import html
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta

import db

logger = logging.getLogger("telegram")

MAX_CATEGORY_BUTTONS = 9


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def is_enabled() -> bool:
    return bool(_token())


def api(method: str, **params) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_token()}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=params.get("timeout", 0) + 15) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        # No loguear la URL: contiene el token
        raise RuntimeError(f"Telegram {method}: {e.code} {e.read().decode(errors='replace')[:200]}") from None


# --- Mensajes ---------------------------------------------------------------------------

def _money(m: dict) -> str:
    if m["currency"] == "USD":
        return f"US${m['amount']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"${m['amount']:,.0f}".replace(",", ".")


def _describe(m: dict) -> str:
    when = (m["occurred_at"] or m["created_at"] or "")[:16]
    card = f"{m['card_label'] or m['card_type'] or ''} · {'crédito' if m['is_credit'] else 'débito'}"
    memo = f"\n📝 {html.escape(m['memo'])}" if m.get("memo") else ""
    note = f"\n🗒 <i>{html.escape(m['note'])}</i>" if m.get("note") else ""
    return (f"🧾 <b>{html.escape(m['merchant'] or '—')}</b>{memo}{note}\n"
            f"{_money(m)} · {html.escape(when)} · {html.escape(card)}\n#{m['id']}")


def _label(m: dict) -> str:
    sub = f" › {m['subcategory']}" if m["subcategory"] else ""
    return html.escape(f"{m['category']}{sub}")


def _button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _fits(data: str) -> bool:
    return len(data.encode()) <= 64  # límite de callback_data de Telegram


def _rows(buttons: list[dict], per_row: int = 3) -> list[list[dict]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _category_keyboard(mid: int) -> dict:
    cats = [c for c in db.category_counts() if _fits(f"c|{mid}|{c}")][:MAX_CATEGORY_BUTTONS]
    rows = _rows([_button(c, f"c|{mid}|{c}") for c in cats])
    rows.append([_button("✏️ Otra", f"o|{mid}"), _button("🗒 Nota", f"t|{mid}"), _button("⏭ Omitir", f"x|{mid}")])
    return {"inline_keyboard": rows}


def _subcategory_keyboard(mid: int, category: str) -> dict:
    subs = [s for s in db.subcategories(category) if _fits(f"s|{mid}|{s}")]
    rows = _rows([_button(s, f"s|{mid}|{s}") for s in subs])
    rows.append([_button("Sin subcategoría", f"n|{mid}"), _button("✏️ Otra", f"o|{mid}"),
                 _button("↩️ Cambiar", f"b|{mid}")])
    return {"inline_keyboard": rows}


def _done_keyboard(mid: int) -> dict:
    return {"inline_keyboard": [[_button("📌 Siempre para este comercio", f"r|{mid}")],
                                [_button("🗒 Nota", f"t|{mid}"), _button("↩️ Cambiar", f"b|{mid}")]]}


def _keyboard_for(m: dict) -> dict:
    return _category_keyboard(m["id"]) if m["category"] == "Sin categoría" else _done_keyboard(m["id"])


NOTE_PROMPT = "🗒 Escribe una nota"  # marca del mensaje que pide la nota (se reconoce al responderlo)


def send_pending(today: date) -> int:
    """Envía los movimientos sin categoría pendientes de aviso. Idempotente (marca notified_at)."""
    pending = db.pending_notifications(today)
    for m in pending:
        api("sendMessage", chat_id=_chat_id(), text=_describe(m), parse_mode="HTML",
            reply_markup=_category_keyboard(m["id"]))
        db.mark_notified(m["id"])
    return len(pending)


# --- Respuestas -------------------------------------------------------------------------

def _edit(msg: dict, text: str, keyboard: dict | None = None) -> None:
    api("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
        text=text, parse_mode="HTML", reply_markup=keyboard or {"inline_keyboard": []})


def _set(mid: int, category: str, subcategory: str | None) -> dict | None:
    if not db.update_movement(mid, category=category, subcategory=subcategory or ""):
        return None
    return db.get_movement(mid)


def handle_callback(cq: dict) -> None:
    action, mid, *rest = cq["data"].split("|", 2)
    mid, value = int(mid), (rest[0] if rest else None)
    msg = cq["message"]
    m = db.get_movement(mid)
    if m is None:
        _edit(msg, "Movimiento no encontrado (¿se borró?)")
        return
    if action == "c":    # eligió categoría
        m = _set(mid, value, None)
        subs = db.subcategories(value)
        _edit(msg, f"{_describe(m)}\n→ {_label(m)}" + ("\n¿Subcategoría?" if subs else ""),
              _subcategory_keyboard(mid, value) if subs else _done_keyboard(mid))
    elif action == "s":  # eligió subcategoría
        m = _set(mid, m["category"], value)
        _edit(msg, f"{_describe(m)}\n✅ {_label(m)}", _done_keyboard(mid))
    elif action == "n":  # sin subcategoría
        _edit(msg, f"{_describe(m)}\n✅ {_label(m)}", _done_keyboard(mid))
    elif action == "r":  # crear regla para el comercio
        if m["merchant"] and m["category"] != "Sin categoría":
            db.upsert_rule(m["merchant"], m["category"], m["subcategory"])
            _edit(msg, f"{_describe(m)}\n✅ {_label(m)}\n📌 Regla creada: «{html.escape(m['merchant'])}»")
    elif action == "b":  # volver a elegir
        _edit(msg, _describe(m), _category_keyboard(mid))
    elif action == "o":  # escribir a mano
        api("sendMessage", chat_id=msg["chat"]["id"], parse_mode="HTML",
            text=f"Responde este mensaje con <code>Categoría</code> o <code>Categoría / Subcategoría</code> "
                 f"para {html.escape(m['merchant'] or '—')}\n#{mid}",
            reply_markup={"force_reply": True, "input_field_placeholder": "Ocio / Tocata"})
    elif action == "t":  # pedir nota libre
        api("sendMessage", chat_id=msg["chat"]["id"], parse_mode="HTML",
            text=f"{NOTE_PROMPT} para {html.escape(m['merchant'] or '—')} ({_money(m)}): qué se pagó, para quién…\n#{mid}",
            reply_markup={"force_reply": True, "input_field_placeholder": "Cumpleaños de la Cata"})
    elif action == "x":  # omitir
        _edit(msg, f"{_describe(m)}\n⏭ Omitido (queda sin categoría)")


_PERIODS = {"anteayer": "anteayer", "ayer": "ayer", "hoy": "hoy", "semana": "esta semana", "mes": "este mes"}


def _period(text: str, today: date) -> tuple[date, date, str] | None:
    # ponytail: palabras clave, no lenguaje natural; "anteayer" va antes que "ayer"
    word = next((w for w in _PERIODS if w in text), None)
    start = {"anteayer": today - timedelta(days=2), "ayer": today - timedelta(days=1), "hoy": today,
             "semana": today - timedelta(days=today.weekday()), "mes": today.replace(day=1)}.get(word)
    if start is None:
        return None
    return start, start if word in ("anteayer", "ayer") else today, _PERIODS[word]


def answer_spending(text: str, today: date) -> str | None:
    """«cuánto gasté con débito hoy» → resumen. None si el texto no es una consulta de gasto."""
    t = text.lower().replace("é", "e").replace("á", "a")
    if "gast" not in t:
        return None
    period = _period(t, today)
    if period is None:
        return "¿De cuándo? Prueba con hoy, ayer, anteayer, semana o mes. Ej: «cuánto gasté con débito ayer»"
    start, end, label = period
    want = 0 if "debito" in t else 1 if "credito" in t else None
    rows = [r for r in db.spending(start, end) if want is None or r["is_credit"] == want]
    lines = [f"💸 Gasto {label}" + {0: " con débito", 1: " con crédito", None: ""}[want] + ":"]
    for kind in (0, 1):
        for r in (r for r in rows if r["is_credit"] == kind):
            lines.append(f"{'💳 Crédito' if kind else '🏦 Débito'}: {_money({'amount': r['total'], 'currency': r['currency']})}"
                         f" ({r['n']} mov.)")
    return "\n".join(lines) if len(lines) > 1 else lines[0] + " nada 🎉"


def handle_message(msg: dict, today_fn=date.today) -> None:
    text = (msg.get("text") or "").strip()
    if text.startswith("/pendientes"):
        n = send_pending(today_fn())
        if not n:
            api("sendMessage", chat_id=msg["chat"]["id"], text="No hay movimientos sin categoría pendientes 🎉")
        return
    replied = (msg.get("reply_to_message") or {}).get("text") or ""
    if not replied and (reply := answer_spending(text, today_fn())):
        api("sendMessage", chat_id=msg["chat"]["id"], text=reply)
        return
    ref = re.search(r"(?m)^#(\d+)$", replied)
    if not ref or not text:
        api("sendMessage", chat_id=msg["chat"]["id"],
            text="Usa los botones de cada movimiento, responde uno con «Categoría / Subcategoría», o envía /pendientes. También: «cuánto gasté con débito hoy».")
        return
    mid = int(ref.group(1))
    if replied.startswith(NOTE_PROMPT):
        if not db.update_movement(mid, note=text):
            api("sendMessage", chat_id=msg["chat"]["id"], text="No encontré ese movimiento.")
            return
        m = db.get_movement(mid)
        api("sendMessage", chat_id=msg["chat"]["id"], parse_mode="HTML",
            text=f"{_describe(m)}\n✅ Nota guardada", reply_markup=_keyboard_for(m))
        return
    category, _, sub = (p.strip()[:60] for p in text.partition("/"))
    m = _set(mid, category, sub) if category else None
    if m is None:
        api("sendMessage", chat_id=msg["chat"]["id"], text="No pude asignar esa categoría.")
        return
    api("sendMessage", chat_id=msg["chat"]["id"], parse_mode="HTML",
        text=f"{_describe(m)}\n✅ {_label(m)}", reply_markup=_done_keyboard(mid))


def handle_update(update: dict) -> None:
    msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    if not _chat_id():
        if update.get("message"):
            api("sendMessage", chat_id=chat, text=f"Tu chat id es {chat}. Ponlo en TELEGRAM_CHAT_ID y reinicia.")
        return
    if chat != _chat_id():
        logger.warning("Ignorando mensaje de chat no autorizado %s", chat)
        return
    if "callback_query" in update:
        cq = update["callback_query"]
        try:
            handle_callback(cq)
        finally:
            api("answerCallbackQuery", callback_query_id=cq["id"])
    elif "message" in update:
        handle_message(update["message"])


# --- Loops ------------------------------------------------------------------------------

def _next_run(now: datetime, at: time) -> datetime:
    run = datetime.combine(now.date(), at)
    return run if run > now else run + timedelta(days=1)


async def _daily_loop(at: time) -> None:
    # Al arrancar después de la hora, se envía igual lo pendiente del día (es idempotente).
    if datetime.now().time() >= at and _chat_id():
        try:
            await asyncio.to_thread(send_pending, date.today())
        except Exception:
            logger.exception("Falló el envío pendiente a Telegram al arrancar")
    while True:
        now = datetime.now()
        await asyncio.sleep((_next_run(now, at) - now).total_seconds())
        if _chat_id():
            try:
                n = await asyncio.to_thread(send_pending, date.today())
                logger.info("Resumen diario: %s movimientos sin categoría enviados", n)
            except Exception:
                logger.exception("Falló el envío diario a Telegram")


async def _poll_loop() -> None:
    offset = None
    while True:
        try:
            updates = await asyncio.to_thread(
                api, "getUpdates", timeout=50, offset=offset, allowed_updates=["message", "callback_query"])
            for u in updates or []:
                offset = u["update_id"] + 1
                try:
                    await asyncio.to_thread(handle_update, u)
                except Exception:
                    logger.exception("Error procesando update de Telegram")
        except OSError as e:  # red caída / reset: una línea, sin traceback
            logger.warning("Sin conexión con Telegram (%s), reintentando", e)
            await asyncio.sleep(10)
        except Exception:
            logger.exception("Falló getUpdates, reintentando")
            await asyncio.sleep(10)


async def run() -> None:
    if not is_enabled():
        logger.info("Telegram deshabilitado (falta TELEGRAM_BOT_TOKEN)")
        return
    at = time.fromisoformat(os.environ.get("TELEGRAM_DAILY_AT", "21:00"))
    await asyncio.to_thread(api, "setMyCommands",
                            commands=[{"command": "pendientes", "description": "Movimientos sin categoría"}])
    await asyncio.gather(_poll_loop(), _daily_loop(at))
