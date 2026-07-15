FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py create_agent.py ./

# Non-root for the platform.
RUN useradd -m appuser
USER appuser

EXPOSE 8080

# app.py reads PORT from env (default 8080).
CMD ["python", "app.py"]
