FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 ams \
    && mkdir -p /data/evidence \
    && chown -R ams:ams /app /data/evidence

USER ams

EXPOSE 8000

CMD ["uvicorn", "ams.interface.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
