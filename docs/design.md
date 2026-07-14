# 设计

## 数据流

`location resolver -> weather API + optional RSS -> cleaner/filter/memory -> LLM provider -> BriefingResult -> delivery provider (platform renderer + publisher)`

核心编排不判断具体 RSS 域名、地区、模型厂商、空气质量服务或投递平台。特殊全文转发规则来自私密 JSON 配置中的 `verbatim_title_patterns`；可选 `location_ids` 决定来源适用于哪些关注地点，空数组表示全部地点。

`OpenAICompatibleChatCompletionsProvider` 实现唯一的 OpenAI-compatible 请求逻辑，统一调用 `/chat/completions`。`DeepSeekProvider` 仅继承该实现并预置官方 Base URL，不覆盖请求或解析行为，因此部署只需提供 API Key 和模型名；OpenAI 或其他兼容服务使用通用 provider 并显式配置 Base URL。核心编排只依赖 `LLMProvider` 协议；未来接入 Anthropic 等非兼容 endpoint 时新增协议实现，不修改简报 service。原样转发规则与次日预报上下文规则分别由 `verbatim_title_patterns` 和 `forecast_title_patterns` 配置，避免来源特定标题进入代码。

## 正文清洗与全文转发

`ContentCleaner` 是 RSS 采集与业务规则之间的替换边界。默认 HTML cleaner 解析 DOM，移除脚本、样式、评论、弹窗等非正文节点，将块级结构规范化为纯文本行，并通用过滤独立日期或时间戳。来源特有的 CSS selector 和署名行规则由私密 source 配置注入，不写入公开代码。

匹配 `verbatim_title_patterns` 的文章跳过 LLM，总结任务只对清洗后的正文做独立完整投递。因此“原样”指正文语义内容不改写、不摘要、不截断，不表示保留来源页面的 HTML、交互组件或元数据噪声。清洗后的同一正文也作为后续预报的权威上下文。

RSS source 在构造 `Article` 前完成清洗。匹配全文转发规则但清洗后正文为空的条目不进入本轮文章集合，因此不会生成只有标题的消息，也不会被状态存储标记为已处理；来源后续返回有效正文时，同一条目仍可重新采集。非全文转发条目保持现有行为，由后续摘要流程决定是否使用。

默认 08:00 的日报与 09:00–23:00 的小时简报共用同一个 LLM 调用和结构化结果校验路径，两类调度均可由运行时环境调整。日报额外加载昨日权威预报、空气质量和生活指数并要求生成生活建议；小时简报只处理增量信息且禁止重复建议。

## 天气、空气质量与生活指数上下文

`LocationSpec` 来自被 Git 忽略的 `BRIEFING_LOCATIONS_FILE`，支持多个地点且只强制 `id` 与 `name`。`CachedLocationResolver` 对已有经纬度直接构造 `ResolvedLocation`；缺少经纬度时调用 `GeocodingProvider`，并把解析结果写入运行状态目录。`FallbackGeocodingProvider` 默认先调用 Open-Meteo `/v1/search` 获取城市或邮编的 WGS84 坐标、ISO 国家码、一级行政区和时区，空结果时再调用 Nominatim 解析详细地名。

`PrecisionReducingGeocodingProvider` 包装完整 provider 链：首先用原始地名查询，所有 provider 均失败后才按 `geography.json` 中明确标注为中国大陆地名格式的规则逐级移除门牌、建筑或更细粒度片段，每个候选仍重新经过完整 provider 链。结果同时保留用户原始地名、provider 实际匹配地名及是否降低精度。首次降精度匹配时，CLI 通过当前投递 provider 发送匹配地名、经纬度和写回私密地点文件的建议；缓存结果不重复通知。

公共 Nominatim provider 始终发送可识别的 User-Agent，在单进程内串行并限制为每秒最多一次，只处理用户配置且尚未缓存的少量地点。README 提供 OpenStreetMap 署名，Base URL 可切换到自建或托管实例。该策略不用于自动补全、批量抓取或每轮定时任务重复查询。

中国大陆服务范围采用包含海南的约北纬 18°–53°34′、东经 73°–135°05′ 极值，封装为宽松快速包围盒；不把延伸至南海的全国领土矩形用于天气来源选择。坐标落在盒外可直接排除中国大陆并避免网络调用；盒内只标记为“可能”，因为矩形同时覆盖邻国和港澳等区域。地名 geocoding 返回国家/行政区时使用其更具体的结果。

地理边界、排除行政区名称、美国 AQI 分级与健康提示、默认正文清洗规则、QWeather 生活指数代码和地区默认天气 provider 顺序均属于领域参考数据，维护在 `weather_briefing/data/*.json`。实现代码只负责加载、校验并应用这些数据。API endpoint、请求字段、JWT 有效期上限和 Telegram 消息上限属于外部协议契约，保留在对应 adapter 中。

`WeatherContextProvider` 以关注地区的经纬度为输入，返回统一的天气上下文快照，核心编排不依赖具体厂商响应结构。快照包含天气预报、可选生活指数和可选空气质量；独立 `AirQualityProvider` 只承担缺失空气质量时的补充。

`QWeatherProvider` 同时读取实时空气质量、今明两日天气预报和当日天气指数。它提供今明天气、温度、风、湿度、预期降水及运动、穿衣、旅游、舒适度和交通指数。空气质量请求失败不会丢弃已经有效的天气结果，而是把空气质量留空交给补充层。

`OpenMeteoProvider` 使用全球 Weather Forecast API，并尝试从其独立 Air Quality API 获取 U.S. AQI 与 PM2.5 原始浓度。公开 endpoint 适用于非商业免费使用、要求署名且无 SLA；Base URL 和可选 API Key 可配置，以便切换商业 endpoint。

`FallbackWeatherContextProvider` 按 `WEATHER_PROVIDERS` 顺序组合天气来源，首项为主要来源。未显式配置时，解析为中国大陆的地点选择 `qweather,open-meteo`，其他地点只选择 `open-meteo`。自动顺序中未配置 JWT 的 QWeather 会被跳过；用户显式指定却缺少凭据时给出配置错误。只有天气请求或响应契约失败才切换下一个已配置天气来源。

`AirQualitySupplementingWeatherProvider` 在最终天气快照缺少空气质量时调用可选 `AQICNProvider`。AQICN 只返回空气质量，不会被当成天气来源；它保留美国 EPA AQI 和 PM2.5 单项 AQI，不反向折算 PM2.5 浓度。天气来源与 AQICN 都无法提供空气质量时抛出可操作错误。

QWeather 认证由独立的 `QWeatherJWTAuthenticator` 负责。它从运行环境读取 Base64 编码的 Ed25519 PKCS#8 私钥 PEM，解码后通过 EdDSA 签发 JWT：Header 只加入凭据 ID `kid`，Payload 只加入项目 ID `sub`、提前 30 秒的 `iat` 和可配置的短期 `exp`。provider 只依赖认证协议生成 Bearer header，不接触私钥字段，也不支持把长期 API KEY 混入同一请求。

上下文快照被转换为带稳定 source ID 和验证 URL 的文档，与 RSS 输入使用同一套引用校验。08:00 的模型提示要求同时输出 AQI 标准，禁止换算或混用标准，并参考健康提示和生活指数生成运动、穿衣及口罩建议。

## 状态

SQLite 保存：

- 文章 ID、来源、发布时间、标题、链接、正文及首次处理时间；
- 已发布简报及类别；
- LLM 返回的有效预警及最后确认时间；
- 每个 RSS 源最后见到文章的时间；
- 连续任务失败次数。
- 每轮天气 API 上下文快照，包括未触发消息投递的普通更新。

数据库路径由 `BRIEFING_STATE_PATH` 指定并被 Git 忽略。单地点保持该路径；多地点以稳定地点 ID 派生独立数据库，使文章去重、预警、快照与失败计数完全隔离。地名解析缓存由 `GEOCODING_CACHE_PATH` 指定，同样只存在运行状态目录。自有服务器通过容器持久卷保存，并限制访问权限，因为这些文件包含位置相关内容和来源正文。

## 时间模型

应用只接受 Pendulum 的时区感知 `DateTime`，绝不把缺少明确时区的信息解释为时间点，也不依赖进程或服务器的本地时区。内存中的时间保留其明确时区，Pendulum 直接按绝对时间比较，不为比较提前转换。

`feedparser` 已将 RSS/Atom 的 `published_parsed` 和 `updated_parsed` 归一化为 UTC 时间元组，RSS source 只把该结果构造为 UTC `DateTime`。判断文章是否属于当地今天或昨天时，以配置的 IANA 时区构造日期起止边界，再直接与 UTC 发布时间比较，不逐条转换文章时间。调度窗口和消息展示使用地点时区。`--at` 必须带 `Z` 或明确 UTC 偏移，解析为确定时间点后只在调度边界转换到地点时区。

天气与空气质量 adapter 在响应边界把可用时间规范化为时区感知值。`parse_datetime_with_default_timezone` 集中承担供应商适配：响应自带偏移时优先使用；QWeather 的无偏移时间按其服务契约采用 `Asia/Shanghai`；AQICN 优先使用响应中的 `time.tz`，否则采用所查询地区的时区；Open-Meteo 的本地时间使用同一响应中的 IANA `timezone` 解析。若只有固定偏移而没有时区名称，则保留该明确偏移，不猜测 IANA 地区。QWeather 实时空气质量响应没有观测时间，因此该字段仍可为空；没有时间值时不伪造观测时间。

SQLite 没有原生日期时间类型，状态存储需要直接对 TEXT 做范围筛选和排序。因此应用仅在 SQLite 持久化边界把时区感知时间转换为固定宽度、包含六位微秒的 UTC 文本，使字典序等同绝对时间顺序；读取时严格校验并恢复为 UTC `DateTime`。该转换不扩散到采集、比较或业务编排路径。

## 预警记忆

每次总结把当前有效预警及其历史来源文章交给模型。模型必须返回结构化 `active_warnings`：明确解除的预警不再返回，仍有效的预警继续返回。只有引用本轮新文章或实时 API 时才刷新确认时间；纯历史延续不会刷新。超过 `WARNING_RETENTION_HOURS` 后仍无新证据的记录自动失效，既容忍短暂信息空窗，也避免预警永久残留。

## 来源引用

输入文章和辅助 API 响应都带唯一 source ID 与 URL。模型输出 JSON 中每条结论包含 `source_ids`；程序验证所有 ID 都存在，再生成平台无关的 `BriefingResult`。各投递 provider 的 renderer 从相同结构化结果生成自己的链接与排版；引用未知来源会使任务失败，禁止模型编造链接。

## 平台模板与投递

`DeliveryProvider` 把同一平台的模板 renderer 与消息 publisher 组合为投递边界。`MessageRenderer` 接收经过引用校验的 `BriefingResult`、文章和上下文，返回带正文及可见长度的 `RenderedMessage`；publisher 只负责传输该消息，不读取 LLM JSON，也不解析平台模板。Telegram provider 组合 Bot API HTML renderer 与 Telegram publisher，纯文本 stdout provider 用于本地测试。新增平台只需提供自己的组合，不修改核心编排。

日常简报在模型契约中使用低于平台上限的可配置字符预算，Telegram publisher 再校验 renderer 提供的可见字符数不超过 4096；超限视为任务失败，不拆分为多条而破坏“单条简报”约束。权威预报正文属于独立的全文投递，不与日常简报合并。

## 失败语义

### RSS 失败

RSS 是可选补充源，其失败不影响任务成功率；天气 API 是主要信息来源。

**获取失败** — 单个 RSS 源经可配置次数重试（默认 3 次，间隔 3–5 秒）后仍无法获取或解析。行为：记录警告日志，本次运行继续使用已成功获取的 RSS 内容和天气 API 数据。

**长期无更新** — RSS 源在配置的小时数（默认 24）内未曾见到任何新文章。判定基准为 `source_health` 表中记录的最后一次文章时间，不受当天本地日期筛选影响。

**告警** — 连续获取失败达到可配置阈值（`RSS_FAILURE_THRESHOLD`，默认 3）时向运维渠道发送告警；成功记录告警状态后，同一失败周期不再重复告警，源恢复后重置计数器并重新开放告警。长期无更新采用相同规则，看到新文章后重新开放。两类 RSS 健康告警均为至少一次投递：投递或状态写入失败只记录日志并在后续任务中重试，不得终止简报任务；投递成功但状态写入失败时可能产生内容相同的重复告警，接收端应按告警类型、源集合和失败周期去重。

未配置任何 RSS 源时不创建 RSS 请求或健康告警，天气 API 独立驱动每小时和每日任务。

### 任务失败

**触发条件**：天气上下文获取失败（所有 provider 均不可用）、辅助上下文源获取失败（例如 `HTTPContextSource.fetch()` 抛出 `SourceFetchError`）、LLM 调用或输出校验失败、配置校验失败。

**行为**：终止当前任务，并立即通过投递 provider 发送一次性运维提醒。投递失败时在后续任务失败中继续重试；成功投递后，同一失败周期不再重复告警。成功的任务重置计数和告警状态。RSS 获取失败不计入任务失败计数。

小时 LLM 结果包含布尔字段 `should_publish`。模型比较当前及历史 API 快照，仅在降雨、显著天气变化、预警或灾害动态值得打扰时设为真；活动预警不允许与 false 同时出现。false 结果不投递消息，但当前快照、文章去重和预警状态仍持久化。

CLI 在读取运行配置前以 INFO 幂等配置单个标准错误 handler，配置成功后再按 `DEBUG` 更新级别，避免配置错误绕过统一格式、daemon 每轮任务重复追加 handler 或向 root logger 重复传播。默认记录生命周期、文章数量、陈旧来源和失败信息。天气 provider 在组合边界由日志装饰器包装：先记录每个地点的有效 provider 顺序，再为每次逻辑调用记录 provider、结果、耗时、实际 source ID 与观测时间；主来源失败时记录经过收敛的阶段、HTTP 状态或异常类型，随后才由 fallback 组合继续。可选空气质量调用失败单独记录，不把可用天气结果改为失败。`DEBUG` 启用 RSS 获取及 LLM 重试诊断，并以非敏感元数据串联 RSS 清洗后的来源、发布时间、正文长度和权威预报标记，权威预报投递前后的来源、发布时间和正文长度，renderer 的可见与 payload 长度，以及 Telegram 分片总数、逐片长度和平台接受状态。这条链路不记录坐标、标题、正文、URL、token、chat ID 或请求 endpoint。业务层只给异常追加失败计数等上下文，完整堆栈由 CLI 入口或 APScheduler 单点记录。

完整渲染文本诊断采用第二道运行时开关，避免仅凭长期 `DEBUG` 配置泄露正文。`diagnostics rendered-text enable --for <duration>`、`status` 和 `disable` 直接读写 `BRIEFING_STATE_PATH` 中带 UTC 过期时间的 SQLite 状态；每次投递在记录前重新读取，因此另一个 CLI 进程的修改无需重启 daemon 即可生效。开关最长 24 小时，首次观察到过期状态时删除记录并发出警告。`DeliveryProvider` 记录 renderer 的完整输出，Telegram publisher 另记实际发送的每个分片，以区分渲染与传输边界；两处都只在 DEBUG 与开关同时有效时输出，并且不记录 token、chat ID 或请求 endpoint。诊断状态后端是可选能力：初始化或状态检查失败只记录警告并视为关闭，不改变消息投递。

## 依赖边界

运行时只保留各自承担单一职责的直接依赖：APScheduler 负责进程内定时调度，Beautiful Soup 负责不可信 HTML 的 DOM 清洗，feedparser 负责 RSS/Atom 解析，HTTPX 负责异步 HTTP 与可选 SOCKS 代理，Pendulum 负责时区感知时间和日历运算，python-dotenv 负责自托管环境的本地配置加载。PyJWT 的 `crypto` extra 是唯一的加密接口，QWeather 认证只调用它的高层 EdDSA JWT 编码 API，不直接调用底层加密原语。异步测试使用 AnyIO 自带的 pytest plugin，在 Python 3.11–3.14 上采用 asyncio backend，不额外引入事件循环插件。项目不引入模型厂商、天气厂商、Telegram SDK 或运行时类型检查框架，以免把可替换边界绑定到额外工具。

## 环境变量兼容性

Docker `run --env-file` 接受的是 `KEY=value` 列表，不按 shell 语义解释值，因此 `KEY='value'` 中的单引号可能成为容器内值的一部分。docker-compose 和 python-dotenv 会解析各自支持的环境文件语法，包括成对引用的值。

本地 `.env` 文件继续由 `load_dotenv(override=False)` 按 python-dotenv 的原生语法加载。`Settings.from_env()` 只为已经注入进程环境、且仍保留一对匹配外层单引号或双引号的值移除这一层；不匹配的引号属于值本身，必须保持不变。这样既兼容 `docker run --env-file .env`，也不会截断合法密钥或令牌。

## 镜像与构建上下文

项目使用 uv 原生 `uv_build` 构建后端。Dockerfile 使用多阶段构建：先从官方 Distroless uv 镜像取得 uv/uvx，再复制到 Debian 13 Distroless Python nonroot 镜像，并直接以该镜像按 `uv.lock` 创建生产虚拟环境；最终阶段基于 digest 固定的 `gcr.io/distroless/python3-debian13`，从独立 assets 镜像加入 Bash 与 Toybox，并只复制运行环境和应用。builder 与 runtime 使用相同 Debian 版本及系统 Python，并通过镜像探针验证；最终进程以无特权用户运行。`.dockerignore` 不继承 `.gitignore`，因此使用独立白名单，只让 Dockerfile 实际需要的项目元数据、锁文件和包源码进入 BuildKit 上下文；`.env`、Git 历史、测试和文档不会发送给 builder。
