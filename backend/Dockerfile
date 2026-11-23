FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY guarded ./guarded
COPY evals ./evals
RUN uv sync --frozen --no-dev

RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "guarded.api:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
