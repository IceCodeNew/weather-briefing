# 设计

## 数据流

`location resolver -> weather API + optional RSS -> cleaner/filter/memory -> LLM provider -> BriefingResult -> delivery provider (platform renderer + publisher)`

核心编排不判断具体 RSS 域名、地区、模型厂商、空气质量服务或投递平台。特殊全文转发规则来自私密 JSON 配置中的 `verbatim_title_patterns`；可选 `location_ids` 决定来源适用于哪些关注地点，空数组表示全部地点。

`AnyLLMStructuredProvider` 是应用与 any-llm SDK 之间的薄适配器，只负责固定严格结构化输出 schema 并把 SDK 响应转换为应用字典；模型厂商的认证、API Base、请求协议和参数差异由 any-llm 及其官方 SDK 处理。`LLM_PROVIDER` 直接使用 SDK 声明的 provider ID，配置层只验证该 ID，不维护厂商分支或复制 provider 环境变量。DeepSeek 已投入使用的 `DEEPSEEK_MODEL` 与 `DEEPSEEK_BASE_URL` 只在配置边界作为通用 `LLM_MODEL` 与 SDK `DEEPSEEK_API_BASE` 的后备。

开发依赖安装 `any-llm-sdk[all]`，运行依赖声明 SDK 核心包；该核心包本身直接依赖 OpenAI 兼容层，部署者再把选定的其他 provider extras 作为独立、锁定的安装要求与应用一起安装。这样仓库可以测试所有 provider 的装载边界，而生产镜像不必携带未使用厂商的附加 SDK。应用不向 provider 注入自建 HTTP client，也不在 any-llm 外层重复实现网络重试；传输超时、退避和重试由对应厂商 SDK 管理。CLI 对工厂创建的 any-llm provider 负责生命周期，在退出时通过 adapter 关闭 provider 持有的 SDK transport；service 使用外部注入的 `LLMProvider` 时不取得其所有权。service 的 `LLM_MAX_ATTEMPTS` 只修复已经返回但不满足输出契约的结果。

`LLMStructuredOutput` 以 Pydantic 声明所有必填字段、类型、非空文本、来源数组和 advice topic，同时作为 any-llm 的 `response_format` 与 service 接收 mock/provider 输出后的复验 schema。厂商 SDK 的结构化输出能力负责生成和初次解析，本地 schema 防止测试替身或 provider 兼容差异绕过契约；来源 ID 是否属于本轮输入仍是运行时领域校验。核心编排只依赖领域端口 `LLMProvider`，它不负责任何厂商映射或 wire protocol；切换 SDK provider 不修改简报 service。原样转发规则与次日预报上下文规则分别由 `verbatim_title_patterns` 和 `forecast_title_patterns` 配置，避免来源特定标题进入代码。

## 正文清洗与全文转发

`ContentCleaner` 是 RSS 采集与业务规则之间的替换边界。默认 HTML cleaner 解析 DOM，移除脚本、样式、评论、弹窗等非正文节点，将块级结构规范化为纯文本行，并通用过滤独立日期或时间戳。来源特有的 CSS selector 和署名行规则由私密 source 配置注入，不写入公开代码。

匹配 `verbatim_title_patterns` 的文章跳过 LLM，总结任务只对清洗后的正文做独立完整投递。因此“原样”指正文语义内容不改写、不摘要、不截断，不表示保留来源页面的 HTML、交互组件或元数据噪声。清洗后的同一正文也作为后续预报的权威上下文。

RSS source 在构造 `Article` 前完成清洗。匹配全文转发规则但清洗后正文为空的条目不进入本轮文章集合，因此不会生成只有标题的消息，也不会被状态存储标记为已处理；来源后续返回有效正文时，同一条目仍可重新采集。非全文转发条目保持现有行为，由后续摘要流程决定是否使用。

默认 08:00 的 `run forecast` 与 09:00–23:00 的 `run briefing` 共用同一个 LLM 调用和结构化结果校验路径，两类调度均可由运行时环境调整。forecast 额外加载昨日权威预报、空气质量和生活指数并要求生成生活建议；briefing 只处理增量信息且禁止重复建议。

结构化结果不包含独立摘要字段。模型把最重要的天气概况浓缩到标题，renderer 固定按天气信息、气象预警、自然灾害动态和生活建议的顺序输出非空章节。来源去重以最终显示名称和可核验 URL 的组合为准；QWeather 空气质量沿用同一次天气查询返回的具体 `fxLink`，不把 API metadata 中的开发者 attribution 页面暴露为内容来源。

## 调度与一次性运行

`daemon` 只负责建立 forecast 和 briefing 两类 APScheduler 任务并保持常驻，不接受立即运行参数。已有 daemon 时，运维从另一个进程执行 `run forecast --run-now` 或 `run briefing --run-now`；两条路径都复用对应的普通编排、忽略调度窗口、执行一次后退出，不创建第二个 scheduler。briefing 路径还显式覆盖 `should_publish=false`，投递积压文章与最新 API 上下文。

`run forecast --date YYYY-MM-DD` 将当地今天或未来目标日期作为独立参数传入 service、天气 provider 和 LLM payload，不修改当前运行时间。QWeather 从 3 日天气和空气质量预报，以及覆盖目标日期的 1 日或 3 日生活指数中选择同一天的数据；若目标超出响应范围则按 provider 组合规则降级。Open-Meteo 使用相同的 `start_date` 与 `end_date` 精确请求目标日天气及服务商范围内的逐小时空气质量和花粉预报，并以目标日最差 AQI 时段及各花粉峰值作为保守的生活建议输入。服务商不支持目标日期或目标超出附加数据预报范围时保留明确缺失，不得复用当前观测或其他日期建议；仅提供当前观测的 AQICN 不参与目标日期补充。这样查看后天或其他未来日期不会把 SQLite 处理时间、RSS 健康状态或历史窗口写到未来。`--date` 只属于 forecast，且与仅供测试历史回放的运行时间覆盖参数 `--at` 互斥。

CLI 根据 `BRIEFING_CRON` 判断当天最后一个 briefing 小时，并在对应地点的 SQLite 状态中查询当地零点至当前时刻是否已有成功发布的 briefing。只有当天尚未发布时，service 才执行最后窗口兜底；LLM 仍正常总结与返回 `should_publish`，结果为 false 时覆盖为发送，并把 `silent=true` 沿 `BriefingService -> DeliveryProvider -> Publisher` 传递。Telegram publisher 将它映射为 Bot API 的 `disable_notification=true`；本来就值得立即投递的 true 结果保持正常通知，stdout 等其他 publisher 可以忽略静默提示。手动 `--run-now` 是用户主动触发，也不使用无声投递。

## 天气、空气质量与生活指数上下文

`LocationSpec` 来自被 Git 忽略的 `BRIEFING_LOCATIONS_FILE`，支持多个地点并强制稳定 `id`；用户必须在 `name` 与完整经纬度之间至少提供一项。`CachedLocationResolver` 将三种输入收敛为名称和坐标都完整的 `ResolvedLocation`：只有名称时执行正向解析，只有坐标时执行反向解析，两者都有时直接采用配置值。正向路径由 `PrecisionReducingGeocodingProvider` 包装 `FallbackGeocodingProvider`，先让 Open-Meteo 和 Nominatim 依次尝试完整名称，全部失败后才按数据驱动规则逐级降低查询精度。反向路径通过独立 `ReverseGeocodingProvider` 协议调用 Nominatim `/reverse`，使用 WGS84 坐标取得最接近的 OSM 地址对象、规范展示名、国家代码和行政区；反向结果不经过正向降精度规则。正反向结果都写入被 Git 忽略的定位缓存。

service 将最终解析得到的完整地点名作为 `location_scope.full_name` 交给模型；用户提供名称时保留该名称，只有坐标时使用反向解析的规范展示名。定位结果中已知的行政区和国家代码只作为可选提示，未知字段不写入 payload，也不得由模型猜测。模型以完整地点名为主，按当地行政区划语义识别上级范围，不在核心代码中硬编码省、市、区或国外行政层级的名称与顺序。完整地点或其任一上级行政区受影响时相关，同级或下级的其他地区不相关。经纬度和内部地点 ID 不进入 LLM payload；经纬度只用于天气 API。由于 RSS 正文没有统一结构化灾害边界，灾害地域语义判断留在 LLM 输出契约中，核心编排不加入地区关键词表或供应商专用分支。

`PrecisionReducingGeocodingProvider` 包装完整 provider 链：首先用原始地名查询，所有 provider 均失败后才按 `geography.json` 中明确标注为中国大陆地名格式的规则逐级移除门牌、建筑或更细粒度片段，每个候选仍重新经过完整 provider 链。结果同时保留用户原始地名、provider 实际匹配地名及是否降低精度。首次降精度匹配时，CLI 通过当前投递 provider 发送匹配地名、经纬度和写回私密地点文件的建议；缓存结果不重复通知。

公共 Nominatim provider 始终发送可识别的 User-Agent，在单进程内串行并限制为每秒最多一次，只处理用户配置且尚未缓存的少量地点。README 提供 OpenStreetMap 署名，Base URL 可切换到自建或托管实例。该策略不用于自动补全、批量抓取或每轮定时任务重复查询。

中国大陆服务范围采用包含海南的约北纬 18°–53°34′、东经 73°–135°05′ 极值，封装为宽松快速包围盒；不把延伸至南海的全国领土矩形用于天气来源选择。坐标落在盒外可直接排除中国大陆并避免网络调用；盒内只标记为“可能”，因为矩形同时覆盖邻国和港澳等区域。地名 geocoding 返回国家/行政区时使用其更具体的结果。

地理边界、排除行政区名称、美国 AQI 分级与健康提示、花粉浓度分级与花粉类型名称、默认正文清洗规则、QWeather 生活指数代码和地区默认天气 provider 顺序均属于领域参考数据，维护在 `weather_briefing/data/*.json`。实现代码只负责加载、校验并应用这些数据。API endpoint、请求字段、JWT 有效期上限和 Telegram 消息上限属于外部协议契约，保留在对应 adapter 中。

`WeatherContextProvider` 以关注地区的经纬度为输入，返回统一的天气上下文快照，核心编排不依赖具体厂商响应结构。快照包含天气预报、可选生活指数、可选空气质量和可选花粉过敏原；独立 `AirQualityProvider` 只承担缺失空气质量时的补充。`AirQualitySnapshot` 用统一生效时间配合 observation/forecast 时间类型表达资料语义，渲染层据此分别标注“观测时间”或“预报时段”，避免把 QWeather 或 Open-Meteo 的目标日期预报描述成既成观测。

`QWeatherProvider` 的常规预报读取实时空气质量、今明两日天气和当日生活指数；显式目标日期查询改用 3 日生活指数及 3 日空气质量预报，并按天气预报中的目标日期选择同一天的数据。它提供目标日期天气、温度、风、湿度、预期降水及运动、穿衣、旅游、舒适度和交通指数。空气质量请求失败不会丢弃已经有效的天气结果；常规预报把空气质量留空交给补充层，目标日期查询则保留缺失而不使用当前 AQICN 观测冒充预报。

`OpenMeteoProvider` 使用全球 Weather Forecast API，并尝试从其独立 Air Quality API 获取 U.S. AQI 与 PM2.5 浓度。公开 endpoint 适用于非商业免费使用、要求署名且无 SLA；Base URL 和可选 API Key 可配置，以便切换商业 endpoint。

`FallbackWeatherContextProvider` 按 `WEATHER_PROVIDERS` 顺序组合天气来源，首项为主要来源。未显式配置时，解析为中国大陆的地点选择 `qweather,open-meteo`，其他地点只选择 `open-meteo`。自动顺序中未配置 JWT 的 QWeather 会被跳过；用户显式指定却缺少凭据时给出配置错误。只有天气请求或响应契约失败才切换下一个已配置天气来源。

`AirQualitySupplementingWeatherProvider` 在最终天气快照缺少空气质量时调用可选 `AQICNProvider`。AQICN 只返回空气质量，不会被当成天气来源；它保留美国 EPA AQI 和 PM2.5 单项 AQI，不反向折算 PM2.5 浓度。天气来源与 AQICN 都无法提供空气质量时抛出可操作错误。

天气快照可选包含花粉过敏原信息。QWeather 生活指数请求过敏指数（类型 7），并将目标日期的供应商指数说明直接放入 `lifestyle_advice`。`OpenMeteoProvider` 在同一次空气质量 API 请求中额外请求花粉变量：常规预报使用当前值，显式目标日期使用服务商支持范围内该日逐小时预报的各类型峰值，再按浓度分级（参考数据 `allergen_guidance.json`）转换为独立 `AllergenSnapshot`，供对应日期生活建议参考。Open-Meteo 花粉数据仅在欧洲花粉季可用，来自 CAMS European Air Quality forecast 的 ENSEMBLE 数据；引用该上下文时，渲染层使用文档名称确定性展示 Open-Meteo 与 CAMS ENSEMBLE 署名。数值边界拒绝布尔值、NaN 和正负无穷等非有限输入；缺失或字段无效时不影响天气与空气质量结果。当前实现不把综合过敏指数解释为具体花粉种类，也不推断独立的杨絮、柳絮等飞絮信息。

QWeather 认证由独立的 `QWeatherJWTAuthenticator` 负责。它从运行环境读取 Base64 编码的 Ed25519 PKCS#8 私钥 PEM，解码后通过 EdDSA 签发 JWT：Header 只加入凭据 ID `kid`，Payload 只加入项目 ID `sub`、提前 30 秒的 `iat` 和可配置的短期 `exp`。provider 只依赖认证协议生成 Bearer header，不接触私钥字段，也不支持把长期 API KEY 混入同一请求。

上下文快照被转换为带稳定 source ID 和验证 URL 的文档，与 RSS 输入使用同一套引用校验。08:00 的模型提示要求同时输出 AQI 标准，禁止换算或混用标准，并参考健康提示和生活指数生成运动、穿衣及口罩建议；PM2.5 直接按“PM2.5 数值 单位”表达，过敏指数和花粉只进入生活建议。建议使用结构化 topic，程序强制每日预报覆盖穿衣、除湿、运动和口罩；当前输入包含综合过敏指数或花粉过敏原时，同时强制覆盖过敏建议。QWeather adapter 根据供应商的过敏指数代码标记天气文档，Open-Meteo 花粉文档直接携带相同标记，核心编排不依赖供应商文案识别过敏数据。天气与空气质量文档使用同一个 provider 展示名称。

## 状态

SQLite 保存：

- 文章 ID、来源、发布时间、标题、链接、正文及首次处理时间；
- 尚未被成功发布的文章；
- 已发布简报、类别及发布时间；
- LLM 返回的有效预警及最后确认时间；
- 每个 RSS 源最后见到文章的时间；
- 连续任务失败次数。
- 每轮天气 API 上下文快照，包括未触发消息投递的普通更新。

数据库路径由 `BRIEFING_STATE_PATH` 指定并被 Git 忽略。单地点保持该路径；多地点以稳定地点 ID 派生独立数据库，使文章去重、预警、快照与失败计数完全隔离。地名解析缓存由 `GEOCODING_CACHE_PATH` 指定，同样只存在运行状态目录。自有服务器通过容器持久卷保存，并限制访问权限，因为这些文件包含位置相关内容和来源正文。

每次任务成功结束时，状态存储在重置任务失败计数的同一事务中清理历史数据。已处理文章、已发布简报和上下文快照保留 `HISTORY_HOURS`，预警按独立的 `WARNING_RETENTION_HOURS` 保留；恰好位于阈值时刻的记录仍属于当前窗口。尚未成功发布的待处理文章和使用 upsert 维护的健康状态不参与历史清理。失败任务不清理历史，以免在恢复前丢失诊断与待处理上下文。

历史上下文快照在进入 LLM 前按 source 折叠连续相同值，并依次保留各 source 的最新值、窗口基线和最近变化节点；每条记录通过 `history_role` 明确标记这三种角色。`LLM_HISTORY_MAX_DOCUMENTS` 限制快照总数，`LLM_HISTORY_MAX_CHARACTERS` 限制 `recent_context_documents` JSON 数组的完整序列化字符数。完整记录放不下时，内置 source adapter 可提供确定性 `history_summary`；入选摘要以 `content_compacted` 和 `original_content_characters` 标记，不截断出语义不完整的正文。最新值或窗口基线在摘要后仍放不下时，任务继续但通过持久化的内容指纹发出一次运维告警；内容改变或恢复容纳后才重新开放告警。最近变化节点因预算丢弃只属于预期降级。最近成功发布的简报仍随 payload 提供，模型因此可以用有界的基线、最新值及变化节点判断相对上次成功发布的累计变化。DEBUG 诊断只记录候选数、入选数和序列化字符数，不记录来源名称、URL 或正文。

## 时间模型

应用只接受 Pendulum 的时区感知 `DateTime`，绝不把缺少明确时区的信息解释为时间点，也不依赖进程或服务器的本地时区。内存中的时间保留其明确时区，Pendulum 直接按绝对时间比较，不为比较提前转换。

`feedparser` 已将 RSS/Atom 的 `published_parsed` 和 `updated_parsed` 归一化为 UTC 时间元组，RSS source 只把该结果构造为 UTC `DateTime`。判断文章是否属于当地今天或昨天时，以配置的 IANA 时区构造日期起止边界，再直接与 UTC 发布时间比较，不逐条转换文章时间。调度窗口和消息展示使用地点时区。测试历史回放使用的 `--at` 必须带 `Z` 或明确 UTC 偏移，解析为确定时间点后只在调度边界转换到地点时区；未来 forecast 的 `--date` 保持独立，不能污染该运行时间。

天气与空气质量 adapter 在响应边界把可用时间规范化为时区感知值。`parse_datetime_with_default_timezone` 集中承担供应商适配：响应自带偏移时优先使用；QWeather 的无偏移时间按其服务契约采用 `Asia/Shanghai`；AQICN 优先使用响应中的 `time.tz`，否则采用所查询地区的时区；Open-Meteo 的本地时间使用同一响应中的 IANA `timezone` 解析。若只有固定偏移而没有时区名称，则保留该明确偏移，不猜测 IANA 地区。QWeather 实时空气质量响应没有观测时间，因此该字段仍可为空；没有时间值时不伪造观测时间。

SQLite 没有原生日期时间类型，状态存储需要直接对 TEXT 做范围筛选和排序。因此应用仅在 SQLite 持久化边界把时区感知时间转换为固定宽度、包含六位微秒的 UTC 文本，使字典序等同绝对时间顺序；读取时严格校验并恢复为 UTC `DateTime`。该转换不扩散到采集、比较或业务编排路径。

## 预警记忆

每次总结把当前有效预警及其历史来源文章交给模型。模型必须返回结构化 `active_warnings`：明确解除的预警不再返回，仍有效的预警继续返回。只有引用本轮新文章或实时 API 时才刷新确认时间；纯历史延续不会刷新。超过 `WARNING_RETENTION_HOURS` 后仍无新证据的记录自动失效，既容忍短暂信息空窗，也避免预警永久残留。

## 来源引用

输入文章和辅助 API 响应都带唯一 source ID、展示名称与 URL。模型输出 JSON 中 `headline` 通过 `headline_source_ids` 引用来源，`conclusions`、`active_warnings`、`disaster_tracking` 和 `advice` 的每一项都包含 `source_ids`；程序验证引用非空且所有 ID 都存在，再生成平台无关的 `BriefingResult`。各投递 provider 的 renderer 从相同结构化结果生成自己的链接与排版，在标题和各分项后明确标注“来源”，并始终以 API provider 名称或 RSS 配置中的 `name` 作为链接文字；RSS 配置应使用公众号、微博账号或发布机构等公开名称。引用未知来源或遗漏来源会使任务进入 LLM 修复重试，禁止模型编造链接。

同一时段的天气现象在来源间冲突时，模型不得把片段拼成无争议的单一结论。契约要求明确呈现差异，并在能够识别当地权威气象机构时优先采用其最新信息，同时保留冲突来源引用；核心编排不硬编码地区机构名称或供应商优先级。

## 平台模板与投递

`DeliveryProvider` 把同一平台的模板 renderer 与消息 publisher 组合为投递边界。`MessageRenderer` 接收经过引用校验的 `BriefingResult`、文章和上下文，返回带正文及可见长度的 `RenderedMessage`；publisher 只负责传输该消息，不读取 LLM JSON，也不解析平台模板。Telegram provider 组合 Bot API HTML renderer 与 Telegram publisher，纯文本 stdout provider 用于本地测试。新增平台只需提供自己的组合，不修改核心编排。

日常简报在模型契约中使用低于平台上限的可配置字符预算，Telegram publisher 再校验 renderer 提供的可见字符数不超过 4096；超限视为任务失败，不拆分为多条而破坏“单条简报”约束。权威预报正文属于独立的全文投递，不与日常简报合并；其分片按解析后的可见字符计数，并在边界处闭合和重新打开格式标签，保证每个 Bot API 请求都包含独立有效的 HTML。

## 失败语义

### RSS 失败

RSS 是可选补充源，其失败不影响任务成功率；天气 API 是主要信息来源。

**获取失败** — 单个 RSS 源只对传输错误和临时 HTTP 状态（408、425、429、500、502、503、504）重试，其他 HTTP 错误立即失败。重试默认最多 3 次、随机间隔 3–5 秒；有效 `Retry-After` 指定更长退避时优先遵守。请求仍无法获取或解析时记录警告日志，本次运行继续使用已成功获取的 RSS 内容和天气 API 数据。

**长期无更新** — RSS 源在配置的小时数（默认 24）内未曾见到任何新文章。判定基准为 `source_health` 表中记录的最后一次文章时间，不受当天本地日期筛选影响。

**告警** — 连续获取失败达到可配置阈值（`RSS_FAILURE_THRESHOLD`，默认 3）时向运维渠道发送告警；成功记录告警状态后，同一失败周期不再重复告警，源恢复后重置计数器并重新开放告警。长期无更新采用相同规则，看到新文章后重新开放。两类 RSS 健康告警均为至少一次投递：投递或状态写入失败只记录日志并在后续任务中重试，不得终止简报任务；投递成功但状态写入失败时可能产生内容相同的重复告警，接收端应按告警类型、源集合和失败周期去重。

未配置任何 RSS 源时不创建 RSS 请求或健康告警，天气 API 独立驱动每小时和每日任务。

### 任务失败

**触发条件**：天气上下文获取失败（所有 provider 均不可用）、辅助上下文源获取失败（例如 `HTTPContextSource.fetch()` 抛出 `SourceFetchError`）、LLM 调用或输出校验失败、配置校验失败。

**行为**：终止当前任务，并立即通过投递 provider 发送一次性运维提醒。投递失败时在后续任务失败中继续重试；成功投递后，同一失败周期不再重复告警。成功的任务重置计数和告警状态。RSS 获取失败不计入任务失败计数。

briefing LLM 结果包含布尔字段 `should_publish`。模型比较上次成功发布以来累计的待处理文章、当前及历史 API 快照，只在用户需要据此采取准备行动时设为真。典型情形包括约一小时后影响关注地区的降雨（消息应给出可用的时间、概率和雨量）、显著天气变化、预警实质动态，以及明确影响 `location_scope` 的灾害；普通复述、节气知识、重复预警描述、轻微数值波动和无影响灾害必须为 false。预警只在 `active_warnings` 表达，不得在天气结论重复；持续生效但内容没有实质变化的预警允许与 false 同时出现，并继续保存在预警状态中。false 结果不投递消息；本轮文章进入独立的待处理集合，当前快照和预警状态照常持久化。后续 briefing 或 forecast 任务把全部待处理文章与新内容一起交给模型，且只有简报成功投递后才把本轮覆盖的待处理文章移入已处理文章集合。这样相邻小时都不显著、但从上次投递起已累积成显著变化的内容仍能触发一次合并简报。输入保留文章发布时间、API 内容中的观测时间，并把历史简报的类别、正文和发布时间组成结构化记录；系统提示要求模型在合并历史时淘汰被较新资料取代的气温、降水、风力、空气质量和短时预报，只保留当前仍有效的信息。

CLI 在读取运行配置前以 INFO 幂等配置单个标准错误 handler，配置成功后再按 `DEBUG` 更新级别，避免配置错误绕过统一格式、daemon 每轮任务重复追加 handler 或向 root logger 重复传播。默认记录生命周期、文章数量、陈旧来源和失败信息。天气 provider 在组合边界由日志装饰器包装：先记录每个地点的有效 provider 顺序，再为每次逻辑调用记录 provider、结果、耗时、实际 source ID 与观测时间；主来源失败时记录经过收敛的阶段、HTTP 状态或异常类型，随后才由 fallback 组合继续。可选空气质量调用失败单独记录，不把可用天气结果改为失败。

应用直接构建的 HTTP adapter 共用 `LoggedAsyncClient` 作为实际调用记录边界。调用点通过 HTTPX request extension 只附加代码内定义的静态 provider 与 operation；客户端统一记录方法、耗时、HTTP 状态或传输异常类型，不读取 URL、查询参数、header、正文、响应或异常消息。这样新增应用 HTTP adapter 仍可使用普通 HTTPX 接口，RSS 的每次重试和 Telegram 的每个分片则自然对应一条请求历史。LLM 是例外：any-llm 及厂商 SDK 拥有其 transport，应用不为获取通用 HTTP 元数据而接管 client、timeout 或 retry。标签只接受小写 kebab-case；缺少或不合法的标签统一记录为 `unclassified/request`，既避免不可信文本进入结构化日志，也保证未来新增请求不会静默缺失。该层只判断 HTTP 请求是否收到错误状态；响应内容的业务校验仍由各 adapter 负责，天气逻辑调用和 fallback 决策继续由较高层日志记录，避免把传输成功误当作业务数据有效。

`DEBUG` 只提高 `weather_briefing` logger 的级别，root handler 与 any-llm、OpenAI、HTTPX 等 SDK logger 始终保持 WARNING，避免第三方客户端输出完整请求。它启用 RSS 获取及 LLM 调用诊断，并以非敏感元数据串联 RSS 清洗后的来源、发布时间、正文长度和权威预报标记，权威预报投递前后的来源、发布时间和正文长度，renderer 的可见与 payload 长度，以及 Telegram 分片总数、逐片长度和平台接受状态。仅启用 DEBUG 时，这条链路不记录坐标、标题、正文、URL、token、chat ID 或请求 endpoint。业务层只给异常追加失败计数等上下文，完整堆栈由 CLI 入口或 APScheduler 单点记录。

完整文本诊断采用第二道运行时开关，避免仅凭长期 `DEBUG` 配置泄露正文。`diagnostics rendered-text enable --for <duration>`、`status` 和 `disable` 直接读写 `BRIEFING_STATE_PATH` 中带 UTC 过期时间的 SQLite 状态；每次记录前重新读取，因此另一个 CLI 进程的修改无需重启 daemon 即可生效。开关最长 24 小时，首次观察到过期状态时删除记录并发出警告。LLM adapter 记录应用拥有的 system prompt、结构化输入和结构化输出，`DeliveryProvider` 记录 renderer 的完整输出，Telegram publisher 另记实际发送的每个分片；这些记录都只在 DEBUG 与开关同时有效时输出。LLM adapter 不记录 SDK client 配置、认证信息或请求 endpoint，第三方 SDK logger 继续保持 WARNING。完整文本可能包含来源内容、来源 URL、坐标和其他位置上下文，因此必须作为敏感日志保护。诊断状态后端是可选能力：初始化或状态检查失败只记录警告并视为关闭，不改变 LLM 请求或消息投递。

## 依赖边界

运行时只保留各自承担单一职责的直接依赖：APScheduler 负责进程内定时调度，Beautiful Soup 负责不可信 HTML 的 DOM 清洗，feedparser 负责 RSS/Atom 解析，HTTPX 负责异步 HTTP 与可选 SOCKS 代理，Pendulum 负责时区感知时间和日历运算，python-dotenv 负责自托管环境的本地配置加载。PyJWT 的 `crypto` extra 是唯一的加密接口，QWeather 认证只调用它的高层 EdDSA JWT 编码 API，不直接调用底层加密原语。异步测试使用 AnyIO 自带的 pytest plugin，在 Python 3.11–3.14 上采用 asyncio backend，不额外引入事件循环插件。项目不引入模型厂商、天气厂商、Telegram SDK 或运行时类型检查框架，以免把可替换边界绑定到额外工具。

## 环境变量兼容性

Docker `run --env-file` 接受的是 `KEY=value` 列表，不按 shell 语义解释值，因此 `KEY='value'` 中的单引号可能成为容器内值的一部分。docker-compose 和 python-dotenv 会解析各自支持的环境文件语法，包括成对引用的值。

本地 `.env` 文件继续由 `load_dotenv(override=False)` 按 python-dotenv 的原生语法加载。`Settings.from_env()` 只为已经注入进程环境、且仍保留一对匹配外层单引号或双引号的值移除这一层；不匹配的引号属于值本身，必须保持不变。这样既兼容 `docker run --env-file .env`，也不会截断合法密钥或令牌。

## 镜像与构建上下文

项目使用 uv 原生 `uv_build` 构建后端。Dockerfile 使用多阶段构建：先从官方 Distroless uv 镜像取得 uv/uvx，再复制到 Debian 13 Distroless Python nonroot 镜像，并直接以该镜像按 `uv.lock` 创建生产虚拟环境；最终阶段基于 digest 固定的 `gcr.io/distroless/python3-debian13`，从独立 assets 镜像加入 Bash 与 Toybox，并只复制运行环境和应用。builder 与 runtime 使用相同 Debian 版本及系统 Python，并通过镜像探针验证；最终进程以无特权用户运行。`.dockerignore` 不继承 `.gitignore`，因此使用独立白名单，只让 Dockerfile 实际需要的项目元数据、锁文件和包源码进入 BuildKit 上下文；`.env`、Git 历史、测试和文档不会发送给 builder。

镜像工作流实现 [requirements.md](requirements.md#运行环境) 定义的标签通道，并用一次 manifest 创建命令同时更新当前事件对应的全部标签。
