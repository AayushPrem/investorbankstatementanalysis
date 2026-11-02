# Multi-stage build for the API and the worker (same image, different command).
# Build:  docker build -t bsaa .
FROM python:3.11-slim AS build
WORKDIR /src
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml README.md ./
COPY schema ./schema
COPY adapters ./adapters
COPY pipeline ./pipeline
COPY analysis ./analysis
COPY jurisdictions ./jurisdictions
COPY reports ./reports
COPY profiles ./profiles
COPY tools ./tools
COPY demo ./demo
COPY scripts ./scripts
COPY config ./config
COPY llm ./llm
COPY db ./db
COPY api ./api
COPY worker ./worker
COPY workbench_config.py ./
RUN python -m venv /venv && /venv/bin/pip install --upgrade pip \
 && /venv/bin/pip install ".[platform]"

FROM python:3.11-slim AS runtime
RUN useradd --create-home --uid 10001 bsaa
COPY --from=build /venv /venv
WORKDIR /app
# Source tree too: alembic.ini, migrations and workbench_config.py are read from the working dir.
COPY --chown=bsaa:bsaa . /app
ENV PATH="/venv/bin:$PATH" PYTHONUNBUFFERED=1 BSAA_ENV_FILE=""
USER bsaa
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
