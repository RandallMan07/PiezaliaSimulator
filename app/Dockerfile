FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=postgresql://postgres:postgres@postgres:5432/rovira

WORKDIR /app

# Cache dependencies independently of application code.
COPY app/piezalink/requirements.txt ./piezalink/requirements.txt
RUN python -m pip install --no-cache-dir -r piezalink/requirements.txt

COPY app/setup.py ./
COPY app/piezalink/ ./piezalink/
RUN python -m pip install --no-cache-dir --no-deps .

# Simulation feeds are available to explicit import commands.
COPY data/feed_dia*.csv ./data/

ENTRYPOINT ["piezalink"]
