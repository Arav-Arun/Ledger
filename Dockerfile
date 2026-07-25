# syntax=docker/dockerfile:1
# Single image that serves both the API and the built UI. Two stages: build the React
# bundle with Node, then run it behind the FastAPI server (main.py mounts UI_DIST as static
# files when it exists).

# ---- stage 1: build the React UI ----
FROM node:20-slim AS ui
WORKDIR /ui
COPY ui/package*.json ./
RUN npm ci
COPY ui/ .
RUN npm run build                      # -> /ui/dist

# ---- stage 2: the FastAPI server ----
FROM python:3.12-slim AS server
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app/server

COPY server/requirements.txt .
RUN pip install -r requirements.txt
COPY server/ .

# Ship the built UI so one container serves everything. main.py's default UI_DIST resolves
# to /app/ui/dist, but we set it explicitly for clarity.
COPY --from=ui /ui/dist /app/ui/dist
ENV UI_DIST=/app/ui/dist

# Drop privileges.
RUN useradd --create-home --uid 10001 ledger
USER ledger

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
