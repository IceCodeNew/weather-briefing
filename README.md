# Weather Briefing

Weather Briefing 定时汇总天气、空气质量、预警和可选信息源，再通过大语言模型生成带来源链接的 Telegram 简报。

## 核心能力

- 每天发送天气、空气质量、预警和生活建议。
- 持续比较天气变化，只在用户可能需要采取行动时提醒。
- 保存历史、未发送信息和有效预警，避免重复或遗漏重要变化。
- 支持多个地点和多种输出语言，各地点的状态互不影响。
- 按地区组合全球与当地天气服务，并在主要来源失败时降级。
- 可以补充私密 RSS 或网页内容，每条结论都保留可核验的来源链接。

## 部署前准备

部署前需要准备：

- 一个能长期运行程序并保存运行状态的环境；
- 一个投递平台的账号和凭据；当前内置 Telegram，需要 Bot Token 和 Chat ID；
- 一个受支持的大语言模型账号、模型名称和凭据。可查阅 [any-llm provider 列表](https://docs.mozilla.ai/any-llm/providers)；
- 至少一个关注地点；
- 一个可持久保存运行状态和定位结果的目录。

默认天气服务不需要密钥。中国大陆用户如需使用和风天气，还要准备项目 ID、凭据 ID、专属 API Host 和 Base64 编码的 Ed25519 私钥。认证方式见[和风天气 JWT 文档](https://dev.qweather.com/docs/configuration/authentication/#json-web-token)。

仓库提供以下配置模板：

- [`env.example`](env.example)：环境变量及用途；
- [`locations.example.json`](locations.example.json)：关注地点；
- [`rss-sources.example.json`](rss-sources.example.json)：可选 RSS 来源。

## 使用发布镜像

Docker 只是部署选项。下面的示例使用 Docker Hub 上的固定版本镜像；
也可以用其他方式运行项目，只要它能长期运行程序并保存上述配置和状态。

先创建目录，并复制配置模板：

```sh
export ROOT_DIR="${HOME}/weather-briefing"
mkdir -p "${ROOT_DIR}/state"
cp env.example "${ROOT_DIR}/.env"
cp locations.example.json "${ROOT_DIR}/locations.json"
```

填写 `.env` 和 `locations.json`。配置完成后收紧文件权限并启动服务：

```sh
sudo chown -R 65532:65532 "${ROOT_DIR}"
chmod 600 "${ROOT_DIR}/.env" "${ROOT_DIR}"/*.json
WEATHER_BRIEFING_VERSION="1.2.1"
IMAGE="icecodexi/weather-briefing:${WEATHER_BRIEFING_VERSION}"
docker pull "${IMAGE}"
docker run -d \
  --name weather-briefing \
  --restart unless-stopped \
  --env-file "${ROOT_DIR}/.env" \
  --mount \
  "type=bind,src=${ROOT_DIR}/locations.json,dst=/home/nonroot/app/locations.json,readonly" \
  --mount \
  "type=bind,src=${ROOT_DIR}/state,dst=/home/nonroot/app/state" \
  "${IMAGE}" daemon
```

升级时修改镜像版本，删除旧容器，再用相同配置重新创建。

## 配置地点

每个地点都必须填写唯一的 `id`。`id` 用于区分地点，配置后不要随意修改。每个地点还要至少提供以下两项之一：

- `name`，表示地点名称；
- 成对的 `latitude` 和 `longitude`，表示经纬度坐标；
- 地点名称和经纬度坐标。

只填地点名称时，程序会查询并缓存坐标；只填经纬度坐标时，程序会反查地点名称。两项都填写时，不会调用定位服务。

`language` 控制该地点简报的语言，默认是英文 `en`。中国大陆示例显式填写 `zh-CN`，表示简体中文（中国）。日本地点如需 JMA 预报，还要填写当地六位 `jma_office_code`。

程序会根据地点选择默认天气来源：

- 中国大陆：和风天气优先，Open-Meteo 备用；
- 新加坡：先读取 Open-Meteo 完整天气，再追加 NEA 两小时预报；
- 日本：先读取 Open-Meteo 完整天气；配置 JMA office code 后，再追加 JMA 预报；
- 其他地区：Open-Meteo。

也可以用 `WEATHER_PROVIDERS` 明确指定调用顺序。完整天气服务应排在只提供局部信息的服务之前。这个顺序只决定程序如何取得数据。

NEA 或 JMA 与 Open-Meteo 在同一时间和地区发生内容冲突时，简报优先采用当地官方机构的最新资料，并保留冲突来源供用户核验。

## 配置模型与投递

`.env` 中至少要填写：

- `LLM_PROVIDER` 和 `LLM_MODEL`；
- 对应模型服务要求的凭据；
- 当前 Telegram 投递所需的 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。

模型调用由 any-llm 处理。不同服务需要的凭据变量以 [any-llm provider 文档](https://docs.mozilla.ai/any-llm/providers) 为准。
官方镜像预装 DeepSeek、OpenAI 和 OpenRouter 所需组件。

RSS 是可选功能。启用时，请在私密配置中填写来源名称、URL 和适用地点。
使用上面的 Docker 示例时，先准备文件：

```sh
cp rss-sources.example.json "${ROOT_DIR}/rss-sources.json"
```

再把以下选项添加到 `docker run` 命令，并放在 `"${IMAGE}" daemon` 之前：

```sh
--mount \
  "type=bind,src=${ROOT_DIR}/rss-sources.json,dst=/home/nonroot/app/rss-sources.json,readonly"
```

## 运行与排障

常驻调度器默认每天 08:00 发送预报，并在 09:00–23:00 检查天气变化。具体时区和时间可在 `.env` 中调整。

手动执行一次任务：

```sh
# 查看未来某天的预报
docker exec weather-briefing \
  weather-briefing run forecast --date 2026-07-23 --run-now
docker exec weather-briefing weather-briefing run briefing --run-now
```

应用把运行日志写到标准错误。普通日志不记录凭据、坐标、正文或私密 URL。

如需临时查看完整渲染正文，先在 `.env` 中设置 `DEBUG=true`，再按上面的启动命令重新创建容器。`docker restart` 不会重新读取 `--env-file`。

新容器启动后，限时打开诊断：

```sh
docker exec weather-briefing \
  weather-briefing diagnostics rendered-text enable --for 15m
docker exec weather-briefing \
  weather-briefing diagnostics rendered-text disable
```

完整正文日志可能包含位置和来源内容。排障后应立即关闭并妥善保护日志。

产品要解决的场景见 [`docs/requirements.md`](docs/requirements.md)。当前实现见 [`docs/design.md`](docs/design.md)。不易理解的技术取舍见 [`docs/notes.md`](docs/notes.md)。

天气和花粉数据可能来自 Open-Meteo 与 CAMS ENSEMBLE。地点查询可能使用 OpenStreetMap Nominatim，数据版权归其贡献者所有。
