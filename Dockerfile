FROM python:3.12-slim

RUN groupadd -r botuser && useradd -r -g botuser -m botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

RUN mkdir -p /app/logs /app/data && chown -R botuser:botuser /app

USER botuser

CMD ["python", "-m", "src.main"]
