"""Bot de Telegram sin red: api() reemplazada por un registro de llamadas.

Uso: uv run python -m pytest test_telegram.py
"""

from datetime import date

import pytest

import db
import telegram_bot as tg


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    db.init_db()
    calls = []
    monkeypatch.setattr(tg, "api", lambda method, **p: calls.append((method, p)) or {})
    return calls


def _press(data, message_id=1):
    tg.handle_update({"callback_query": {"id": "q", "data": data,
                                         "message": {"chat": {"id": 42}, "message_id": message_id}}})


def test_daily_flow(bot):
    db.upsert_rule("UBER", "Transporte")
    db.upsert_rule("PUNTOTICKET", "Ocio", "Tocata")
    db.insert_movement(5_000, "****3670", "UBER *TRIP", "23/10/2026 10:00", "s", "b")   # ya categorizado
    mid = db.insert_movement(20_000, "****3670", "BAR LA CUEVA", "23/10/2026 22:00", "s", "b")
    db.insert_movement(9_000, "****3670", "VIEJO", "01/10/2026 10:00", "s", "b")         # fuera de hoy/ayer

    assert tg.send_pending(date(2026, 10, 23)) == 1
    method, p = bot[-1]
    assert method == "sendMessage" and "BAR LA CUEVA" in p["text"] and p["text"].endswith(f"#{mid}")
    assert tg.send_pending(date(2026, 10, 23)) == 0  # idempotente

    _press(f"c|{mid}|Ocio")                       # categoría → ofrece subcategorías de Ocio
    edit = next(p for m, p in reversed(bot) if m == "editMessageText")
    assert "Tocata" in str(edit["reply_markup"])
    _press(f"s|{mid}|Tocata")
    _press(f"r|{mid}")                            # 📌 crea la regla
    m = db.get_movement(mid)
    assert (m["category"], m["subcategory"]) == ("Ocio", "Tocata")
    assert {"pattern": "BAR LA CUEVA", "category": "Ocio", "subcategory": "Tocata"} in db.list_rules()
    assert bot[-1][0] == "answerCallbackQuery"


def test_reply_with_text(bot):
    mid = db.insert_movement(7_000, "****3670", "FERIA", "23/10/2026 12:00", "s", "b")
    replied = f"🧾 FERIA\n$7.000\n#{mid}\n→ Algo"  # mensaje ya editado: el #id no está al final
    tg.handle_update({"message": {"chat": {"id": 42}, "text": "Comida / Feria",
                                  "reply_to_message": {"text": replied}}})
    m = db.get_movement(mid)
    assert (m["category"], m["subcategory"]) == ("Comida", "Feria")


def test_ignores_other_chats(bot):
    mid = db.insert_movement(7_000, "****3670", "FERIA", "23/10/2026 12:00", "s", "b")
    tg.handle_update({"message": {"chat": {"id": 666}, "text": "Hack", "reply_to_message": {"text": f"#{mid}"}}})
    assert db.get_movement(mid)["category"] == "Sin categoría" and bot == []


def test_note(bot):
    mid = db.insert_movement(12_000, "****3670", "FALABELLA", "23/10/2026 12:00", "s", "b")
    _press(f"t|{mid}")                                  # 🗒 Nota → pide escribirla
    method, p = bot[-2]                                 # (el último es answerCallbackQuery)
    assert method == "sendMessage" and p["text"].startswith(tg.NOTE_PROMPT) and p["reply_markup"]["force_reply"]
    tg.handle_update({"message": {"chat": {"id": 42}, "text": "Regalo cumpleaños Cata",
                                  "reply_to_message": {"text": p["text"]}}})
    m = db.get_movement(mid)
    assert (m["note"], m["category"]) == ("Regalo cumpleaños Cata", "Sin categoría")  # la nota no toca la categoría
    assert "🗒" in bot[-1][1]["text"] and f"x|{mid}" in str(bot[-1][1]["reply_markup"])  # sigue ofreciendo categorizar
