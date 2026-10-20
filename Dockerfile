FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# The API runs as an unprivileged user. Its UID/GID match the host user (build args MAI_UID /
# MAI_GID in docker-compose.yml), so files it writes into the mounted inbox stay editable.
ARG UID=1000
ARG GID=1000

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY dashboard ./dashboard
COPY config ./config
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN groupadd --gid "$GID" mai \
 && useradd --uid "$UID" --gid "$GID" --no-create-home --shell /usr/sbin/nologin mai \
 && mkdir -p /app/models /app/data/documents /app/inbox \
 && chown -R mai:mai /app/data /app/inbox
USER mai

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
