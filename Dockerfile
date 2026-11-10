FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 soup
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=builder --chown=soup:soup /app/.venv /app/.venv
COPY --chown=soup:soup src ./src
COPY --chown=soup:soup alembic.ini ./
COPY --chown=soup:soup migrations ./migrations
USER soup
EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM builder AS tools
RUN uv sync --frozen
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY tests ./tests
CMD ["pytest", "-q"]
