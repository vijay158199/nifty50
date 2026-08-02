# NIFTY 50 ICT/SMC strategy dashboard - single container: FastAPI app +
# in-process APScheduler (live monitor + 17:00 IST daily report).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first so this layer is cached across code-only changes.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# Matches the local dev convention (`cd backend && python run.py`) so all the
# relative paths in app/config.py (DATA_DIR = backend/../data, etc.) resolve
# the same way in the container as they do on a dev machine.
WORKDIR /app/backend

EXPOSE 8080

# Shell form (not exec/JSON-array form) so $PORT actually gets expanded -
# Render injects its own PORT env var dynamically (default 10000, host
# picks it), Fly.io doesn't set one so this falls back to 8080 matching
# fly.toml's internal_port, and a plain `docker run` locally gets 8080 too.
CMD python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
