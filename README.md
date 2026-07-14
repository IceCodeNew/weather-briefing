# Weather Briefing

有状态的天气信息简报生成器。它以天气 API 为基础，可选读取私密 RSS 补充信息，保留历史上下文和有效预警，通过可替换的 LLM provider 生成带来源引用的增量简报。

## 功能

- 默认 08:00 生成当日预报及穿衣、除湿、运动、口罩建议。
- 默认 09:00–23:00 每小时生成增量简报，不重复生活建议。
- 标题匹配配置规则的权威预报文章经 HTML 与页面噪声清洗后完整独立转发，并进入后续预报上下文。
- 日报通过可组合 provider 加入 API 天气预报、AQI、指数标准、PM2.5 原始浓度及生活指数，并用于穿衣、运动和口罩建议。
- 中国大陆天气默认使用 QWeather、Open-Meteo 的降级顺序，其他地区默认只使用 Open-Meteo；也可通过 `WEATHER_PROVIDERS` 显式指定主要来源和其他备用来源。
- 支持多个关注地点；只给地名时通过 Open-Meteo Geocoding 解析并缓存坐标与国家信息，已有坐标时不发起地理编码请求。
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
uv run --frozen weather-briefing run hourly
```

`env.example` 将必填项、条件必填项和选填项分别写在注释中，所有凭据和投递标识均为无效占位值。复制 `locations.example.json` 为被 Git 忽略的 `locations.json` 后可配置多个地点；示例使用北京市西城区中南海的公开坐标。每项必须有稳定 `id` 和 `name`，`latitude` 与 `longitude` 可同时删除，此时程序用 Open-Meteo Geocoding 解析并把结果缓存到 `state/`。

`LLM_PROVIDER=deepseek` 使用 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` 和可选的 `DEEPSEEK_BASE_URL`；DeepSeek provider 已预置官方 Base URL。`LLM_PROVIDER=openai-compatible` 使用 `LLM_API_KEY`、`LLM_MODEL` 和 `LLM_BASE_URL`。两套配置互不回退。

应用将带时间、级别和 logger 名称的运行日志写入标准错误；INFO 日志记录每个地点的天气 provider 顺序，以及每次天气 API 尝试、成功、失败、耗时、实际来源和安全的失败原因，因此发生自动降级时可从容器日志还原调用历史。设置 `DEBUG=true` 可输出 RSS 获取、LLM 重试，以及从 RSS 清洗、权威预报转发和平台渲染到 Telegram 分片接受状态的非敏感诊断信息。该链路不记录坐标、标题、正文、URL、token、chat ID 或请求 endpoint。若仍需排查平台渲染或分片内容，可在不重启 daemon 的情况下临时记录完整渲染正文：

```bash
weather-briefing diagnostics rendered-text enable --for 15m
weather-briefing diagnostics rendered-text status
weather-briefing diagnostics rendered-text disable
```

容器部署通过同一运行实例执行，例如 `docker exec weather-briefing weather-briefing diagnostics rendered-text enable --for 15m`。该开关最长启用 24 小时并自动过期，状态保存在 `BRIEFING_STATE_PATH`。只有同时启用 `DEBUG` 和临时开关时才记录正文；日志包含简报、告警、权威预报以及 Telegram 分片的完整文本，可能暴露来源内容和位置上下文，排障后应立即关闭并妥善保护日志。token、chat ID 和请求 endpoint 不会写入这些诊断日志。

定位层从地名解析国家或行政区代码。Open-Meteo 负责城市/邮编查询，空结果时由 OpenStreetMap Nominatim 解析详细地名；结果会持久缓存。只有坐标时使用中国大陆服务范围四至宽松包围盒作快速可能性判断。省略 `WEATHER_PROVIDERS` 时，中国大陆地点使用 QWeather、Open-Meteo，其他地点只使用 Open-Meteo；显式配置时首项是主要来源，后续项依次作为备用。

RSS 为可选补充数据。需要使用时复制 `rss-sources.example.json` 为被 Git 忽略的 `rss-sources.json` 并填写真实来源；不创建该文件即可只使用天气 API。

QWeather 使用 Ed25519 JWT 认证。将控制台中的项目 ID、JWT 凭据 ID、Base64 编码的私钥 PEM 和专属 API Host 分别写入 `QWEATHER_PROJECT_ID`、`QWEATHER_CREDENTIAL_ID`、`QWEATHER_PRIVATE_KEY` 与 `QWEATHER_API_HOST`。应用每次请求前解码私钥并签发短期 Token，不使用长期 API KEY。

Telegram 投递 provider 组合平台专用 HTML renderer 与 Bot API publisher，负责渲染粗体章节、预警和可点击来源链接并完成投递。日常简报限制为一条不超过 Telegram 4096 可见字符上限的消息；权威预报清洗正文单独完整发送。需要回放历史数据进行隔离测试时，可覆盖业务时间和状态数据库：

```bash
BRIEFING_STATE_PATH=state/replay.sqlite3 weather-briefing run daily --at 2026-07-11T08:00:00+08:00
```

## 调度

项目提供单一 OCI 镜像，不需要 Docker Compose。构建并运行常驻调度器：

日报默认在 `BRIEFING_TIMEZONE` 的 08:00 运行，可用 `GREETING_HOUR` 和 `GREETING_MINUTE` 调整。小时简报默认在 09:00–23:00 的整点运行；`BRIEFING_CRON` 是 APScheduler 的小时字段表达式，例如 `9-23`、`8,12,16` 或 `*/2`。启动常驻调度器时传入 `daemon --run-now` 可在建立定时任务前立即运行一次小时简报。

```bash
cp env.example .env
cp locations.example.json locations.json
docker buildx build --load -t weather-briefing .
docker volume create weather-briefing-state
docker run -d --name weather-briefing --restart unless-stopped \
  --env-file .env \
  --mount type=bind,src="$PWD/locations.json",dst=/app/locations.json,readonly \
  --mount source=weather-briefing-state,target=/app/state \
  weather-briefing
```

若启用 RSS，再把私密 `rss-sources.json` 以只读方式挂载到容器内配置的同名路径。配置文件不会进入镜像构建上下文。

也可在持久主机上使用 cron：

```cron
0 8 * * * cd /srv/weather-briefing && .venv/bin/weather-briefing run daily
0 9-23 * * * cd /srv/weather-briefing && .venv/bin/weather-briefing run hourly
```

## 安全

- 不要把 `.env`、数据库、生成简报或真实配置提交到 Git。
- CI 使用 Gitleaks 扫描凭据，并运行自定义隐私哨兵测试。
- Telegram token 与 chat ID 只从运行环境读取；测试可选择 stdout publisher。

地点解析使用 [Open-Meteo Geocoding](https://open-meteo.com/en/docs/geocoding-api)；详细地名备用数据 © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)，通过 Nominatim 获取。公共 Nominatim 仅在本地缓存未命中时调用，并遵守其使用政策。
