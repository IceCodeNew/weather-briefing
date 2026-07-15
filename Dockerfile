# syntax=mirror.gcr.io/docker/dockerfile:1.25.0@sha256:0adf442eae370b6087e08edc7c50b552d80ddf261576f4ebd6421006b2461f12

FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS distroless-uv
FROM mirror.gcr.io/icecodexi/python:debian-nonroot@sha256:c65280376649d99af2536eadf24d7966e89900662bb94f089ba0cc9e064a8ade AS uv
COPY --link --from=distroless-uv /uv /uvx \
    /usr/local/bin/
ENV PATH="/home/nonroot/.local/bin:${PATH}" \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

FROM uv AS build
WORKDIR /home/nonroot/weather-briefing
COPY --link --chown=65532:65532 pyproject.toml uv.lock README.md ./
RUN uv --no-progress sync --frozen --no-dev --no-install-project
COPY --link --chown=65532:65532 weather_briefing ./weather_briefing
RUN uv --no-progress sync --frozen --no-dev --no-editable


FROM mirror.gcr.io/icecodexi/bash-toybox:0.8.14@sha256:c56a6ec48a565c1ba91964d69069c77aec46bfcb0fea07778620e1c63c2b8561 AS assets
FROM gcr.io/distroless/python3-debian13:latest@sha256:02b579c054e3b6647ef07a01b319b50d87984cfc99637fdffb34dd92aa26bee3
# toybox + bash(ash) + catatonit
COPY --link --from=assets /usr/bin/ /usr/bin/
COPY --link --from=build --chown=65532:65532 \
    /home/nonroot/weather-briefing/.venv/ /app/.venv/

SHELL ["/usr/bin/bash", "-o", "pipefail", "-c"]
# hadolint ignore=SC2114
RUN rm -rf /bin/ && ln -sf /usr/bin /bin \
    && mkdir /app/state && chown 65532:65532 /app/state

USER nonroot:nonroot
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

VOLUME ["/app/state"]
ENTRYPOINT ["catatonit", "-g", "--", "/app/.venv/bin/python", "-m", "weather_briefing.cli"]
CMD ["daemon"]
