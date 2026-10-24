# GG · Gestor de Gastos

Ledger personal de movimientos bancarios (Banco de Chile) en SQLite. Lee los correos del banco
por IMAP, los categoriza con reglas y los muestra en un dashboard web. Concilia los estados de
cuenta de la tarjeta de crédito y avisa por Telegram los movimientos sin categoría.

## Funcionalidades

- **Ingesta por IMAP (Gmail):** compras con crédito/débito, giros y transferencias
  (`parser.py`). Deduplica por `Message-ID` del correo.
- **Dashboard** (`/dashboard`): gasto mensual por categoría, débito vs crédito del mes,
  categorías/subcategorías, ahorro y proyección de pagos de tarjeta por mes de vencimiento.
- **Movimientos** (`/movements`): filtros, orden, comercios únicos (para crear reglas),
  edición de categoría/subcategoría/cuotas y el correo original de cada movimiento.
- **Estados de cuenta** (`/statements-view`): sube el PDF mensual y lo concilia con el ledger;
  completa las cuotas y permite agregar cargos que no llegan por correo.
- **Configuración** (`/config`): tarjetas (crédito/débito, nombre) y reglas de categorización
  (el patrón se busca en el comercio y en el detalle de transferencias).
- **Telegram:** cada día envía los movimientos sin categoría con botones para categorizarlos.

## Uso

```bash
cp .env.example .env                      # completar credenciales
cp imap/config.example.json imap/config.json
docker compose up -d --build              # http://127.0.0.1:8080
```

El puerto solo escucha en `127.0.0.1` porque la interfaz no tiene login. Para acceder desde
otros dispositivos usa un túnel SSH o `tailscale serve --bg 8080`.

Cargar historial (los correos ya leídos no los toma la ingesta automática):

```bash
docker compose exec backend python -m imap.backfill --days 90
```

Exportar/importar la configuración manual (reglas, tarjetas, categorías asignadas a mano):

```bash
docker compose exec -T backend python -m db.backup export > gg-config.json
docker compose exec -T backend python -m db.backup import < gg-config.json
```

## Desarrollo

```bash
uv sync
DB_PATH=gg.db uv run uvicorn main:app --reload --port 8080
uv run python -m pytest db imap test_parser.py test_statement.py test_telegram.py
```

| Archivo | Qué hace |
|---|---|
| `main.py` | API FastAPI y páginas |
| `db/` | Esquema SQLite, consultas, export/import |
| `imap/` | Ingesta IMAP, backfill y lectura del correo original |
| `parser.py` | Parsers de correos del banco |
| `statement.py` | Parser de estados de cuenta PDF, conciliación y calendario de facturación |
| `telegram_bot.py` | Bot de Telegram |
| `static/` | Dashboard y vistas (HTML + Chart.js) |
