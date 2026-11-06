FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  DATABASE_URL=postgresql://postgres:postgres@piezalia-db:5432/rovira \
  ACCOUNTS_CSV=/app/accounts.csv

WORKDIR /app

RUN apk add --no-cache bash

# Cache dependencies independently of application code.
COPY app/piezalink/requirements.txt ./piezalink/requirements.txt
RUN python -m pip install --no-cache-dir -r piezalink/requirements.txt

COPY app/setup.py ./
COPY app/piezalink/ ./piezalink/
RUN python -m pip install --no-cache-dir --no-deps .

# Seed accounts are loaded by init_db on first schema setup/upgrade.
COPY accounts.csv ./accounts.csv

# Simulation feeds are available to explicit import commands.
COPY data/feed_dia*.csv ./data/

# Keep the development container running; invoke PiezaLink explicitly.
CMD ["sleep", "infinity"]
