FROM python:3.13-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# No compiler needed: every dependency ships a manylinux wheel for cp313.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim AS runner

WORKDIR /app

RUN addgroup --system appuser \
    && adduser --system --group appuser \
    && chown appuser:appuser /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --chown=appuser:appuser . .

USER appuser

# A worker: no port, no HTTP server. Exec form, so SIGTERM reaches Python and the state is saved on stop.
CMD ["python", "-m", "app.main"]
