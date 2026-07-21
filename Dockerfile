# syntax=mirror.gcr.io/docker/dockerfile:1.25.0@sha256:0adf442eae370b6087e08edc7c50b552d80ddf261576f4ebd6421006b2461f12

FROM mirror.gcr.io/icecodexi/bash-toybox:0.8.14@sha256:8dfe2229d2855e09bce8304cdcc84be90cd2026fe78d30e03efd328bd0bc7b6f AS assets
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:0e52dfee02b1aba142e77b004f6ea11210b79456b51f10d70e9bd631cbc21d98 AS py-runtime
# toybox + bash(ash) + catatonit
COPY --link --from=assets /usr/bin/ /usr/bin/
SHELL ["/usr/bin/bash", "-o", "pipefail", "-c"]
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.flatt.tech/simple/ \
    UV_DEFAULT_INDEX=https://pypi.flatt.tech/simple/


FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS distroless-uv
FROM py-runtime AS uv
COPY --link --from=distroless-uv /uv /uvx \
    /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

FROM uv AS build
WORKDIR /home/nonroot/app/
COPY --link --chown=65532:65532 pyproject.toml uv.lock README.md ./
RUN uv --no-progress sync --frozen --no-dev --group docker --no-install-project
COPY --link --chown=65532:65532 weather_briefing ./weather_briefing
RUN uv --no-progress sync --frozen --no-dev --group docker --no-editable


FROM py-runtime AS final
COPY --link --from=build --chown=65532:65532 \
    /home/nonroot/app/.venv/ /home/nonroot/app/.venv/
WORKDIR /home/nonroot/app/
ENV PATH="/home/nonroot/app/.venv/bin:${PATH}" \
    TZ=Asia/Shanghai

VOLUME ["/home/nonroot/app/state"]
ENTRYPOINT ["catatonit", "-g", "--", "/home/nonroot/app/.venv/bin/weather-briefing"]
CMD ["daemon"]
