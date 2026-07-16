# Weather Briefing

有状态的天气信息简报生成器。它以天气 API 为基础，可选读取私密 RSS 补充信息，保留历史上下文和有效预警，通过可替换的 LLM provider 生成带来源引用的增量简报。

## 功能

- 默认 08:00 生成当日预报及穿衣、除湿、运动、口罩建议。
- 默认 09:00–23:00 每小时生成增量简报，不重复生活建议。
- 标题匹配配置规则的权威预报文章经 HTML 与页面噪声清洗后完整独立转发，并进入后续预报上下文。
- 日报通过可组合 provider 加入 API 天气预报、AQI、指数标准、PM2.5 浓度、生活指数及花粉过敏原信息，并用于穿衣、运动和口罩建议。
- 中国大陆天气默认使用 QWeather、Open-Meteo 的降级顺序，其他地区默认只使用 Open-Meteo；也可通过 `WEATHER_PROVIDERS` 显式指定主要来源和其他备用来源。
- 支持多个关注地点；只给地名时正向解析并缓存坐标与国家信息，只给坐标时反查地点名和行政信息，名称与坐标齐全时不发起定位请求。
- 完整地名无法解析时按可配置规则逐级降低查询精度；首次匹配会投递实际匹配地名和坐标，请用户确认并写回私密地点文件。
- 天气来源缺少空气质量时才使用可选 AQICN；两者都无法提供空气质量时给出明确配置错误。
- RSS 来源从可选 `rss-sources.json` 加载；没有该文件时小时任务仍由天气 API 正常运行。
- 普通无变化或仅有无变化的持续预警时不发送小时消息；未发布内容会保留并与后续更新累计总结，直到即将降雨、显著变化、预警动态或灾害动态值得提醒时合并发送。
- SQLite 持久化文章去重、历史简报、预警状态和任务/源健康状态。
- 时间点统一使用时区感知的 Pendulum 值；尽量避免不必要的时区转换，绝对禁止在代码中处理任何不含明确时区信息的时间。
- RSS 请求在 3–5 秒随机退避后重试；连续任务失败或源长期无更新时发送运维告警。
- 所有结论要求引用本轮输入中的来源链接。
- 所有日报与小时简报都由 LLM 生成平台无关的结构化结果，由 Telegram 等投递 provider 各自的模板渲染并输出。
- LLM、正文 cleaner、天气/空气质量 provider 与投递 provider 均通过接口解耦。

完整需求与设计见 [docs/requirements.md](docs/requirements.md) 和 [docs/design.md](docs/design.md)。

## 快速开始

支持 Python 3.11–3.14；CI 覆盖全部受支持版本，并以 Python 3.14 作为首选开发与测试版本。Distroless Debian 13 镜像使用其系统 Python 3.13。

```bash
uv lock --check
uv sync --frozen
cp env.example .env
cp locations.example.json locations.json
uv run --frozen weather-briefing run briefing
```

`env.example` 将必填项、条件必填项和选填项分别写在注释中，所有凭据和投递标识均为无效占位值。复制 `locations.example.json` 为被 Git 忽略的 `locations.json` 后可配置多个地点；示例使用北京市西城区中南海的公开坐标。每项必须有稳定 `id`，并在 `name` 与成对的 `latitude`、`longitude` 之间至少提供一项：只有名称时程序正向解析并支持降精度回退，只有坐标时通过 Nominatim 反查规范地点名和行政信息，两者都有时不发起定位请求。解析结果缓存到 `state/`。

`LLM_PROVIDER=deepseek` 使用 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` 和可选的 `DEEPSEEK_BASE_URL`；DeepSeek provider 已预置官方 Base URL。`LLM_PROVIDER=openai-compatible` 使用 `LLM_API_KEY`、`LLM_MODEL` 和 `LLM_BASE_URL`。两套配置互不回退。

应用将带时间、级别和 logger 名称的运行日志写入标准错误；INFO 日志记录每个地点的天气 provider 顺序和逻辑降级过程，并为天气、空气质量、地理编码、LLM、RSS、辅助上下文及 Telegram 的每个实际 HTTP 请求记录 provider、operation、方法、成功或失败、耗时和 HTTP 状态或异常类型，因此可从容器日志还原外部 API 调用历史。RSS 重试与 Telegram 分片分别按实际请求次数记录。常规 INFO 日志及仅由 `DEBUG=true` 启用的非敏感诊断不记录坐标、标题、正文、URL、token、chat ID、请求 endpoint 或异常消息；DEBUG 元数据覆盖从 RSS 清洗、权威预报转发和平台渲染到 Telegram 分片接受状态的链路。若仍需排查平台渲染或分片内容，可在不重启 daemon 的情况下临时记录完整渲染正文：

```bash
weather-briefing diagnostics rendered-text enable --for 15m
weather-briefing diagnostics rendered-text status
weather-briefing diagnostics rendered-text disable
```

容器部署通过同一运行实例执行，例如 `docker exec weather-briefing weather-briefing diagnostics rendered-text enable --for 15m`。该开关最长启用 24 小时并自动过期，状态保存在 `BRIEFING_STATE_PATH`。只有同时启用 `DEBUG` 和临时开关时才记录正文；日志包含简报、告警、权威预报以及 Telegram 分片的完整文本，可能暴露来源内容、来源 URL、坐标和其他位置上下文，排障后应立即关闭并妥善保护日志。token、chat ID 和请求 endpoint 不会写入这些诊断日志。

定位层把名称或坐标补全为统一地点信息。Open-Meteo 负责城市/邮编正向查询，空结果时由 OpenStreetMap Nominatim 解析详细地名；只有坐标时由 Nominatim 反向查询规范地点名、国家和行政区。名称与坐标齐全时不请求定位服务，并使用中国大陆服务范围四至宽松包围盒作快速可能性判断；所有查询结果都会持久缓存。省略 `WEATHER_PROVIDERS` 时，中国大陆地点使用 QWeather、Open-Meteo，其他地点只使用 Open-Meteo；显式配置时首项是主要来源，后续项依次作为备用。

RSS 为可选补充数据。需要使用时复制 `rss-sources.example.json` 为被 Git 忽略的 `rss-sources.json` 并填写真实来源；其中 `name` 使用公众号、微博账号或发布机构等会显示给用户的公开名称。不创建该文件即可只使用天气 API。

QWeather 使用 Ed25519 JWT 认证。将控制台中的项目 ID、JWT 凭据 ID、Base64 编码的私钥 PEM 和专属 API Host 分别写入 `QWEATHER_PROJECT_ID`、`QWEATHER_CREDENTIAL_ID`、`QWEATHER_PRIVATE_KEY` 与 `QWEATHER_API_HOST`。应用每次请求前解码私钥并签发短期 Token，不使用长期 API KEY。

Telegram 投递 provider 组合平台专用 HTML renderer 与 Bot API publisher，负责渲染粗体章节、预警和可点击来源链接并完成投递。日常简报限制为一条不超过 Telegram 4096 可见字符上限的消息；权威预报清洗正文单独完整发送。需要回放历史数据进行隔离测试时，可覆盖业务时间和状态数据库：

```bash
BRIEFING_STATE_PATH=state/replay.sqlite3 weather-briefing run forecast --at 2026-07-11T08:00:00+08:00
```

## 调度

项目提供单一 OCI 镜像，不需要 Docker Compose。构建并运行常驻调度器：

`run forecast` 生成当日预报，默认由 daemon 在 `BRIEFING_TIMEZONE` 的 08:00 调度，可用 `GREETING_HOUR` 和 `GREETING_MINUTE` 调整。`run briefing` 生成增量简报，daemon 默认在 09:00–23:00 的整点运行；`BRIEFING_CRON` 是 APScheduler 的小时字段表达式，例如 `9-23`、`8,12,16` 或 `*/2`。

已有 daemon 运行时，可从另一个进程执行 `weather-briefing run forecast --run-now` 或 `weather-briefing run briefing --run-now`。两种命令都不创建第二个 scheduler，忽略调度窗口，执行一次后退出；briefing 还会汇总并强制投递此前因不值得打扰而积压的信息。如果当天尚未成功投递 briefing，daemon 会在最后一个 `BRIEFING_CRON` 小时强制投递；Telegram 使用无声消息，避免在较晚时段打扰。

`run forecast --date YYYY-MM-DD` 可查看当地今天或指定未来日期（例如后天）的 forecast，也可以与 `--run-now` 组合。目标日期只影响要查询和总结的预报日期，实际运行时间、状态写入时间和历史窗口仍使用当前时间。`--at` 只在测试历史回放时覆盖实际运行时间。

### 使用发布镜像部署

下面的脚本从 Docker Hub 拉取固定版本的多架构镜像，并以非特权用户运行常驻调度器。运行前先在 `ROOT_DIR` 下准备根据 `env.example` 填写的 `.env` 和有效的 `locations.json`；路径和镜像名称可按实际环境调整。

```sh
#!/bin/sh
set -eu

CONTAINER_NAME="weather-briefing"
WEATHER_BRIEFING_IMAGE="icecodexi/${CONTAINER_NAME}"
WEATHER_BRIEFING_VERSION="1.0.0"
ROOT_DIR="${HOME}/${CONTAINER_NAME}"
CONTAINER_ROOT_DIR="/home/nonroot/app"

mkdir -p "${ROOT_DIR}/app/state"
test -s "${ROOT_DIR}/.env"
test -s "${ROOT_DIR}/locations.json"
test -s "${ROOT_DIR}/rss-sources.json" || printf '[]\n' >"${ROOT_DIR}/rss-sources.json"
chown -R 65532:65532 "${ROOT_DIR}"
chmod 600 "${ROOT_DIR}/.env" "${ROOT_DIR}/locations.json" "${ROOT_DIR}/rss-sources.json"

docker pull "${WEATHER_BRIEFING_IMAGE}:${WEATHER_BRIEFING_VERSION}"
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    --env-file "${ROOT_DIR}/.env" \
    --mount "type=bind,src=${ROOT_DIR}/locations.json,dst=${CONTAINER_ROOT_DIR}/locations.json,readonly" \
    --mount "type=bind,src=${ROOT_DIR}/rss-sources.json,dst=${CONTAINER_ROOT_DIR}/rss-sources.json,readonly" \
    --mount "type=bind,src=${ROOT_DIR}/app/state,dst=${CONTAINER_ROOT_DIR}/state" \
    "${WEATHER_BRIEFING_IMAGE}:${WEATHER_BRIEFING_VERSION}" \
    daemon
```

脚本需要有权执行 `chown` 和管理 Docker。RSS 未配置时使用空数组；配置文件以只读方式挂载，SQLite 状态保留在宿主机。升级时修改 `WEATHER_BRIEFING_VERSION` 并重新运行即可，重建容器期间会有短暂停机。

### 本地构建镜像

```bash
cp env.example .env
cp locations.example.json locations.json
docker buildx build --load -t weather-briefing .
docker volume create weather-briefing-state
docker run -d --name weather-briefing --restart unless-stopped \
  --env-file .env \
  --mount type=bind,src="$PWD/locations.json",dst=/home/nonroot/app/locations.json,readonly \
  --mount source=weather-briefing-state,target=/home/nonroot/app/state \
  weather-briefing
```

若启用 RSS，再把私密 `rss-sources.json` 以只读方式挂载到 `/home/nonroot/app/rss-sources.json`。配置文件不会进入镜像构建上下文。

也可在持久主机上使用 cron：

```cron
0 8 * * * cd /srv/weather-briefing && .venv/bin/weather-briefing run forecast
0 9-23 * * * cd /srv/weather-briefing && .venv/bin/weather-briefing run briefing
```

## 安全

- 不要把 `.env`、数据库、生成简报或真实配置提交到 Git。
- CI 使用 Gitleaks 扫描凭据，并运行自定义隐私哨兵测试。
- Telegram token 与 chat ID 只从运行环境读取；测试可选择 stdout publisher。

花粉数据由 [Open-Meteo](https://open-meteo.com/en/docs/air-quality-api) 基于 [CAMS ENSEMBLE](https://confluence.ecmwf.int/spaces/CKB/pages/202173092/CAMS+Regional+European+air+quality+analysis+and+forecast+data+documentation) 提供。地点解析使用 [Open-Meteo Geocoding](https://open-meteo.com/en/docs/geocoding-api)；详细地名备用数据 © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)，通过 Nominatim 获取。公共 Nominatim 仅在本地缓存未命中时调用，并遵守其使用政策。
