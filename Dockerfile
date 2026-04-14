# ---- build stage ----
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# ---- runtime stage ----
FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

CMD ["python", "-m", "doctor_collector", "--collect"]
