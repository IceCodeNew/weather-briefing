# syntax=mirror.gcr.io/docker/dockerfile:1.25.0@sha256:0adf442eae370b6087e08edc7c50b552d80ddf261576f4ebd6421006b2461f12

FROM mirror.gcr.io/icecodexi/bash-toybox:0.8.14@sha256:c56a6ec48a565c1ba91964d69069c77aec46bfcb0fea07778620e1c63c2b8561 AS assets
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:02b579c054e3b6647ef07a01b319b50d87984cfc99637fdffb34dd92aa26bee3 AS py-runtime
# toybox + bash(ash) + catatonit
COPY --link --from=assets /usr/bin/ /usr/bin/
SHELL ["/usr/bin/bash", "-o", "pipefail", "-c"]
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.flatt.tech/simple/ \
    UV_DEFAULT_INDEX=https://pypi.flatt.tech/simple/


FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS distroless-uv
FROM py-runtime AS uv
COPY --link --from=distroless-uv /uv /uvx \
    /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

FROM uv AS build
WORKDIR /home/nonroot/app/
COPY --link --chown=65532:65532 pyproject.toml uv.lock README.md ./
RUN uv --no-progress sync --frozen --no-dev --no-install-project
COPY --link --chown=65532:65532 weather_briefing ./weather_briefing
RUN uv --no-progress sync --frozen --no-dev --no-editable


FROM py-runtime AS final
COPY --link --from=build --chown=65532:65532 \
    /home/nonroot/app/.venv/ /home/nonroot/app/.venv/
WORKDIR /home/nonroot/app/
ENV PATH="/home/nonroot/app/.venv/bin:${PATH}" \
    TZ=Asia/Shanghai

VOLUME ["/home/nonroot/app/state"]
ENTRYPOINT ["catatonit", "-g", "--", "/home/nonroot/app/.venv/bin/python3", "-m", "weather_briefing.cli"]
CMD ["daemon"]
