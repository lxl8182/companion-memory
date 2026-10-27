**companion-memory 接手指南**

更新：2026-10-24。项目根：`F:\game\projectNEKO\companion-memory`。

**1. 用户目标与当前交付边界**

用户要把 N.E.K.O 的通用记忆抽成自己的独立项目，增加能够修订当前人设卡的自演化，再无感接回 N.E.K.O。情绪分析、Live2D 控制、表情、动作、口型、语音、打断、主动交流与游戏接管继续使用宿主原链路。

项目已有独立记忆与人格演化核心、宿主适配器和管理台，C01—C05 成果继续复用。用户于 2026-10-24 明确改为新系统交付：新配置、新数据根、新启动入口；旧流程、旧基线和旧数据不作为开发/验收前置，完成后退役旧配置读取与旧启动路径。当前按 [S05 新系统开发与验收计划](docs/s05-new-system-plan.md) 的 P0、A—F 与 N01—N15 推进。旧 C06/B15 执行已取消，其交付记录仅作历史参考。

**2. 先读哪些文件**

按本文件 → `docs/roadmap.md` 的 S 台账 → `docs/s05-new-system-plan.md` 当前工作包 → 相关模块与测试读取。领域合同按需查 `docs/design.md`；管理台复用 `webui/` 当前水彩页面及已有管理操作。`closeout.md` 与旧启动说明仅用于查已有实现，不恢复旧 C06 的前置和验收要求。批次状态只在 roadmap 维护；历史记录中的“下一批”不作为当前任务入口。

相关现有路径：

| 内容 | 当前路径 |
|---|---|
| 工作区约定 | `F:\game\projectNEKO\AGENTS.md` |
| N.E.K.O 源码 | `F:\game\projectNEKO\N.E.K.O` |
| 修改宿主前的规则 | `F:\game\projectNEKO\N.E.K.O\.agent\rules\neko-guide.md` |
| 既有记忆实现与历史阶段 | `F:\game\projectNEKO\N.E.K.O-记忆成长与遗忘方案.md` |
| NekoSpire 源码 | `F:\game\projectNEKO\plugin\nekospire` |
| 用户实际数据根 | `F:\game\projectNEKO\nekodata`，实际 memory_dir 仍按配置解析 |

原宿主和插件包含未提交的有效改动。迁移来源以实际工作树为准，不能只拿远端 main；不能 reset、覆盖或顺手提交其他人的改动。

2026-10-24 已接通 S05 的 N.E.K.O 文字端：独立服务 48931、宿主 48911，同一个 companion 角色；新入口配置 `host.enabled=true` 后拉起带 `companion_host.py` 的源码宿主。完整轮次、持久补交、发布版本与页面显示回执已验证，`neko_chat` 单独计数。TTS/麦克风/主动交流等开关按实际需要调整，不再默认静音。语音播放/打断、主动/实时/游戏及宿主语义编辑继续按 roadmap 推进。

**日常启动入口（新链路）**：`start-rin-stack.cmd`（仓库根，纯 ASCII 批处理）。它先起服务 48931 与 `companion_host` 宿主 48911，等宿主确认为 `connected` 后再按需拉起 Steam 桌宠壳；`NEKO_SHELL=none` 只起服务与宿主。Steam 启动选项填 `cmd /c "F:\game\projectNEKO\companion-memory\start-rin-stack.cmd" %command%`。旧的 `plugin
ekospire\scripts\start-neko-source-shell.cmd` 走 `COMPANION_MEMORY_ENGINE` 内嵌链路，占用同样的 48911-48916 端口，两条链路不能同时开；新脚本在端口被占时直接报错，不会强杀进程。

下一批原为 [N.E.K.O 宿主详细接手计划](docs/s05-neko-host-next-plan.md) 的 H01—H03，2026-10-25 已按该顺序交付并在日常实例完成只读/草稿验收：宿主角色设置直接读写服务的当前人格（新端点 `/v1/host/persona*`，能力由 `stack.toml` 的 `host_persona` 显式授予，默认关闭），聊天页按轮显示生成中/待同步/已保存/中断/失败并在刷新后恢复，文字主动交流使用固定服务快照、消费者 `neko_active` 与分类 `companion_output`，未送达候选不写正文。本轮未接实时语音、游戏或 DSH/QQ；完整桌宠壳与语音/实时/游戏验收仍未完成。工作包状态只在 roadmap 维护，交付细节见其 H01—H03 记录与 `docs/webui-console.md` §2.3。

**3. 当前代码地图**

| 模块 | 已实现内容 | 后续边界 |
|---|---|---|
| `contracts.py` | 冻结的领域合同；B02 起含 schema_version/received_at/content_class/payload/request_id/event_id/episode_id、回执状态与处理位置、日志条目/哨兵/恢复结果；B03 增 MemoryBlock、Deletion/Correction 请求与回执、MentionDirective | 后续按批次增补状态/人格回执与类型化错误 |
| `ports.py` | EventSink / ProfileReader / ContextProvider + Clock / StorageRoots / AtomicJsonStore / WritePermit / EventJournal / OutboxStore / CursorStore；B04 增 ModelInvoker（tier/messages/预算/timeout/purpose）；B06 增 TokenCounter（count/acount/truncate/identity）与 EmbeddingProvider（is_available/model_id/embed_batch） | 只在实现需要时增加依赖接口；核心不 import 宿主 |
| `invalidation.py` | B03 纯判定：文本归一化/匹配/哈希、subject/scene 匹配、fact/reflection/persona 失效判定与过滤 | B04/B05/B06 复用，不在各模块重写 |
| `event_sources.py` | B03 来源键构造/解析/清洗（host/plugin 两种格式） | 与宿主/插件两侧规则一致，不改旧键 |
| `classification.py` | B04 内容分类与窗口过滤（9 类、优先级、plugin-only 路由、legacy cutoff、删除来源跳过），纯函数 | B05/B06 复用；分类口径随宿主枚举增补 |
| `api.py` | 事件转交、人格读取、可选召回预算、输出范围过滤；B03 增 correct/forget/retract/retry/correction_block；B05 增 reflect/promote_reflections/archive_reflections/record_surfaced/record_reflection_feedback/reflection_block/growth_block/growth_items/record_growth_usage/retract_growth/persona_block/persona_entries/persona_corrections/resolve_persona_corrections；B06 增 build_turn_context/recall_memory/recall_by_time，build_context 改用真实召回；B07 增 read_state/commit_state/maintain（未装配抛 NotConfiguredError） | 保持组合入口；不把整个记忆管线堆进这里 |
| `persistence/` | 原子 JSON/文本写、schema 门禁、数据根与角色名校验、写许可、时钟、事件快照指纹；事件日志与回放、outbox、游标、来源账本、请求幂等、scoped tombstone、facts/archive 原始视图、SQLite time_indexed+FTS；B03 增 memory_deletions/memory_corrections/user_directives；B04 增 recent.json 存储与两代内容闸门只读读取；B05 增 reflections/surfaced/synth_backoff/reflection_archive、persona/persona_corrections/persona_archive、growth_uses 存储与归档分片；B07 增 companion_state.json 存储 | 领域规则由 domain 实现，不在存储层重写 |
| `domain/` | B03 删除屏障、forget 引擎与更正引擎；B04 事实抽取（ingest→原文→outbox→模型→去重→屏障）与最近对话压缩、删除 recent 清理器；B05 反思合成/证据/晋升/成长、人格条目与更正队列、删除清理器与更正失效器；B06 只读召回（BM25/cosine/RRF、被动 turn_context、episode/fading/mention、读侧失效过滤）；B07 companion_state 服务（租约/版本/幂等）、outbox 派发与维护循环 | 宿主调度/生命周期在 B08 接管；缓存/校准属 O01/O04 |
| `adapters/durable.py` | `DurableEventStore`：journal 落盘 → 账本提交 → `durable=true`；构造时注册回放处理器，首次写入前 reconcile；B03 接入屏障（删除来源写入 rejected、回放跳过） | 生产写入口与宿主许可在 B08 注入；可直接用于现有运行根 |
| `adapters/composition.py` | B04 显式装配 `build_memory_core`：journal/ledger/outbox/barrier/facts/recent/gates/timeindex/deletion/correction/directives/extraction/compression；B05 增 reflection/persona 存储与域服务、归档分片，并把两个 service 注册为删除清理器与更正派生失效器；B06 增 RecallService（可选 token/embedding 端口注入，token_counter 同时进更正块）；B07 增 StateStore/StateService、OutboxDispatcher（extract_facts bulk）与 MaintenanceService（recover=sink.recover） | B08 宿主适配复用；B14 迁移钩子挂到 maintenance.migrations |
| `scope.py` | 场景适用性匹配 | 迁入时与已有 applies_to 的行为对齐 |
| `persona/rendering.py` | 同角色、主体及场景的人设投影 | 修订、发布与持久化由下方 B10—B13 服务负责 |
| `adapters/in_memory.py` | 进程内幂等事件表、人格快照和原始事件示例 | 仅演示，不接用户目录 |
| `__main__.py` | 十一段可运行示例：内存、临时数据根持久、更正+删除闭环、对话→抽取→压缩闭环、反思→证据→晋升→成长→删除/撤回闭环、召回→指代续接→更正过滤闭环、状态/维护/outbox 独立闭环、人设卡导入/修订/冻结/删除投影、发布版本/消费边界/planner/回滚、演化提案、演化管理/审核/回滚/开关（固定替身模型/嵌入，无网络） | 示例使用临时数据根，不写现有运行根 |
| `cli.py` | 独立数据目录入口：`demo`、`run --memory-root --character --once|--loop`、`migrate --memory-root [--backup-root] [--dry-run]`、`rollback --memory-root --backup-root [--no-preserve-new]`、`package-check`；单写者锁、路径校验、退出码 0/2/3/1 | 宿主内由 runtime 接管（B08），CLI 用于独立运行/演练 |
| `persistence/ownership.py` | S01 根所有权锁：`canonical_root`（真实路径，处理大小写/`.`/`..`/目录别名）、`acquire_root_lock`（OS 文件锁，`mode` 仅作说明）、`read_root_lock` 诊断读取；锁记录放在锁定字节之前，冲突时可读 | CLI/宿主/WebUI 三个可写入口共用；独立服务端（S04）接入同一锁 |
| `persistence/turns.py`、`domain/turns.py` | S02 整轮提交：`turn_requests.json` 状态机（pending/committed/failed/interrupted/deleted）、`request_id` 业务幂等（回放/冲突/已删）、`core.turn_committed` 回放处理器、`TurnCommitService`（reserve→生成→依据复查→入队→journal 提交）；删除/更正清正文只留标识 | HTTP 端点由 S04 提供；生成器/依据检查由装配方注入 |
| `adapters/model_http.py` | S04 零依赖模型适配器：`HttpModelInvoker`（urllib + 工作线程、分档、信号量限并发、逐调用超时）、`ModelSettings`（`api_key_env` 是变量**名**）、`ModelCallError` 归类 auth/config/timeout/rate_limit/transport/format | 凭据从不记录；公网鉴权/TLS 属 S05 之后的独立设计 |
| `service/memory_service.py`、`service/server.py` | S04 常驻服务：`MemoryService.start()`（根锁→建核心→recover→就绪）、`storage/chat/background` 分项能力、`stop()` 先停后台再放锁；`MemoryApi` 实现 `/v1/chat` `/v1/turns` `/v1/context` `/v1/requests/{id}` `/v1/health`，`MemoryRequestHandler` 为标准库传输，统一错误信封 | 默认仅回环并校验 Host/Origin；CLI 用 `companion-memory-run serve` 启动 |
| `stack_config.py`、`stack.py`、`tools/stack.ps1` | S05 新配置模板/解析、固定安装解释器、init/start/stop/status、独立根与服务进程 | 新运行根 companion-runtime；宿主目录注入与远程接线继续按 roadmap |
| `start.cmd`、`stop.cmd`、`start.ps1`、`启动说明.txt`（仓库根） | **Windows 日常入口**：双击 `start.cmd` 启动（首跑自动 `init`、已在运行则复用、成功后开浏览器），`stop.cmd` 停止，`start.ps1 -Action status` 查看状态。`start.cmd`/`stop.cmd` 为 GBK，`start.ps1`/`启动说明.txt` 为 UTF-8 BOM | 走同一套 `companion_memory.stack`；解释器默认仓库 `.venv`，可用 `-Python` 覆盖 |
| `service/listening.py` | 绑定语义的唯一实现：Windows 用 `SO_REUSEADDR` 会让第二个进程绑上**正在监听**的端口（曾导致同端口多实例），因此平台分支为 Windows 独占 / POSIX 允许 reuse | 两个传输层都用 `ExclusiveAddrMixin`；新增监听 socket 时勿自行设 `allow_reuse_address` |
| `service/console.py`、`service/management.py` | 复用既有管理业务视图，同核心 /v1 管理接口、空库创建/首次发布、写身份与显示名 | 水彩资源随 wheel；当前 API 合同见 docs/webui-console.md。**显示名是人格卡身份段首行 `名字: <名字>`，不是独立文件或独立段落**：`service_character.json` 与旧 `identity_name` 段都只作一次性迁移来源，并入后删除；编辑身份正文时服务会自动保留名字行 |
| `service/chat.py` | 管理台直连对话 `ChatBridge`：组装 `TurnRequest` 后调 `MemoryApi.chat`，复用整轮提交/幂等重放/能力状态；`history()` 只读 `recent.json`；`DemoChatModel` 仅供 demo 根且回复自述离线 | 不新增领域规则；`status()` 只报凭据是否存在，永不返回凭据；对宿主接入（C）无前置依赖 |
| `service/model_config.py` | 设置页的模型配置存储：校验/规范化/原子写 `local_state/model_config.json`、损坏 fail closed、只覆盖页面填入的非空字段、公开视图不含凭据 | 凭据仅存本机文件（POSIX 0o600），不进响应/导出/日志；`stack.toml` 仍是超时/并发/代理的权威 |
| `migration.py` | B14 数据格式清单/版本检测、迁移步骤与失败回滚、备份 manifest+sha256、rollback preserve_new（保留切换后新增对话/更正/删除）、verify、make_migration_hook | C05 已接宿主钩子与副本演练；正式数据操作按当次授权 |
| `webui/` | 管理台：唯一前端 `designs/tidebell/index-watercolor.html`（水彩稿 + `live.js` 实时层），`server.py`（API+静态，`/` 302 到该页）、`seed.py`、C04 独立测试前端 `testbed/` 与 `tests/test_testbed_api.py`；启动与评审见 `docs/webui-console.md` | 其余设计稿与 `webui/index.html`、`webui/assets/**` 已删除；`--memory-root` 可直连真实根，默认只读 |

B01 冻结的接口字段与端口见 design.md §17.9，B02 已实现存储侧端口与合同增补（§17.14），B03 已实现更正/删除屏障（§17.15），B04 已实现最近对话、事实抽取与内容闸门（§17.16），B05 已实现反思、现有人格与成长能力（§17.17），B06 已实现相关召回、经历关联与淡出（§17.18），B07 已实现当前状态、后台维护与独立闭环（§17.19，D1 完成）；本表描述 B07 结束时的代码；B08—B14 追加：宿主仓库 `app/memory_server/engine_bridge/`（ports/runtime/mappers/middleware，默认关闭）、`persistence/persona_card.py` 与 `domain/persona_card.py`（当前卡/revision/冻结/投影/删除更正过滤）、`persistence/persona_versions.py` 与 `domain/persona_publish.py`（发布版本令牌/消费边界/planner 配对/回滚）、`persistence/evolution.py` 与 `domain/evolution.py`（证据驱动提案/确定性校验，默认关闭）、`persistence/persona_admin.py` 与 `domain/persona_admin.py`（审核/接受/拒绝/撤回/diff/回滚/开关）、`migration.py`（格式检测/迁移/备份/回滚演练），以及 `adapters/composition.py`、`api.py` 的对应装配与操作。

**B10—B14 追加模块**

| 模块 | 内容 | 边界 |
|---|---|---|
| `persistence/persona_card.py`、`domain/persona_card.py` | 当前人设卡（revision/历史/冻结/投影/删除更正过滤/回放） | C02 补宿主投影与编辑 |
| `persistence/persona_versions.py`、`domain/persona_publish.py` | 发布版本令牌/消费边界/planner 配对/回滚/删除失效 | C02 补实时/游戏等消费者 |
| `persistence/evolution.py`、`domain/evolution.py` | 证据驱动修订提案/确定性校验，默认关闭、只提议不写卡 | C03 接后台触发与自动采用 |
| `persistence/persona_admin.py`、`domain/persona_admin.py` | 审核/接受/拒绝/撤回/diff/回滚/自动开关/删除更正联动 | C03 统一模式，C04 接管理台 |
| `migration.py` | 数据格式检测/迁移/备份/回滚 preserve_new/verify | C05 接迁移钩子与副本演练 |

**C01—C03 追加模块**

| 模块 | 内容 | 边界 |
|---|---|---|
| 宿主 `engine_bridge/ROUTE_MATRIX.md` | 64 端点归属（委托 15/宿主保留约 40/停用 10 个旧循环）与 C02 API 合同 | C04/C05 复核迁移 |
| 宿主 `engine_bridge/host_projection.py`、`main_logic/engine_persona.py` | characters.json 语义投影读写、编辑入口（base_revision/409+draft） | 资源/语音/凭据保留宿主 |
| 宿主 `engine_bridge/evolution_admin.py` | 演化 status/mode/evaluate/retry facade（基于 EngineRuntime） | 路由由 C04 接管理台 |
| 核心 `persistence/persona_projection.py`、`domain/persona_projection.py` | 宿主卡导入、投影同步、四消费者 adopted/pending/failed、宿主编辑、删除更正失效 | 实时 ack 实机待 C05 |
| 核心 `persistence/evolution_runs.py`、`domain/evolution_scheduler.py` | off/propose/auto 单源模式、后台触发/幂等/去重/冷却、auto accept→revise→publish、重启续跑 | 端到端实机待 C06 |

**C04—C05 追加内容**

| 内容 | 说明 | 边界 |
|---|---|---|
| `webui/testbed/`、`webui/tests/test_testbed_api.py` | 独立可测试管理台（原生 HTML/CSS/JS）：状态/概览/时间流/事实/反思/人格卡/发布/演化/清理/导出；6 API 测试 | 现有 webui 前端与美术未动；实机 UI 待 C06 |
| `webui/server.py` 加法 API | engine/status、evolution mode、persona consumers/adopt/sync、cleanup/delete、evolution evaluate/retry、/testbed 静态路由 | 旧响应字段保留；服务仍 loopback |
| `.scratch/check_c04_webui.py`、`check_c05_integration.py`、`check_c05_perf.py`、`check_c05_versions.py`、`check_c05_rehearsal.py`、`c05-a01-a16.md` | C04/C05 验收证据与 A01—A16 映射 | 实机项归 C06 |


`MemoryEngine` 在 B00/B02 只有 observe、get_profile、build_context；B03 起可通过注入的 deletion/correction 服务使用 correct、forget、retract、retry、correction_block；B05 起通过注入的 reflection/persona 服务使用 reflect、promote_reflections、record_surfaced、growth_block、persona_block 等操作；B06 起 build_context 走真实召回，并新增 build_turn_context/recall_memory/recall_by_time；B07 起 read_state/commit_state/maintain 可用；B10—B13 增卡片、发布、提案与审核操作。未装配服务时抛 `NotConfiguredError`，不返回空成功。

内存 append 的 `accepted` 表示进程内接收，`durable=false`；不能将其当成落盘回执。持久路径 `DurableEventStore` 的 `accepted/duplicate/conflict` 才返回 `durable=true`，写许可拒绝返回 `rejected/durable=false`。`demo_event` 不能冒充已确认事实。内存示例只演示事件；后续示例使用替身模型/嵌入演示抽取、召回和演化。

当前预算控制只把可选召回超时转换为空结果，必需人格读取和内部错误继续传播；后续网络或磁盘适配器必须设置相应 I/O 期限和取消语义。

**4. 必须保持的架构边界**

1. 核心可以在不安装 N.E.K.O 的环境独立导入和运行；不导入宿主 `app/main_logic/main_routers/config/utils` 或插件 SDK。
2. 领域属于核心；N.E.K.O 适配器属于宿主仓库。S05 新链路只在独立服务内实例化核心，宿主与同源管理台使用该权威实例。
3. 正式数据同一时刻只有一个写入方。main/tool/plugin 不分别建立会写同一目录的实例。
4. 存储、模型配置、token 工具和宿主写入许可经明确接口注入；复用实际需要的基础代码，不整包复制 `utils` 或所有依赖。
5. 角色/主体是身份边界；来源 scene 与 applies_to 分开。游戏生活事实可共享，战术经验留在插件或原场景；群组/用户习惯不可串到其他主体。
6. 同事件的原文、镜像、摘要、反思只算一份经历。用户明确偏好直接生效；AI 复述、时间经过和用户沉默不加支持。
7. 用户情绪、角色持续心情和本次外显表情分别处理。核心不直接调用 Live2D、不发送模型参数、不接 TTS 队列。
8. 人设修订只动语义段落，保留宿主工具/语音/表现协议。配置中的模型路径、音色、API 凭据不属于可演化字段。
9. 引擎维护当前人设唯一真源，旧 characters.json 同步最终应读取其投影，避免旧卡覆盖新卡。该链路已接通（H01—H03），自动演化（off/propose/auto）可自由切换。
10. 原 HTTP 路径、WS 事件、turn/request/speech 标识和真实送达回执保持兼容；内部 dataclass 不直接替换现有 wire 格式。

**5. 接手顺序：按 roadmap 从首个未完成批次推进**

[分批台账](docs/roadmap.md) 是唯一状态入口。S05 的服务侧与非宿主收尾已完成（dev2，134 项定向/安装检查、flash 真实对话、N14 p95 109.14 ms）；接着完成 A 宿主目录注入、C 远程客户端/持久待同步队列、D 主动/实时/游戏与表现、E 整栈验收及 F 旧入口退役。服务合同见 `docs/webui-console.md` §2.3。C06 已取消，不恢复旧流程或双模式切换。下面的清单是 B01 的历史调研入口，已有交付见 design §17，按当前工作包需要查阅。

需要补查接口归属时，复用以下依赖清单：

- 现有事实、反思、人格、状态及删除存储的读取/写入入口。
- EventLog、Reconciler、outbox、cursors 的实例所有者、任务启动与停止点。
- 角色创建/切换/重命名/删除、数据根迁移、维护模式及云存档写入许可。
- 普通聊天、主动候选、回忆工具、记忆管理、游戏 context/events/state 的现有响应合同。

已有原格式持久化与组件装配继续复用，可直接在现有运行根上跑。沿用现有 ID、来源键、时间、主体、证据、删除标记与处理位置，不全量重写历史。

在开放任何生产持久写入前，把删除屏障、写入代次和恢复顺序一起接通。不要先搬事实写入、以后再补防复活。测试中可分模块迁移，正式切换必须作为完整闭环验收。

类型合同按真实调用者的必要字段演进，并用已有测试验证兼容，保留来源与删除语义。

**6. 原代码迁移地图**

以下路径相对于 `F:\game\projectNEKO\N.E.K.O`：

| 方向 | 优先阅读的源文件 |
|---|---|
| 组件装配/恢复/路由 | `app/memory_server/runtime.py`、`routes.py`、`post_turn.py` |
| 数据及日志 | `memory/recent.py`、`timeindex.py`、`event_log.py`、`outbox.py`、`cursors.py` |
| 来源/去重/内容闸门 | `event_sources.py`、`event_classification.py`、`source_event_ledger.py`、`companion_event_ingest.py`、`fact_suppression.py`，均在 memory 下 |
| 更正/遗忘 | `memory/correction_store.py`、`correction_propagation.py`、`deletion_store.py`、`deletion_engine.py`、`scoped_client.py` |
| 成长/人格 | `memory/growth.py`、`reflection/`、`persona/`，特别是 persona/persistence.py 的角色卡同步 |
| 召回/连续性 | `memory/turn_context.py`、`hybrid_recall.py`、`companion_context.py`、`topic_continuity.py`、`episode.py`、`fading.py` |
| 状态与宿主接入 | `memory/companion_state.py`、`main_logic/companion_state.py`、`main_logic/turn_context.py`、`main_routers/companion_router.py` |
| 游戏人格版本 | `main_routers/sts2_companion_router.py`；插件 `service.py` 的 `_host_persona` / `persona_version` 路径 |
| 表现层回归点 | `main_routers/system_router/emotion.py`、`main_logic/core/turn.py`、`static/app/app-websocket.js`、`static/live2d/live2d-emotion.js`、`static/avatar/avatar-reaction-bubble.js` |

这些模块可能已经按包拆分。用 `rg --files` 查真实路径，避免把旧文档中的 `core.py`、`config_manager.py` 当成当前文件。当前 `app/memory_server/`、`utils/cloudsave_runtime/`、`main_routers/characters_router/`、`memory/reflection/`、`memory/persona/` 均为包；持久数据、路由、后台任务与接口的完整 file:line 清单见 design.md §17。

**7. 后续阶段与交付判断**

| 阶段 | 交付内容 | 对应执行批次 |
|---|---|---|
| D0 | 兼容合同、原数据/路由/任务清单、基线 | B01 |
| D1 | 真正的独立记忆闭环、原存储兼容、示例 | B00、B02—B07 |
| D2 | 原宿主接口委托新引擎，旧体验回归 | B08—B09 |
| D3 | 当前卡、版本、用户编辑与安全边界热更新 | B10—B11 |
| D4 | 有证据的人设修订、审核、撤回和删除联动 | B12—B13 |
| D5 | 正式数据切换、实机验收、回滚 | B14—B15 |

每批前置、具体交付、完成条件和实际状态见 roadmap。后续 O/X 系列优化扩展按需求领取，不混入当前 B 批次；首版必要缺陷仍在对应 B 批修复。

不要在同一批同时更换存储、重做召回算法、调成长门槛和修改 Live2D。先行为兼容，再启用自演化。原 plan 的 16 项验收仍是最终标准。

实时客户端的指令更新能力不同：支持在线更新的走确认机制；需要新连接的在原会话交接边界切换，保留输入和播放。不能只更新 manager 的字符串就宣称人格已在实时会话生效。

**8. 开发与验证**

```powershell
cd F:\game\projectNEKO\companion-memory
uv sync
uv run python -m companion_memory
uv run python -m unittest discover -s tests -v
```

已有测试覆盖三类框架风险（来源幂等与冲突、主体/场景隔离、可选召回取消且保留人格）与 B02 持久化风险（重开保持、重试只生效一次、中断恢复、损坏/未知 schema 不覆盖、写许可拒绝）。无需新增测试依赖；后续按迁入模块复用原测试，再补真正缺失的行为用例；需要多少测试就加多少，不再设数量上限。

本轮实际检查：Python 3.11.15；`uv sync`、内存示例、3 个 unittest 均通过；`uv build` 成功生成 sdist 与 wheel，wheel 已在 uv 隔离环境运行示例。Windows 管道中需要明确 UTF-8 时使用 `python -X utf8 -m companion_memory`。这些检查覆盖框架，不包含原记忆系统或宿主接入。B01 轮复核：unittest 3/3 与内存示例仍通过；宿主侧只读核对未修改工作树。B02 轮检查：unittest 15/15 通过（框架 3 + B02 新增 12）；`python -m companion_memory` 输出内存与临时数据根两段，后者 `durable=true`、重试与重开均为 `duplicate`；核心包无宿主导入，数据只在系统临时目录创建。宿主侧测试入口与基线方法见 design.md §17.11/§17.12。B03 轮检查：unittest 29/29 通过（框架 3 + B02 12 + B03 新增 14）；`python -m companion_memory` 输出内存、B02 持久、B03 更正+删除三段，第三段删除后重放同来源返回 `rejected/deleted_source/durable=true`，重开回执仍 completed；`uv build` 成功；核心包无宿主导入，数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。B04 轮检查：unittest 39/39 通过（B03 14 + B04 新增 10：recent 3、classification 3、extraction 4）；`python -m companion_memory` 增加第四段对话→抽取→压缩闭环（固定替身模型，无网络），facts 行含 hash/source/schema_version、压缩 summarized=14、timeindex 原文行保留；`uv build` 成功；核心包无宿主导入，数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。B05 轮检查：unittest 60/60 通过（B04 10 + B05 新增 21：存储 6、反思域 11、人格域 4）；`python -m companion_memory` 增加第五段事实→反思→证据→晋升→成长→删除收窄→growth 撤回→重开闭环（固定替身模型，无网络），persona id 为 `prom_<reflection_id>`/`prom_growth_<sha1>`，删除后反思收窄 `needs_rebuild` 且派生人格被撤回，重开后状态保持；`uv build` 成功；核心包无宿主导入，数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。B06 轮检查：unittest 68/68 通过（B05 60 + B06 新增 8：排序/融合 2、读侧失效 1、指代/episode 1、预算与端口 1、成长块 1、时间召回/降级 1、引擎 build_context 1）；`python -m companion_memory` 增加第六段召回闭环（固定替身嵌入/计数器，无网络），显式召回 hybrid 且带 source_keys、被动上下文命中 2 条、新话题 miss、指代续接带 episode_id、更正后召回 miss 但更正块可见、时间窗召回命中；`uv build` 成功；核心包无宿主导入，数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。B07 轮检查：unittest 76/76 通过（B06 68 + B07 新增 8：状态 3、outbox 1、维护循环 1、引擎 API 1、CLI 2）；`python -m companion_memory` 增加第七段独立闭环（租约+状态提交 version=1、6h 后情绪强度 0.50 且版本不变、outbox processed=1/pending=0、维护 10 步、删除后 retry ok、重开保持）；`python -m companion_memory.cli run --memory-root <临时目录> --character demo --once` 退出码 0，持锁并发退出码 3，nekodata/Documents 路径退出码 2；`uv build` 成功；核心包无宿主导入，数据只在系统临时目录创建，未读写 nekodata、未改宿主/插件。

B08 轮检查（宿主侧，只读新增文件+1 处钩子）：新增 `app/memory_server/engine_bridge/`（ports/runtime/mappers/middleware）并在 `app/memory_server/runtime.py` 加 5 行安装钩子；`pytest tests/unit/test_engine_bridge_*.py -q` 25 passed，宿主基线 38 passed，`git status --porcelain` 110（原 106+4），默认关闭时宿主行为不变；未读 nekodata、未启服务、未切换正式目录。B09 轮检查：宿主必跑 38 passed、26 个定向文件 25 绿 506 passed（唯一既有失败 test_outbox_wiring 并发语言用例，基线即失败）、前端 node 71 pass、engine enabled 集成 25 passed；git 110 前后一致、无 __pycache__、未启端口。B10 轮检查：B10 当批 82/82（B10 新增 6）；八段 demo 第 8 段（当前已扩为十一段）（卡导入/修订/冻结拒绝自动修订/来源删除移除投影/重开保持）；`uv build` 成功；核心无宿主导入。B11 轮检查：`tests.test_b11_publish` 6/6；B11 当批 88/88；九段 demo 第 9 段（当前已扩为十一段）（next_turn/realtime/game 发布、旧消费者 superseded、planner matched/reissue、rollback 后重开保持）；核心无宿主导入。B12 轮检查：`tests.test_b12_evolution` 6/6；B12 当批 94/94；十段 demo 第 10 段（当前已扩为十一段）（模型 2 次调用、提案写入但卡片 revision 不变、request_id duplicate、冻结段落拒绝）；核心无宿主导入。

B13 轮检查：`tests.test_b13_admin` 6/6；十一段 demo 第 11 段（accept 后 revision 1→2、reject 提案、diff 仅 tone、rollback 恢复且 revision 3、开关重开保持）；全量 100/100；核心无宿主导入。B14 轮检查：`migration.py` + CLI 三个子命令；`.scratch/check_b14_migration.py` 44 PASS；Lead 复核临时根 migrate dry-run/migrate/rollback 均 exit 0 且 backup_verified=true；`package-check` ok（wheel 347,469 B/68 文件，隔离 venv import+demo 退出 0，离线无网络）；全量 100/100、十一段 demo、`uv build` 成功；宿主 git 110 未改、未读 nekodata、未启服务。B15 待条件：无部署授权/维护窗口/实机环境，未执行正式切换。

C01—C03 轮检查（2026-10-23）：核心全量 **119/119**；`.scratch/check_c02_c03_e2e.py` exit 0（导入→编辑冲突/成功→发布→四消费者 adopted→再发布 reissue→C03 off/propose/auto→auto 发布→重启保持；final revision 4、version 8b8b63faf5650f99d7f47dd3）；十一段 demo 与 `uv build` 通过；核心无宿主导入。宿主适配器 **47 passed**（B08 25 + C01 5 + C02 5 + flow 5 + C03 7），默认关闭基线 **38 passed**，宿主 `git status` **116**（110 + 6 个新增文件/目录），无 `__pycache__`、未读 nekodata、未启端口。独立复核 `.scratch/c01-c03-review.md` 的 P0（generation 前置、start_maintenance、发布回写）已修复；剩余 C05：实时 ack 实机、主服务↔memory_server 编辑端到端、迁移钩子、性能/副本演练。

C04—C05 轮检查（2026-10-23）：C04 新增 `webui/testbed/` 测试前端与 `webui/tests/test_testbed_api.py`（6/6），`.scratch/check_c04_webui.py` exit 0（看来源→改卡→发布→adopt→提案 accept/withdraw→删除→搜索/导出→重启保持）；现有 `webui/index.html`、`assets/**`、`designs/**` 未改动。C05：`.scratch/check_c05_integration.py` exit 0（11 读端点 200、revise→publish→四消费者 match、propose→accept、delete、只读 403）；`.scratch/check_c05_perf.py` turn_context p50/p95=25.3/27.1ms、recall=22.1/25.5ms、0 失败、前台模型调用 0，snapshot HTTP p50/p95≈618/662ms；`.scratch/check_c05_rehearsal.py` 15 checks PASS（backup→migrate→切换后新增数据→rollback preserve_new→verify，不复活/树哈希一致）；`.scratch/check_c05_versions.py` wheel 374,956B、sdist 523,272B（sdist exclude 修复后 33MB→0.5MB）；`.scratch/c05-a01-a16.md` 映射。核心 **121/121**、十一段 demo、`uv build` 通过；宿主适配器 **49 passed**、默认关闭基线 **38 passed**、宿主 git **117**；未读 nekodata、未启长期端口。剩余 C06：正式切换与实机语音/首字首音/Live2D/游戏/浏览器/真实模型验收。

C06 切换检查（2026-10-23，用户授权）：宿主 .venv 安装 wheel；停服后以 `COMPANION_MEMORY_ENGINE=1` 启动。engine status enabled/available/built=true、legacy_writers_allowed=false；小铃自动导入宿主卡 19 段、card_revision=1、version `card_8d654ff831d8c007`；`turn_context` 返回 persona_version/revision，`get_settings` 返回 1087 字符引擎投影文本；维护 running/writeback running、last_status=completed、evolution_trigger=skipped(off)；48911/48912/48915 健康。迁移钩子首轮 failed：`outbox.ndjson` 第 2 行缺 op_id/type（corrupt），按设计拒绝迁移且未写 manifest/备份；已关闭钩子，数据未修改。修复：首个委托请求自动导入宿主卡、v2 中文 profile + `_reserved` 导入/写回、engine/status 维护可观测；宿主适配器 **51 passed**、默认关闭基线 **38 passed**、宿主 git **117**。回滚：停服去掉环境变量重启。剩余 A01—A16 实机验收（语音/打断、Live2D、游戏、主动、手改/演化、性能）。

开发安装使用独立 `.venv`。交付包应能从构建出的 wheel 在独立环境导入；确认不包含宿主运行依赖。

**写入所有权（S01，2026-10-23 起）**：同一数据根同一时刻只有一个写入进程，经 `persistence/ownership.py` 的 OS 文件锁（`.companion_memory.root.lock`）仲裁。CLI `run`/`migrate`/`rollback`、宿主 `engine_bridge/runtime.py`（`build_core` 取锁，`reload`/`shutdown` 释放）、WebUI 可写在线模式（`build_live_core` 取锁，`close()` 释放）全部纳入；只读入口不取锁。互斥只看 OS 锁——pid/mode 仅供排障，**不要**再引入「pid 不在即清理锁」的逻辑。修改任何新增可写入口时，必须先取得根锁再装配核心，否则本规则失效。

代码遵循 Python 3.11、异步 I/O、明确类型与边界校验。生产模型调用沿用宿主层级配置并有输入/输出预算及 timeout；不写死模型或复制密钥。

**9. 数据、部署与版本管理**

- 当前工作树是新建的独立本地 Git 仓库，无远端。不要向 N.E.K.O upstream 推送或创建 PR。
- 现有运行根（`companion-runtime`）、`nekodata`、锚点根都可以直接读写、清空、重来；测试数据不需要保留，也不强制新建临时根。服务、宿主、管理台不受路径限制；只有独立 `companion-memory` CLI 的 `--memory-root/--backup-root` 仍带代码级路径校验（`cli.py` 的 `validate_data_root`，本轮按要求未改代码），需要直连真实根时走服务/管理台。
- **唯一例外：人格卡不能清空或重建。** 指 `<memory_root>/persona_card.json`、`persona_versions.json`、`persona_admin.json`、`persona_card_archive/` 以及宿主侧投影（`host/memory/<角色>/persona.json`、`host/config/characters.json` 的身份语义段）。这些人格内容只能通过服务的正常编辑、发布、回滚和演化流程修改；其它记忆/日志/队列/回执/配置可以随时删光重来。
- 真正部署前按工作区规则检查服务与游戏状态，安排单写者切换。当前没有部署授权需要执行的步骤。
- 回滚保留切换后新增对话、更正及删除，不能直接恢复旧目录使数据倒退。
- 数据格式升级必须包含版本检测与恢复行为；人设历史和恢复副本也受删除屏障约束。
- 迁入上游源码时保留 Apache-2.0 版权头和适用 NOTICE，记录本地修改；B00 骨架与 B02 存储层均未复制上游运行时代码（按合同重写，沿用字段名与文件格式）。

**10. 接手后的第一条可执行任务**

按 [S05 新计划](docs/s05-new-system-plan.md) 从 P0 核验当前服务修复、A 新配置/启动骨架、B 空库初始化开始，先交付“新入口启动服务和管理台，新建人格后重启仍在”的闭环，再接宿主对话和各消费者。旧基线、旧数据迁移、内嵌模式回退均退出本轮范围。验收使用新根和 N01—N15；完成后只使用新的配置与启动方式。

**独立服务化（S01—S07）**：合同见 [docs/standalone-service.md](docs/standalone-service.md)，状态只在 roadmap §2.2 维护。P0 的 [R01—R07](docs/standalone-service.md#service-review-20261024) **已于 2026-10-24 全部修复并验证**（`tests/test_s05_review_fixes.py` 17 项，走真实服务装配与 HTTP 调用链；核心 213/213），P0 无需重复实现。修复后必须保持：真实删除/更正失效请求结果并清正文（410）；`stop()` 先拒新请求、排空在途、最后释放根锁；HTTP 的 `subject/scene` 贯穿上下文与结果查询；重试复用首次 `occurred_at`；`/chat` 生成前构建记忆上下文且回复进入整轮历史；只有配置/鉴权错误才关闭聊天能力；模型并发由 `threading.BoundedSemaphore` 在工作线程内释放。S03 编码护栏在 `tests/test_s03_encoding.py`，不要重查已定位正确的存储编码；真实时钟重试按 R04 单独处理。S06 核心召回样本在 `tools/s06_*`，S07 按收益决定是否启动。S05 的运行方式、新配置/启动、开发顺序和验收统一以新计划为准。
