FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# pdftotext para leer los estados de cuenta en PDF
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*

COPY main.py parser.py statement.py telegram_bot.py .
COPY db ./db
COPY imap ./imap
COPY static ./static

# Archivo SQLite del ledger (montar un volumen en /data para persistirlo)
ENV DB_PATH=/data/gg.db
RUN mkdir -p /data

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
