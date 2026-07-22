# Weather Briefing

[English](README.md) | [简体中文](README_zh-Hans.md) | [日本語](README_ja.md)

Weather Briefing periodically gathers weather, air quality, warnings, and optional private RSS content, then uses a large language model to produce a briefing with links to its sources.

## Core capabilities

- Delivers daily weather, air quality, warnings, and practical lifestyle advice.
- Tracks weather changes during configured monitoring periods and alerts you only when action may be needed.
- Persists history, unsent messages, and active warnings so important changes are neither repeated nor missed.
- Supports multiple locations and output languages, with independent state for each location.
- Combines global and regional weather services and falls back when a primary source fails.
- Can incorporate private RSS content while retaining a verifiable source link for every conclusion.

## Prerequisites

Before deploying, you will need:

- An environment that can keep the program running and persist its runtime state.
- An account and credentials for a delivery platform. Telegram is currently built in and requires a Bot Token and Chat ID.
- An account, model name, and credentials for a supported large language model. See the [any-llm provider list](https://docs.mozilla.ai/any-llm/providers).
- At least one location of interest.
- A directory that can persist runtime state and geocoding results.

The default weather services require no API key. Users in mainland China who want QWeather will also need a Project ID, Credential ID, a dedicated API Host, and a Base64-encoded Ed25519 private key. See the [QWeather JWT documentation](https://dev.qweather.com/docs/configuration/authentication/#json-web-token) for authentication details.

The repository provides the following configuration templates:

- [`env.example`](env.example) &mdash; environment variables and their purposes.
- [`locations.example.json`](locations.example.json) &mdash; locations of interest.
- [`rss-sources.example.json`](rss-sources.example.json) &mdash; optional RSS sources.

## Using the published image

Docker is the recommended deployment method. The examples below use a fixed-version image from Docker Hub. You can also run the project in other ways, as long as it can run persistently and preserve the configuration and state described above.

First, prepare the host directory and configuration files. The default location is under the current user's home directory; change `ROOT_DIR` if you keep application data elsewhere.

```sh
CONTAINER_NAME="weather-briefing"
ROOT_DIR="${HOME}/${CONTAINER_NAME}"
CONTAINER_ROOT_DIR="/home/nonroot/app"

mkdir -p "${ROOT_DIR}/state"
touch "${ROOT_DIR}/.env" "${ROOT_DIR}/locations.json"
```

Use the repository templates to fill in `.env` and `locations.json`. The locations file must contain a valid JSON array and cannot remain empty.

Once configured, tighten file permissions and start the service. The commands below treat GID `65532` as a trusted container service group with write access; do not assign unrelated host users to that group. The container is replaced in place, while bind-mounted configuration and state remain intact.

```sh
sudo chgrp -R 65532 "${ROOT_DIR}"
find "${ROOT_DIR}" -type d -exec chmod 770 {} +
find "${ROOT_DIR}" -type f -exec chmod 660 {} +

WEATHER_BRIEFING_IMAGE="icecodexi/${CONTAINER_NAME}"
WEATHER_BRIEFING_VERSION="2.3.0"
TZ="$(sed -n 's/^BRIEFING_TIMEZONE=//p' "${ROOT_DIR}/.env" | tail -n 1 | tr -d '\n\r')"
docker pull "${WEATHER_BRIEFING_IMAGE}:${WEATHER_BRIEFING_VERSION}"

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  --env "TZ=${TZ:-Asia/Shanghai}" \
  --env-file "${ROOT_DIR}/.env" \
  --mount \
  "type=bind,src=${ROOT_DIR}/locations.json,dst=${CONTAINER_ROOT_DIR}/locations.json" \
  --mount \
  "type=bind,src=${ROOT_DIR}/state,dst=${CONTAINER_ROOT_DIR}/state" \
  "${WEATHER_BRIEFING_IMAGE}:${WEATHER_BRIEFING_VERSION}" \
  daemon
```

To upgrade, change `WEATHER_BRIEFING_VERSION`, then run the pull and startup commands again. The old container is replaced, while configuration, geocoding cache, and runtime state under `${ROOT_DIR}` are preserved.

## Configuring locations

Every location must have a unique, stable `id`. It separates one location's state from another, so do not change it casually after configuration. Each location must also provide a name, a coordinate pair, or both:

- `name`, the location name;
- `latitude` and `longitude`, provided together.

With only a name, the program resolves the coordinates and writes them back to `locations.json`. With only coordinates, it performs a reverse lookup and writes back the readable name. Existing fields are never overwritten. Reduced-precision matches still require confirmation and are not written automatically. When both are present, no geocoding service is called.

`language` controls the briefing language for that location. It accepts a basic BCP 47 language tag and defaults to `en`. Tags are normalized (`ja-jp` becomes `ja-JP`) before being passed to the language model. Briefing labels are available in `en`, `ja`, `zh-CN`, and `zh-TW`; variants use the closest available localization, while unsupported primary languages fall back to English labels. For a location in Japan that needs JMA forecasts, also provide its six-digit `jma_office_code`.

The program selects default weather sources by region:

- Mainland China: QWeather first, Open-Meteo as fallback;
- Singapore: complete Open-Meteo weather data, supplemented with NEA two-hour forecasts;
- Japan: complete Open-Meteo weather data, with JMA forecasts added when an office code is configured;
- Other regions: Open-Meteo.

You can set `WEATHER_PROVIDERS` to override the regional default order. Include `nea-sg` or `jma-jp` explicitly if you still want those regional supplements. Providers that return complete weather data should come before providers that return only partial information. This order affects only how the program fetches data.

When NEA or JMA content conflicts with Open-Meteo for the same time and region, the briefing prioritizes the latest data from the local official agency and retains conflicting sources for your verification.

### JMA office codes

See [`docs/jma-office-codes.md`](docs/jma-office-codes.md) for the forecast office codes covering all 47 prefectures and usage instructions.

## Configuring the model and publisher

At minimum, configure the following in `.env`:

- `LLM_PROVIDER` and `LLM_MODEL`;
- the credentials required by your chosen model service;
- for Telegram delivery: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. These are not needed when testing with `PUBLISHER=stdout`.

For private-chat delivery, open the bot in Telegram and send `/start` before the first briefing. A bot can send messages to a private Chat ID only after the user has initiated the conversation. For group delivery, add the bot to the group and grant it permission to send messages.

Model calls are handled by any-llm. The credential variables needed by each service follow the [any-llm provider documentation](https://docs.mozilla.ai/any-llm/providers). The official image ships with the components required for DeepSeek, OpenAI, and OpenRouter.

RSS is optional and is not mounted by default. To enable RSS, create `rss-sources.json` based on [`rss-sources.example.json`](rss-sources.example.json), then add source names, URLs, and applicable locations. Add the following option to the `docker run` command before the image name:

```sh
--mount \
  "type=bind,src=${ROOT_DIR}/rss-sources.json,dst=${CONTAINER_ROOT_DIR}/rss-sources.json,readonly"
```

Recreate the container after adding the mount.

## Running and troubleshooting

By default, the persistent scheduler sends a daily forecast at 08:00 and checks for weather changes from 09:00&ndash;23:00. Both the timezone and schedule can be adjusted in `.env`.

The default timezone is `Asia/Shanghai`. For other regions, change `BRIEFING_TIMEZONE`; the startup command above reads it from `.env` and passes the same value to the container as `TZ`.

Run a task immediately:

```sh
# View the forecast for a future date
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run forecast --date 2026-07-23 --run-now
# Run an immediate briefing
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run briefing --run-now
```

Once verified with stdout, switch `.env` back to `PUBLISHER=telegram`, fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, and recreate the container.

The application writes operational logs to standard error. Normal logs do not contain credentials, coordinates, message bodies, or private URLs.

To temporarily inspect rendered message text, first set `DEBUG=true` in `.env`, then recreate the container with the start command above. `docker restart` does not re-read `--env-file`.

After the new container is running, enable diagnostics for a limited time:

```sh
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text enable --for 15m
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text disable
```

Diagnostic text may contain location details and source content. Disable diagnostics as soon as troubleshooting is complete, and protect the resulting logs.

For the scenarios the product addresses, see [`docs/requirements.md`](docs/requirements.md). For the current implementation, see [`docs/design.md`](docs/design.md). For technical tradeoffs that may appear questionable, see [`docs/notes.md`](docs/notes.md).

Weather and pollen data may originate from Open-Meteo and CAMS ENSEMBLE. Location queries may use OpenStreetMap Nominatim; data copyright belongs to its contributors.
