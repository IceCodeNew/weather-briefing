# Weather Briefing

[English](README.md) | [简体中文](README_zh-Hans.md) | [日本語](README_ja.md)

Weather Briefing 定时汇总天气、空气质量、预警和可选信息源，再通过大语言模型生成带来源链接的简报。

## 核心能力

- 每天发送天气、空气质量、预警和生活建议。
- 在配置的监测时段内比较天气变化，只在用户可能需要采取行动时提醒。
- 保存历史、未发送信息和有效预警，避免重复或遗漏重要变化。
- 支持多个地点和多种输出语言，各地点的状态互不影响。
- 按地区组合全球与当地天气服务，并在主要来源失败时降级。
- 可以补充私密 RSS 内容，每条结论都保留可核验的来源链接。

## 部署前准备

部署前需要准备：

- 一个能长期运行程序并保存运行状态的环境；
- 一个投递平台的账号和凭据。当前内置 Telegram，需要 Bot Token 和 Chat ID；
- 一个受支持的大语言模型账号、模型名称和凭据。可查阅 [any-llm provider 列表](https://docs.mozilla.ai/any-llm/providers)；
- 至少一个关注地点；
- 一个可持久保存运行状态和定位结果的目录。

默认天气服务不需要密钥。中国大陆用户如需使用和风天气，还要准备项目 ID、凭据 ID、专属 API Host 和 Base64 编码的 Ed25519 私钥。认证方式见[和风天气 JWT 文档](https://dev.qweather.com/docs/configuration/authentication/#json-web-token)。

仓库提供以下配置模板：

- [`env.example`](env.example)：环境变量及用途；
- [`locations.example.json`](locations.example.json)：关注地点；
- [`rss-sources.example.json`](rss-sources.example.json)：可选 RSS 来源。

## 使用发布镜像

Docker 是推荐的部署方式。下面的示例使用 Docker Hub 上的固定版本镜像；也可以用其他方式运行项目，只要它能长期运行程序并保存上述配置和状态。

先准备宿主目录和配置文件。默认放在当前用户的主目录下；如需放在其他位置，只需修改 `ROOT_DIR`。

```sh
CONTAINER_NAME="weather-briefing"
ROOT_DIR="${HOME}/${CONTAINER_NAME}"
CONTAINER_ROOT_DIR="/home/nonroot/app"

mkdir -p "${ROOT_DIR}/state"
touch "${ROOT_DIR}/.env" "${ROOT_DIR}/locations.json"
```

参考仓库中的模板填写 `.env` 和 `locations.json`。`locations.json` 必须是有效的 JSON 地点数组，不能保持为空文件。

配置完成后收紧文件权限并启动服务。以下命令将 GID `65532` 视为具有写权限的受信任容器服务组；不要把无关的宿主用户加入该组。容器会以同一名称替换旧实例，绑定挂载的配置和状态不会被删除。

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

升级时修改 `WEATHER_BRIEFING_VERSION`，再重新执行拉取和启动命令。旧容器会被替换，`${ROOT_DIR}` 下的配置、定位缓存和运行状态会继续保留。

## 配置地点

每个地点都必须填写唯一的 `id`。`id` 用于区分地点，配置后不要随意修改。每个地点还要填写名称、成对的经纬度坐标或同时填写两者：

- `name`，表示地点名称；
- 成对的 `latitude` 和 `longitude`，表示经纬度坐标。

只填地点名称时，程序会查询坐标并回写到 `locations.json`；只填经纬度坐标时，程序会反查并回写便于阅读的地点名称。已有字段不会被覆盖。降低精度得到的匹配仍需用户确认，不会自动写入。两项都填写时，不会调用定位服务。

`language` 控制该地点简报的语言，接受基本的 BCP 47 格式标签，默认是 `en`。程序会规范化标签（例如将 `ja-jp` 转为 `ja-JP`）并传给语言模型。简报标签提供 `en`、`ja`、`zh-CN` 和 `zh-TW` 本地化；变体标签使用最接近的本地化，未支持的主要语言则回退到英文标签。日本地点如需 JMA 预报，还要填写当地六位 `jma_office_code`。

程序会根据地点选择默认天气来源：

- 中国大陆：和风天气优先，Open-Meteo 备用；
- 新加坡：先读取 Open-Meteo 完整天气，再追加 NEA 两小时预报；
- 日本：先读取 Open-Meteo 完整天气；配置 `jma_office_code` 后，再追加 JMA 预报；
- 其他地区：Open-Meteo。

也可以用 `WEATHER_PROVIDERS` 替换地区默认调用顺序。如需继续使用当地补充服务，必须显式加入 `nea-sg` 或 `jma-jp`。完整天气服务应排在只提供局部信息的服务之前。这个顺序只决定程序如何取得数据。

NEA 或 JMA 与 Open-Meteo 在同一时间和地区发生内容冲突时，简报优先采用当地官方机构的最新资料，并保留冲突来源供用户核验。

### JMA 办公室编码

覆盖日本 47 个都道府县的预报区域办公室编码及用法见 [`docs/jma-office-codes.md`](docs/jma-office-codes.md)。

## 配置模型与投递

`.env` 中至少要填写：

- `LLM_PROVIDER` 和 `LLM_MODEL`；
- 对应模型服务要求的凭据；
- 当前 Telegram 投递所需的 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。用 `PUBLISHER=stdout` 测试时不需要填写。

使用 Telegram 私聊投递时，请在首次投递前打开该机器人并向它发送 `/start`。机器人只有在用户主动启动私聊会话后，才能向对应的私聊 Chat ID 发送消息。投递到群组时无需执行这一步，但必须先将机器人加入群组并授予发送消息的权限。

模型调用由 any-llm 处理。不同服务需要的凭据变量以 [any-llm provider 文档](https://docs.mozilla.ai/any-llm/providers) 为准。官方镜像预装 DeepSeek、OpenAI 和 OpenRouter 所需组件。

RSS 是可选功能，默认不会挂载。需要启用时，参考 [`rss-sources.example.json`](rss-sources.example.json) 创建 `rss-sources.json`，填写来源名称、URL 和适用地点，再把以下选项添加到 `docker run` 命令，并放在镜像名称之前：

```sh
--mount \
  "type=bind,src=${ROOT_DIR}/rss-sources.json,dst=${CONTAINER_ROOT_DIR}/rss-sources.json,readonly"
```

添加后重新创建容器。

## 运行与排障

常驻调度器默认每天 08:00 发送预报，并在 09:00&ndash;23:00 检查天气变化。具体时区和时间可在 `.env` 中调整。

默认时区为 `Asia/Shanghai`。其他地区只需修改 `BRIEFING_TIMEZONE`；上述启动命令会从 `.env` 读取该值，并以 `TZ` 传入容器。

手动执行一次任务：

```sh
# 查看未来某天的预报
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run forecast --date 2026-07-23 --run-now
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  run briefing --run-now
```

用 stdout 验证通过后，将 `.env` 改回 `PUBLISHER=telegram`，填写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，重新创建容器即可。

应用把运行日志写到标准错误输出。普通日志不记录凭据、坐标、正文或私密 URL。

如需临时查看完整渲染正文，先在 `.env` 中设置 `DEBUG=true`，再按上面的启动命令重新创建容器。`docker restart` 不会重新读取 `--env-file`。

新容器启动后，限时打开诊断：

```sh
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text enable --for 15m
docker exec "${CONTAINER_NAME:-weather-briefing}" \
  /home/nonroot/app/.venv/bin/weather-briefing \
  diagnostics rendered-text disable
```

完整正文日志可能包含位置和来源内容。排障后应立即关闭并妥善保护日志。

产品要解决的场景见 [`docs/requirements.md`](docs/requirements.md)。当前实现见 [`docs/design.md`](docs/design.md)。不易理解的技术取舍见 [`docs/notes.md`](docs/notes.md)。

天气和花粉数据可能来自 Open-Meteo 与 CAMS ENSEMBLE。地点查询可能使用 OpenStreetMap Nominatim，数据版权归其贡献者所有。
