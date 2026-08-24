# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
COPY migrations ./migrations
RUN python -m pip install --no-cache-dir --no-deps . \
    && mkdir -p /app/data \
    && chown -R app:app /app/data

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "app", "check"]

ENTRYPOINT ["python", "-u", "-m", "app"]
CMD ["check"]
