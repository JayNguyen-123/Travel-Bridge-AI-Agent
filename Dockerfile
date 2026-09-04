FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ ./config/
COPY middleware/ ./middleware/
COPY tools/ ./tools/
COPY payments/ ./payments/
COPY notifications/ ./notifications/
COPY db/ ./db/
COPY agents/ ./agents/
COPY server/ ./server/

EXPOSE 8080
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# uvicorn serves the FastAPI app in server/main.py (websocket voice endpoint +
# Stripe webhook + static browser client). This replaces the old
# `CMD ["python", "app.py"]`, which called a non-existent ADK runtime API.
CMD ["sh", "-c", "uvicorn server.main:app --host 0.0.0.0 --port ${PORT}"]
