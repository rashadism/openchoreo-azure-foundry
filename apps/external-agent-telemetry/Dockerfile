FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

# Run as non-root
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# PORT is read by agent.py; uvicorn binds 0.0.0.0
CMD ["python", "agent.py"]
