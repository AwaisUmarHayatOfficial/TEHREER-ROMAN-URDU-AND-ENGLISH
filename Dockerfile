FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend backend
COPY frontend frontend
COPY tehreer.sql tehreer.sql

RUN useradd -m app && mkdir -p /data/jobs && chown -R app /data /app
USER app
ENV JOBS_DIR=/data/jobs

EXPOSE 6550
# Single worker only: job state lives in memory
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "6550", "--workers", "1", "--proxy-headers"]
