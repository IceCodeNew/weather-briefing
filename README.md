# Weather Briefing

[English](README.md) | [简体中文](README_zh-Hans.md) | [日本語](README_ja.md)

Weather Briefing periodically aggregates weather, air quality, warnings, and optional information sources, then generates briefings with source citations via large language models.

## Core capabilities

- Delivers daily weather, air quality, warnings, and lifestyle advice.
- Continuously compares weather changes, alerting only when you may need to act.
- Persists history, unsent messages, and active warnings so nothing repeats or falls through the cracks.
- Supports multiple locations and output languages with independent state per location.
- Composes global and local weather services by region, falling back when a primary source fails.
- Optionally supplements private RSS or web content; every conclusion retains a verifiable source link.

## Prerequisites

Before deploying, you will need:

- An environment that can run the program long-term and persist runtime state.
- A delivery platform account and credentials. Telegram is currently built-in; you will need a Bot Token and Chat ID.
- A supported large language model account, model name, and credentials. See the [any-llm provider list](https://docs.mozilla.ai/any-llm/providers).
- At least one location of interest.
- A directory that can persist runtime state and geocoding results.

The default weather services require no API key. Users in mainland China who want QWeather will also need a Project ID, Credential ID, a dedicated API Host, and a Base64-encoded Ed25519 private key. See the [QWeather JWT documentation](https://dev.qweather.com/docs/configuration/authentication/#json-web-token) for authentication details.

The repository provides the following configuration templates:

- [`env.example`](env.example) &mdash; environment variables and their purposes.
- [`locations.example.json`](locations.example.json) &mdash; locations of interest.
- [`rss-sources.example.json`](rss-sources.example.json) &mdash; optional RSS sources.

## Using the published image

Docker is one deployment option. The examples below use a fixed-version image from Docker Hub. You can also run the project in other ways, as long as it can run persistently and preserve the configuration and state described above.

First, create a directory and copy the configuration templates:

```sh
export ROOT_DIR="${HOME}/weather-briefing"
mkdir -p "${ROOT_DIR}/state"
cp env.example "${ROOT_DIR}/.env"
cp locations.example.json "${ROOT_DIR}/locations.json"
```

Fill in `.env` and `locations.json`. Once configured, tighten file permissions and start the service:

```sh
sudo chgrp -R 65532 "${ROOT_DIR}"
chmod 750 "${ROOT_DIR}"
chmod 770 "${ROOT_DIR}/state"
chmod 640 "${ROOT_DIR}/.env" "${ROOT_DIR}"/*.json
WEATHER_BRIEFING_VERSION="2.2.0"
IMAGE="icecodexi/weather-briefing:${WEATHER_BRIEFING_VERSION}"
TZ="$(sed -n 's/^BRIEFING_TIMEZONE=//p' "${ROOT_DIR}/.env" | tail -n 1 | tr -d '\r')"
TZ="${TZ:-Asia/Shanghai}"

docker pull "${IMAGE}"
docker run -d \
  --name weather-briefing \
  --restart unless-stopped \
  --env "TZ=${TZ}" \
  --env-file "${ROOT_DIR}/.env" \
  --mount \
  "type=bind,src=${ROOT_DIR}/locations.json,dst=/home/nonroot/app/locations.json,readonly" \
  --mount \
  "type=bind,src=${ROOT_DIR}/state,dst=/home/nonroot/app/state" \
  "${IMAGE}" daemon
```

To upgrade, change the image version, remove the old container, and recreate it with the same configuration.

## Configuring locations

Every location must have a unique `id`. The `id` distinguishes locations; do not change it casually after configuration. Each location must also provide a name, a coordinate pair, or both:

- `name`, the location name;
- `latitude` and `longitude`, provided together.

When only a name is provided, the program will resolve and cache the coordinates. When only coordinates are provided, it will reverse-lookup the name. When both are provided, no geocoding call is made.

`language` controls the briefing language for that location. Supported values are `en` (default), `ja`, `zh-CN`, and `zh-TW`. For locations in Japan that require JMA forecasts, also provide the local six-digit `jma_office_code`.

The program selects default weather sources by region:

- Mainland China: QWeather first, Open-Meteo as fallback;
- Singapore: Open-Meteo full weather, supplemented with NEA two-hour forecasts;
- Japan: Open-Meteo full weather; when a JMA office code is configured, JMA forecasts are appended;
- Other regions: Open-Meteo.

You can also set `WEATHER_PROVIDERS` to replace the regional default order. Include `nea-sg` or `jma-jp` explicitly if you still want those regional supplements. Full-weather services should come before services that only provide partial information. This order only affects how the program fetches data.

When NEA or JMA content conflicts with Open-Meteo for the same time and region, the briefing prioritizes the latest data from the local official agency and retains conflicting sources for your verification.

### JMA office codes

See [`docs/jma-office-codes.md`](docs/jma-office-codes.md) for the forecast office codes covering all 47 prefectures and usage instructions.

## Configuring the model and publisher

At minimum, fill in the following in `.env`:

- `LLM_PROVIDER` and `LLM_MODEL`;
- the credentials required by your chosen model service;
- for Telegram delivery: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. These are not needed when testing with `PUBLISHER=stdout`.

When using Telegram for the first time, open the bot in Telegram and send `/start`. The bot can only send messages to a Chat ID after the user has initiated a conversation; otherwise the briefing will fail because the target session is unreachable.

Model calls are handled by any-llm. The credential variables needed by each service follow the [any-llm provider documentation](https://docs.mozilla.ai/any-llm/providers). The official image ships with the components required for DeepSeek, OpenAI, and OpenRouter.

RSS is optional. When enabled, fill in the private configuration with source names, URLs, and applicable locations.

When using the Docker example above, first prepare the file:

```sh
cp rss-sources.example.json "${ROOT_DIR}/rss-sources.json"
sudo chgrp 65532 "${ROOT_DIR}/rss-sources.json"
chmod 640 "${ROOT_DIR}/rss-sources.json"
```

Then add the following option to the `docker run` command, placing it before `"${IMAGE}" daemon`:

```sh
--mount \
  "type=bind,src=${ROOT_DIR}/rss-sources.json,dst=/home/nonroot/app/rss-sources.json,readonly"
```

## Running and troubleshooting

The persistent scheduler sends a daily forecast at 08:00 by default, and checks for weather changes from 09:00&ndash;23:00. You can adjust the timezone and schedule in `.env`.

The default timezone is `Asia/Shanghai`. For other regions, change `BRIEFING_TIMEZONE`; the startup command above reads it from `.env` and passes the same value to the container as `TZ`.

Run a one-off task:

```sh
# View the forecast for a future date
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run forecast --date 2026-07-23 --run-now
# Run an immediate briefing
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run briefing --run-now
```

Once verified with stdout, switch `.env` back to `PUBLISHER=telegram`, fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, and recreate the container.

The application writes operational logs to stderr. Normal logs do not record credentials, coordinates, message bodies, or private URLs.

To temporarily inspect rendered message text, first set `DEBUG=true` in `.env`, then recreate the container with the start command above. `docker restart` does not re-read `--env-file`.

After the new container is running, enable diagnostics for a limited time:

```sh
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text enable --for 15m
docker exec weather-briefing \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text disable
```

Diagnostic text may contain location and source content. Disable diagnostics immediately after troubleshooting and protect the logs.

For the scenarios the product addresses, see [`docs/requirements.md`](docs/requirements.md). For the current implementation, see [`docs/design.md`](docs/design.md). For technical tradeoffs that may appear questionable, see [`docs/notes.md`](docs/notes.md).

Weather and pollen data may originate from Open-Meteo and CAMS ENSEMBLE. Location queries may use OpenStreetMap Nominatim; data copyright belongs to its contributors.
