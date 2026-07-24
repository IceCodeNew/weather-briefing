# 系统设计

本文说明当前实现及各模块的边界。产品行为见[产品需求](requirements.md)，非直观取舍见[技术取舍](notes.md)。

## 总体流程

```text
地点解析
  -> 天气与可选信息源
  -> 清洗、筛选和历史状态
  -> 大语言模型
  -> 结构化简报
  -> 平台渲染与投递
```

核心流程只依赖应用自己的接口，不直接处理模型厂商、天气厂商、服务状态页、RSS 站点或投递平台。外部服务的请求格式和平台语法留在各自的适配器中。

主要边界包括：

- `GeocodingProvider`：把地点名称解析为坐标；
- `WeatherContextProvider`：提供完整天气信息；
- `ContextCapabilityProvider`：补充空气质量、过敏原、预警或短时预报；
- `LLMProvider`：把上下文转换为平台无关的结构化结果；
- `DeliveryProvider`：渲染并发送结果；
- `SQLiteStateStore`：保存文章、简报、预警和运行状态。

包职责如下：

- `config` 在环境变量和私密文件边界完成解析与校验；
- `geocoding` 包含定位协议、候选匹配、外部服务适配器和缓存解析器；
- `weather` 包含平台无关协议、各天气服务适配器、能力组合和来源文档转换；
- `llm` 只包含模型协议、结构化 schema、any-llm 兼容适配器和结果解析，不依赖天气领域；
- `delivery` 分离平台无关投递协议、渲染器和具体平台适配器；
- `application` 保存历史上下文预算、模型输入构造和输出契约修复等应用策略；
- `composition` 负责根据配置组装外部服务，`cli` 只负责命令分派、运行生命周期和调度；
- `persistence` 把 schema、固定格式序列化和运行时诊断与事务存储分开，业务结果仍由单个 `SQLiteStateStore` 原子提交；
- `data` 保存随程序发布的提示词、端点、分类和本地化资源，读取与领域校验由使用这些资源的功能模块负责。

包的 `__init__` 只导出有意支持的功能接口，测试直接引用行为的所有者模块。

## 配置入口

运行配置来自环境变量和两个私密 JSON 文件：

- `BRIEFING_LOCATIONS_FILE` 指向地点文件，默认是 `locations.json`；
- `RSS_SOURCES_FILE` 指向可选 RSS 文件，默认是 `rss-sources.json`。

配置在进入业务流程前完成类型、范围和必填校验。应用拥有的固定选项会拒绝未知值。第三方模型名称和厂商参数交给对应 SDK 校验。

`locations.json` 的每个地点都必须填写唯一的 `id`。`id` 用于区分地点，配置后不要随意修改。每个地点还应至少提供以下两项之一：地点名称；完整的经纬度坐标。

可选 `language` 是简报目标语言，默认 `en`。日本地点可用 `jma_office_code` 指定六位 JMA 预报区代码。

RSS 来源可以按地点 ID 限定范围，并配置正文清洗规则。`verbatim_title_patterns` 决定哪些文章要完整转发。`forecast_title_patterns` 决定哪些文章进入次日预报上下文。

这些来源特有的规则只存在私密配置中。

## 地点解析与地区判断

`CachedLocationResolver` 把地点输入转换为完整的 `ResolvedLocation`：

- 只有地点名称时，依次使用 Open-Meteo Geocoding 和 Nominatim 正向查询；
- 只有经纬度坐标时，使用 Nominatim 反向查询；
- 地点名称和经纬度坐标都有时，直接采用配置值。

正向查询按顺序匹配逗号分隔的地点名称、地区和国家。定位结果可以在这些名称之间包含其他行政区。

只有带中文地址标记的结构化名称会在正向查询全部失败后根据 `geography.json` 降低精度。其他名称不会删除末尾数字、地区或国家后重试。

降低精度后的每个名称仍会依次交给 Open-Meteo Geocoding 和 Nominatim。

地点字段补全、失败后继续运行和低精度匹配确认等用户行为见 [`requirements.md`](requirements.md)。

`locations.json` 回写复用配置加载器的名称和坐标校验。事务按地点 ID 合并：`name` 为 `null` 时补齐名称，`latitude` 和 `longitude` 均为 `null` 时成对补齐坐标；已有值和其他字段保持不变。应用在原文件描述符上写入、截断并同步内容，保留单文件 Docker bind mount 的 inode。

`locations.json` 属于单个部署。CLI 在加载配置前获取 state 目录的 run lock，并持有到定位缓存、地点配置和 SQLite 业务提交全部结束，使该部署的定时任务和手动任务串行。地点文件不维护另一套锁；同步回写在工作线程中执行，避免文件刷新阻塞异步事件循环。

定位缓存路径由 `GEOCODING_CACHE_PATH` 指定，默认是 `state/geocoding.json`。缓存先于地点配置事务写入，保存坐标、行政区和时区，不保存当前简报语言。每次读取缓存后，都以地点文件中的 `language` 覆盖运行时值。

中国大陆的快速判断使用宽松包围盒，只用于排除明显在范围外的坐标。包围盒命中不能代替国家和行政区判断。定位服务返回国家信息时，以该信息为准。

公共 Nominatim 请求带可识别的 User-Agent，并在单进程内限制为每秒最多一次。它只处理用户配置且尚未缓存的少量地点。

定位候选筛选日志记录服务、地点 ID、查询序号、候选数量和筛选结果。

日志不记录地点名称、坐标或第三方响应正文。

`no-results` 表示服务没有返回候选，`no-match` 表示应用拒绝了所有候选。

## 天气能力与地区组合

`CapabilityProviderSet` 按能力组合天气信息。完整天气服务放在天气槽位，局部服务只声明自己真正提供的能力。

当前能力包括：

- 天气预报；
- 空气质量；
- 结构化过敏原；
- 生活指数；
- 气象预警；
- 短时预报。

未设置 `WEATHER_PROVIDERS` 时，程序按地点选择：

- 中国大陆：`qweather,open-meteo`；
- 新加坡：`open-meteo,nea-sg`；
- 日本：`open-meteo`，有 office code 时追加 `jma-jp`；
- 其他地区：`open-meteo`。

没有和风天气凭据时，自动顺序会跳过 `qweather`。用户明确指定 `qweather` 却缺少凭据时，启动会报配置错误。

显式顺序中的第一项是主要来源，后续完整天气服务是备用来源。只提供局部能力的服务必须放在完整天气服务之后。局部服务也可以单独运行，但不会被当成完整天气来源。

这个顺序只控制数据获取和降级，不表示信息权威性的高低。

天气请求或响应不符合契约时，完整天气服务按顺序降级。局部服务出现预期的请求或输入错误时，只丢弃该补充信息。其他编程错误继续向上抛出。

### 内置天气服务

`QWeatherProvider` 提供天气、生活指数和可选空气质量。普通预报使用实时空气质量；指定未来日期时使用对应日期的预报数据。空气质量失败不会丢弃已经取得的天气结果。

和风天气使用 `QWeatherJWTAuthenticator` 签发短期 JWT。认证输入包括项目 ID、凭据 ID、专属 API Host 和 Base64 编码的 Ed25519 私钥。实现不支持用长期 API Key 代替该流程。

`OpenMeteoProvider` 提供全球天气，并从独立接口读取空气质量和欧洲花粉数据。Open-Meteo API 返回语言中立的结构化数据，适配器把天气、空气质量和花粉统一转换为固定 `en` 来源正文。WMO 天气代码映射在模块加载时完成验证，未知代码保留为 “Unrecognized weather condition” 并写入安全日志。`SourceDocument` 记录英文来源语言，模型按地点配置的目标语言生成最终简报。公开接口适用于非商业使用，没有 SLA；Base URL 和可选 API Key 可以替换。

`AQICNProvider` 只在最终天气结果缺少空气质量时补充当前观测。AQICN 的结构化数据也统一转换为固定 `en` 来源正文。它不参与未来日期查询，也不把 PM2.5 单项 AQI 换算为浓度。

`NEASingaporeNowcastProvider` 只提供新加坡两小时预报。`JMAJapanForecastProvider` 只提供地点所配置预报区的日本本地预报。它们没有完整天气服务所需的全部数据，因此与 Open-Meteo 组合，而不是作为完整天气 fallback。

模型在相同时间和地区出现冲突时优先采用当地权威机构的最新资料，并同时引用冲突来源。

JMA 没有 office code 时不会猜测东京或其他预报区。

### 日期和空气质量

指定目标日期时，天气、空气质量、生活指数和过敏原要选择同一天的数据。服务不支持该日期时保留明确缺失，不复用当前观测或其他日期建议。

Open-Meteo 的逐小时空气质量和花粉预报按目标日峰值生成生活建议输入。AQI 和污染物保持来源给出的标准和单位，不做跨标准换算。

## 语言

每个天气或能力服务都声明实际输出语言，以及是否允许在请求时选择语言。请求只能选择服务声明支持的语言。固定语言服务不会伪报成用户目标语言。

语言使用基础 BCP 47 标签，并做大小写规范化。当前不支持扩展子标签和私有子标签。匹配时会逐步去掉地区或脚本部分；没有匹配项时使用服务默认语言。

`SourceDocument` 保存来源正文的实际语言。地点的 `language` 则表示最终简报语言，两者不能混用。模型只在两者主语言不同时进行最终翻译。

`BriefingResult.output_language` 传给渲染器。当前平台标题支持简体中文、繁体中文、英文和日文。其他合法语言保留模型正文，但平台标题回退为英文。

## RSS、正文清洗与来源

`ContentCleaner` 位于 RSS 采集和业务规则之间。默认实现解析 HTML，移除脚本、样式、评论、弹窗和独立时间戳，再输出纯文本。来源特有的 CSS selector 和署名规则由私密配置提供。

命中完整转发规则的文章不经过模型改写。这里的“完整”指保留清洗后的正文语义，不表示保留网页 HTML 和交互组件。清洗后为空的文章不会进入本轮输入，也不会标记为已处理。

文章 ID 同时包含来源身份，用于去重和模型引用。同一内容出现在不同来源时保留不同 ID，以维持来源隔离。

天气和 RSS 都会转换为 `SourceDocument`，其中包含供模型引用的来源 ID、名称、语言和核验链接。模型输出中的来源 ID 必须属于本轮输入。渲染时按显示名称和 URL 去重。

## AI 服务状态

`ServiceStatusProvider` 与 `WeatherContextProvider` 同级，返回平台无关的 `ServiceStatusSnapshot`。每个厂商适配器在独立模块中；当前注册 DeepSeek、OpenAI、Anthropic 和 Kimi。运行时通过 `SERVICE_STATUS_PROVIDERS` 选择，默认启用全部，空值关闭。

四个 provider 都读取官方状态页的事件 feed。feed 的稳定事件标识与内容修订共同形成 revision ID，能够覆盖故障进展和明确恢复，又避免每五分钟下载完整历史 API。适配器从官方事件标题和受影响组件保守分类为 `web`、`api` 或 `other`；未知范围不做推断。状态页失败属于可选来源失败，不阻断其他状态源或天气任务。

服务状态使用 `SERVICE_STATUS_CRON` 独立调度，默认 `*/5 * * * *`。调度器可以同时注册天气和服务状态任务；没有地点文件但显式配置了状态来源时只注册服务状态任务。两类任务只共享进程级状态锁，避免并发写入持久化状态。首次读取的已恢复历史只建立基线；新的官方消息先进入独立的通知价值判断，值得打扰用户时才投递。成功投递或明确判定无需通知后才记录 handled revision，因此投递失败可以重试。

通知价值判断是独立于采集、内容生成、翻译和投递的应用契约；天气简报与服务状态使用同一策略资源，但分别提供各自的当前事实和历史。服务状态没有预定义故障或恢复文本，标题和正文只来自官方消息。消息为英语或 `SERVICE_STATUS_LANGUAGE` 指定语言时原样转发，语言不匹配时只请求忠实翻译，失败时回退官方原文。`SERVICE_STATUS_PUBLISHERS` 接受逗号分隔的一个或多个平台，未配置时回退天气的单值 `PUBLISHER`；每个 revision 分平台记录投递结果，部分平台失败后的重试不会向已成功的平台重复发送。服务状态不进入天气 `SourceDocument`、历史预算或简报结构化输出。

## 大语言模型

`AnyLLMStructuredProvider` 是 any-llm SDK 的薄适配器。模型厂商的认证、API Base、请求格式、超时和网络重试由 any-llm 及厂商 SDK 处理。

`LLM_PROVIDER` 使用 any-llm 的 provider ID，`LLM_MODEL` 使用对应模型 ID。已部署的 DeepSeek 旧变量只在配置入口作为通用变量的后备。

开发环境安装 `any-llm-sdk[all]`，用于验证所有 completion provider 的装载边界。基础运行依赖只包含 SDK 核心包。官方镜像额外安装 DeepSeek、OpenAI、OpenRouter 和 Z.AI GLM 所需组件；GLM 使用 any-llm 的 `zai` provider ID。

`LLMStructuredOutput` 同时用于 SDK 的结构化输出和应用侧复验。应用还会检查来源 ID、必填建议、预警 ID 和章节间重复等领域规则。

`LLM_MAX_ATTEMPTS` 只修复已经返回但不符合输出契约的正文。认证失败、限流、超时或空响应不进入契约修复。

CLI 负责关闭自己创建的模型服务对象及其网络资源。测试或外部调用方注入的对象视为借用，不由应用关闭。

## 调度与投递

`daemon` 创建 forecast 和 briefing 两类 APScheduler 任务，并保持常驻。它不接受立即运行参数。

`run forecast --run-now` 和 `run briefing --run-now` 是独立的一次性进程。它们复用普通业务流程，不创建第二个调度器。

`run forecast --date YYYY-MM-DD` 把当地目标日期传给天气服务和模型。该参数不改变实际运行时间、状态写入时间或历史窗口。测试历史回放使用 `--at`，不能与 `--date` 混用。

每天最后一个 briefing 时段会查询当天是否已经成功发送过变化提醒。尚未发送时，即使模型认为无需提醒，也会投递一条无声消息。手动运行不使用无声投递。

模型返回平台无关的 `BriefingResult`。Telegram 和纯文本渲染器分别负责标题、链接、转义和长度限制。Bark 使用紧凑纯文本渲染器，将带来源编号的 headline 放入通知标题，正文不再重复标题，并以短编号关联末尾的来源名称表；来源 URL 省略，但仍保留逐项引用校验。

Telegram publisher 把静默标志转换为 `disable_notification=true`。INFO 日志记录消息长度、分块数量、单条消息模式和静默投递选项，但不记录正文、Bot Token 或 Chat ID。

Telegram 拒绝请求时，publisher 以唯一一条 WARNING 记录 HTTP 状态、分块位置和安全错误类别。错误类别来自已知的 Telegram API 描述、`parameters.migrate_to_chat_id` 字段和 HTTP 状态映射，未知响应不会原样进入日志。投递异常携带结构化错误类别；当业务消息因目标会话、Bot 身份或发送权限不可用而失败，且运维告警复用同一投递对象时，服务不再尝试通过该对象发送失败告警，只记录跳过原因。

Bark publisher 支持明文推送和可选的 AES-GCM 加密推送。只配置 device key 时，请求体始终包含 `body`，并在 rendered message 提供标题时包含 `title`；配置边界要求加密 key 和初始 IV 同时存在或同时缺失，并允许用不含凭据、query 或 fragment 的绝对 HTTP(S) `BARK_BASE_URL` 覆盖官方端点。加密 key 接受 16、24 或 32 个 ASCII 字符，对应 Bark 的 AES128、AES192 和 AES256；初始 IV 是与 Bark App 设置一致的 12 字符值，App 端模式必须设为 GCM 和 noPadding。publisher 每次加密生成新的 12 字符随机 IV，并在请求中与 ciphertext 一起发送；Bark App 优先使用请求携带的 IV 解密。`cryptography` 的 `AESGCM` 直接生成 Bark 所需的 ciphertext 与 16 字节 tag 组合，再以 Base64 编码；正文始终加密，标题存在时随正文一起加密。两种模式都使用 `/push` JSON 请求体，避免 device key 进入 HTTP URL 日志。`BARK_GROUP` 原样映射到 `group`；普通投递的内部 level 固定为 `timeSensitive`，无声投递固定为 `passive`。

Bark 内容和 APNs 元数据共享 4 KiB payload。`BRIEFING_MAX_CHARACTERS` 是优先压缩到一条消息的目标，默认且不得超过 650；确有必要时，服务允许简报达到目标的两倍，并最多投递两条。结构化 LLM 输出默认使用 4096 token，为 JSON 字段、来源 ID 和模型生成开销保留独立于可见正文限制的空间；显式的较低值仍会传入 prompt，要求模型连同 JSON 结构一并压缩。生成约束要求 forecast 的天气结论通常压缩到一至两项、每个生活建议主题只保留一个行动短句。模型在输出 token 上限被截断属于输出契约失败，参与有界修复重试；网络、鉴权等请求失败仍立即终止。Telegram 和 stdout 保持 3500 个简报字符和 8192 个输出 token。Bark renderer 省略天气段标题和多余空行，以短编号关联末尾的来源名称表；publisher 对每个分片共同计算标题和正文的字符预算，并使用保证最少分块数的换行优先算法。INFO 和 WARNING 日志只记录长度、分块位置、HTTP 状态和安全错误类别，不记录 device key、加密 key、标题、正文、ciphertext、IV 或第三方错误正文。HTTP 2xx 响应也必须是 `code=200` 的 JSON，防止把代理或兼容服务的异常响应误判为成功。

## 状态

SQLite 按地点保存：

- 已见文章和待处理文章；
- 已发布简报；
- 天气上下文快照；
- 当前有效预警；
- RSS 和任务健康状态；
- 已登记但尚未确认投递的完整正文。

单地点直接使用 `BRIEFING_STATE_PATH`。多地点根据配置中的地点 `id` 分别创建数据库，防止历史和预警互相污染。修改 `id` 会让该地点改用新的数据库，因此配置后不要随意修改。

主简报被平台接受后，应用在一个事务中保存本轮文章状态、简报、天气快照、预警变化和待投递正文。事务失败时全部回滚。

完整正文随后按稳定顺序逐篇投递，每篇在平台接受后单独确认。

历史数据按 `HISTORY_HOURS` 清理，预警按 `WARNING_RETENTION_HOURS` 清理。待处理文章、待投递正文和健康状态不参加普通历史清理。

历史天气快照先按来源折叠连续相同值，再受文档数和字符数限制。每个来源至少保留窗口基线和最新值。预算仍不足时继续生成简报，同时发送去重后的运维告警。

所有持久化时间都转换为固定宽度 UTC 文本。业务层仍使用带时区的 Pendulum 值。

## 失败与告警

RSS 只重试传输错误和指定的临时 HTTP 状态。等待时间默认随机取 3–5 秒，并尊重有效的 `Retry-After`。重试耗尽后记录失败，但不阻断天气流程。

任务失败、RSS 连续失败和来源长期无更新分别记录并告警。告警投递或计数写入属于次要操作，失败不能替换原始业务错误。

未知的预警解除 ID 会被过滤并按数量记录，不修改现有预警。有效预警只有在明确解除、降级或超过保留时间时更新。

## 日志与隐私

启动时先建立基础日志，再在配置读取成功后应用 `DEBUG` 级别。这样配置错误也有统一格式。

INFO 日志记录服务、操作、方法、结果、耗时、HTTP 状态或异常类型。天气服务的选择和降级也会记录。

常规日志不记录凭据、坐标、标题、正文、URL、接收方标识或异常消息。第三方 SDK logger 保持 WARNING，避免其调试内容绕过隐私边界。

完整渲染正文只能通过限时诊断开关启用，并且还要求 `DEBUG=true`。应用只记录自己控制的字段，不直接开放第三方 SDK 原始 DEBUG 日志。

诊断状态读取失败时按关闭处理，不能阻断消息投递。

## 构建与发布

项目使用 uv 和 `uv_build`，提交 `uv.lock`。支持 Python 3.11–3.14，开发和测试优先使用当前最新稳定版本。

OCI 镜像基于 Distroless Debian 13 的系统 Python，以非 root 用户运行。镜像依赖从锁文件安装，不复制其他发行版或 Python 构建生成的虚拟环境。

镜像的常驻入口和 `docker exec` 命令都直接调用 `/home/nonroot/app/.venv/bin/weather-briefing`。调用方不需要激活虚拟环境。

正式版本由 `X.Y.Z` tag 触发一次构建。版本标签、`latest`、`edge` 和 commit SHA 指向同一正式镜像。后续普通 master 构建只更新 `edge` 和 commit SHA。
