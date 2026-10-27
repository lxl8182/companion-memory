# companion-memory

面向不同宿主的记忆与人格演化核心。N.E.K.O 是首个接入方，继续负责对话、语音、情绪分析、Live2D、表情、动作与插件调度；本仓库只做记忆、人格与演化，不实现宿主表现层。

- 当前版本 `0.1.0.dev6`，Python 3.11+，核心运行依赖为零。
- 模型、嵌入、分词、数据根与写入许可由端口注入，核心不导入宿主的任何模块或插件 SDK。
- 许可证：Apache-2.0。

## 能力与交付边界

- 独立记忆流程：原文记录、事实抽取、反思与成长、相关召回、经历关联、状态、后台维护及重启恢复。
- 数据控制：来源与请求幂等、主体/场景隔离、更正、删除屏障、派生内容撤回及防复活。
- 人格核心：当前卡、局部修订、冻结、历史、版本发布、消费回执、修订提案与审核。
- 陪伴连续性：单轮明确教导与撤销、按主题维护反思、独立经历与真实证据资格、助手输出回执、可重建工作摘要、真实状态及主动候选队列。自动演化需要独立经历与交互依据，明确教导使用独立快速路径。
- 交付工具：独立运行 CLI、包检查、格式检测、迁移、备份和保留新增数据的回滚。
- 对话事项与主动交流：同一次后台提炼同时更新事实和事项，支持进展、结案与有原话来源的回访时间；受限 `/v1/proactive/*` 接口供宿主领取、生成、发送前复查与恢复。DSH 主会话保持人格与魔法段、请求不带 `tools`，QQ 分段回执决定哪些文字进入近期记忆。
- 新 stack 已完成非宿主闭环：TOML 配置、同源水彩管理台、真实模型对话、记忆提炼/召回/更正/删除、人格发布/演化、消费回执和重启恢复。已接通 N.E.K.O 的文字对话、已发布人格、记忆回写和有界持久补交；语音/主动/实时/游戏接线继续按 S05 推进。模板默认 `host.enabled=false`，配置宿主后启用。
- 数据策略：本轮为测试开发，现有运行根与 `nekodata` 可直接读写、清空、重来，测试数据无需保留；人格卡不重建，只能经服务的编辑/发布/回滚/演化修改。

实际状态统一维护在 [分批台账](docs/roadmap.md)。[S05 新系统计划](docs/s05-new-system-plan.md) 的非宿主收尾已完成，下一批为语音、主动、实时和游戏消费。C06 已取消。

## 快速开始

### 1. 构建并安装到项目 `.venv`

以下命令都在本仓库根目录执行；Windows + Git Bash / PowerShell 均已验证。

```powershell
uv sync                     # 兼容旧文档；本仓库无第三方运行依赖
uv build --wheel
uv pip install --python .venv/Scripts/python.exe --reinstall dist/companion_memory-0.1.0.dev6-py3-none-any.whl
```

仅首次没有 `.venv` 时先执行 `uv venv .venv --python 3.11`。也可以直接 `uv run` 使用源码树，跳过安装步骤。

### 2. 初始化并启动新实例

```powershell
.\tools\stack.ps1 init   -Config ..\companion-runtime\config\stack.toml
.\tools\stack.ps1 start  -Config ..\companion-runtime\config\stack.toml
.\tools\stack.ps1 status -Config ..\companion-runtime\config\stack.toml
.\tools\stack.ps1 stop   -Config ..\companion-runtime\config\stack.toml
```

管理台默认在 `http://127.0.0.1:48931`。`init` 会写入一份可运行模板；路径相对配置文件解析，运行数据位于源码仓库之外。模板没有模型也能创建人格与发布，在设置页填写端点、档位和凭据后保存立即生效并重启保持，也可通过 `models.service.api_key_env` 引用环境变量（`api_key_env` 是变量**名**，凭据不落库）。升级已有实例时把 `runtime.package_version` 改为已安装版本。`-Python` 可指定解释器，`-NoOpen` 用于后台验收；安装后的 `companion-stack init|start|stop|status --config <绝对路径>` 不依赖工作目录。

管理操作、写请求身份、配置缺项和故障处理见 [管理台说明](docs/webui-console.md)。旧独立 WebUI 脚本保留为开发工具，当前新链路不通过它装配核心。

### 3. 运行与自检

```powershell
uv run python -X utf8 -m companion_memory          # 十一段示例，退出后清理临时数据
uv run python -m unittest discover -s tests -v     # 测试套件
```

示例包含十一段：从内存事件到持久记忆、更正/删除、抽取/压缩、成长、召回、状态维护、人设编辑、版本发布、演化提案和审核回滚。Windows 管道需要明确 UTF-8 时使用 `python -X utf8`。

安装后的 `companion-memory` 运行示例；`companion-memory-run` 是离线开发工具，需显式传 `--memory-root`（该 CLI 仍拒绝 `nekodata`/`Documents` 一类路径；要直连真实根请走服务、宿主或管理台）。新 stack 由服务唯一持有核心，页面复用该实例。

2026-10-22 批次交付记录：核心 100/100 测试、十一段示例、wheel/sdist 构建与隔离安装通过，迁移回滚在临时目录演练通过。宿主默认关闭回归、新引擎适配器测试和实机验收分别记录在台账中。

## 结构与接手入口

| 位置 | 职责 |
|---|---|
| `src/companion_memory/contracts.py`、`ports.py` | 领域合同与外部依赖接口 |
| `src/companion_memory/api.py` | `MemoryEngine` 组合入口 |
| `src/companion_memory/domain/` | 记忆、状态、删除、更正、人设与演化规则 |
| `src/companion_memory/persistence/` | 原格式存储、日志、恢复、版本与屏障 |
| `src/companion_memory/adapters/` | 内存示例、持久事件和显式组件装配 |
| `src/companion_memory/service/`、`stack.py`、`stack_config.py` | 常驻服务、管理 API、新 stack 配置与启停 |
| `src/companion_memory/cli.py`、`migration.py` | 独立入口、迁移与回滚 |
| `tools/stack.ps1` | 新实例 `init/start/stop/status` 包装脚本 |
| `webui/` | 管理台页面与设计预览，复用同一核心服务 |
| `N.E.K.O/app/memory_server/engine_bridge/`（宿主仓库） | N.E.K.O 端口、运行时与路由委托；不在本仓库内 |

领域逻辑只通过明确端口获取宿主能力；核心不导入 N.E.K.O 模块或插件 SDK。管理台通过 API 使用权威引擎，正式数据保持单写者。模型路径、音色、凭据和 Live2D 控制协议由宿主管理，人设演化只修改允许演化的语义段落。

## 文档索引

| 文档 | 内容 |
|---|---|
| [AGENTS.md](AGENTS.md) | 接手规则、源码地图、部署与数据边界 |
| [docs/design.md](docs/design.md) | 架构、行为合同、A01—A16 验收与历史交付 |
| [docs/roadmap.md](docs/roadmap.md) | 唯一进度台账、B 批次和 O/X 优化扩展 |
| [docs/standalone-service.md](docs/standalone-service.md) | 独立服务化方案：运行所有权、对话提交与召回优化 |
| [docs/s05-new-system-plan.md](docs/s05-new-system-plan.md) | S05 新系统开发与验收计划（P0、A—F、N01—N15） |
| [docs/s05-neko-host-next-plan.md](docs/s05-neko-host-next-plan.md) | S05 宿主人格设置、运行状态与文字主动交流接手计划 |
| [docs/webui-console.md](docs/webui-console.md) | 管理台启动、接口与故障处理 |
| [docs/companion-continuity-plan.md](docs/companion-continuity-plan.md) | 记忆与陪伴连续性改造方案 |
| [docs/server-qq-dsh-plan.md](docs/server-qq-dsh-plan.md) | Windows 本机优先的 QQ bridge → DSH 陪伴角色、记忆接入与后续服务器部署计划（Q01—Q06） |
| [docs/remote-memory-host-plan.md](docs/remote-memory-host-plan.md) | 远程记忆宿主方案（A：SSH 隧道 + 服务常驻服务器）；**已确认、暂缓执行** |
| [docs/closeout.md](docs/closeout.md) | 收尾任务、管理台合同、界面风格建议与交接方法 |
| [LICENSE](LICENSE) | Apache-2.0 全文 |

## 许可

本项目采用 Apache-2.0。设计基于本工作区 N.E.K.O 扩展；迁入原实现时保留版权头、适用 NOTICE 和修改说明。
