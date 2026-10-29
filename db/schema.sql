-- Ledger de movimientos (SQLite). El monto es inmutable; solo category e
-- installments se editan desde el dashboard.
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount NUMERIC NOT NULL,
    card_type TEXT,
    merchant TEXT,
    occurred_at TEXT,               -- 'YYYY-MM-DD HH:MM' (hora local del banco)
    is_credit INTEGER NOT NULL DEFAULT 0,
    installments INTEGER NOT NULL DEFAULT 1 CHECK (installments >= 1),
    category TEXT,
    subcategory TEXT,               -- opcional; solo tiene sentido junto a category
    raw_subject TEXT,
    raw_body TEXT,
    message_id TEXT,                    -- Message-ID del correo, evita duplicados al re-ingerir
    currency TEXT NOT NULL DEFAULT 'CLP',  -- 'CLP' | 'USD'; gráficos y cuotas solo suman CLP
    notified_at TEXT,                   -- cuándo se avisó por Telegram que estaba sin categoría
    memo TEXT,                          -- detalle de transferencias (asunto/mensaje); las reglas también buscan aquí
    note TEXT,                          -- nota libre del usuario (qué se pagó, contexto)
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_movements_occurred_at ON movements (occurred_at DESC);

CREATE TRIGGER IF NOT EXISTS movements_amount_immutable
BEFORE UPDATE OF amount ON movements
BEGIN
    SELECT RAISE(ABORT, 'amount is immutable');
END;

-- Reglas de categorización: si el comercio contiene `pattern` (sin distinguir
-- mayúsculas), se asigna `category`. Gana el patrón más largo.
CREATE TABLE IF NOT EXISTS category_rules (
    pattern TEXT PRIMARY KEY COLLATE NOCASE,
    category TEXT NOT NULL,
    subcategory TEXT
);

-- Configuración por tarjeta/cuenta ('****XXXX'). Si existe, define si es crédito
-- (manda sobre lo que detecte el parser) y un nombre para mostrar.
CREATE TABLE IF NOT EXISTS cards (
    card_type TEXT PRIMARY KEY,
    label TEXT,
    is_credit INTEGER NOT NULL
);

-- Estados de cuenta de tarjeta de crédito subidos a mano (un PDF trae uno nacional CLP y
-- uno internacional USD). Se reemplazan si se vuelve a subir el mismo.
CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    statement_date TEXT NOT NULL,       -- 'YYYY-MM-DD' (fecha de facturación)
    period_from TEXT,
    period_to TEXT,
    due_date TEXT,                      -- pagar hasta
    next_period_to TEXT,                -- próximo corte
    total NUMERIC NOT NULL,             -- monto total facturado
    previous_total NUMERIC NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (card_type, currency, statement_date)
);

CREATE TABLE IF NOT EXISTS statement_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id INTEGER NOT NULL REFERENCES statements(id),
    kind TEXT NOT NULL,                 -- compra | cuotas | pago | cargo | devolucion
    op_date TEXT NOT NULL,
    reference TEXT,
    description TEXT,
    amount NUMERIC NOT NULL,            -- monto de la operación
    origin_amount NUMERIC,              -- internacional: monto en moneda de origen
    installment_n INTEGER NOT NULL DEFAULT 1,
    installments INTEGER NOT NULL DEFAULT 1,
    installment_amount NUMERIC NOT NULL -- cargo de este mes
);
