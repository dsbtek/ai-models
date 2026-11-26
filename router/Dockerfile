FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries=5 -r requirements.txt

COPY models.json app.py config.py auth.py ./
COPY db ./db
COPY memory ./memory
COPY routes ./routes
COPY ui ./ui

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${API_PORT:-8080}"]
