FROM python:3.14-slim AS builder

    RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libpq-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
    RUN pip install --no-cache-dir --upgrade pyopenssl

    # T-CYK-1009 CSO N5: uv by version and digest, the same uv the fork CI runs.
    COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /uvx /bin/

    WORKDIR /root_project

    # Install dependencies first (cached unless lock/pyproject change)
    COPY uv.lock pyproject.toml ./
    ENV VIRTUAL_ENV=/opt/venv
    ENV UV_PROJECT_ENVIRONMENT=/opt/venv
    RUN uv venv $VIRTUAL_ENV
    ENV PATH="$VIRTUAL_ENV/bin:$PATH"
    RUN uv sync --locked --no-dev --no-install-project

    # Copy source and install the project itself (fast, deps already cached)
    COPY . .
    RUN uv sync --locked --no-dev --no-editable
    # T-CYK-1009 CSO N7: the image runs the app only; tests and their hooks stay
    # out. Removed here, not in .dockerignore: a BuildKit git context (how
    # BH.Loop builds this image) does not apply .dockerignore.
    RUN rm -rf tests

FROM python:3.14-slim

    RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

    WORKDIR /root_project

    COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /uvx /bin/

    COPY --from=builder /opt/venv /opt/venv
    COPY --from=builder /root_project /root_project
    ENV VIRTUAL_ENV=/opt/venv
    ENV PATH="/opt/venv/bin:$PATH"
    ENV UV_PROJECT_ENVIRONMENT=/opt/venv
    ENV UV_NO_SYNC=true

    # Commit SHA for Sentry release tracking. Optional: defaults to empty when
    # not passed (e.g. local builds), in which case the app falls back to the
    # plain package version.
    ARG GIT_SHA=""
    ENV GIT_SHA=$GIT_SHA

    EXPOSE 8000
