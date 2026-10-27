# 远程记忆宿主方案（A：SSH 隧道 + 服务常驻服务器）

> 状态：**已确认，暂缓执行**。等记忆系统本身优化到位后再按本文归一。
> 确认日期：2026-10-27
> 范围：**只把记忆系统搬到服务器**。DSH 桌面端、QQ bridge、N.E.K.O 桌宠壳继续留在本机，
> 它们作为**客户端**连过来，不迁移、不改架构。

---

## 1. 目标与边界

### 1.1 要达成的

- `rin-memory` 服务常驻服务器，**人格卡、事实、召回、更正删除、发布版本**只有服务器上这一份。
- 服务器上的 `memory/`、`host/`、`state/` 是**唯一权威数据根**，本机不再保留副本。
- N.E.K.O（本机）与 DSH 记忆插件（本机）都通过隧道连过去，行为与今天完全一致。
- 管理台仍可在本机浏览器打开，用的还是 `http://127.0.0.1:48931`。

### 1.2 明确不做的

| 不做 | 原因 |
|---|---|
| 改 `service.host` 校验、`allowed_hosts` | 方案 A 靠隧道，服务继续只听回环，**代码一行不改** |
| 给服务加 TLS / 反向代理 | 服务没有 TLS 代码；隧道本身已加密，不需要再叠一层 |
| 暴露任何公网端口 | 全部流量走 SSH，服务器不需要开放 48931 |
| 迁移 DSH / QQ bridge / N.E.K.O 本体 | 按用户要求只接记忆系统 |
| 改 QQ bridge 的 `127.0.0.1` 假设 | 它连的是 NapCat 和本地 DSH，与记忆服务无关 |

### 1.3 为什么选 A 而不是「直接监听内网地址」

服务当前有三道回环硬约束：

| 位置 | 约束 |
|---|---|
| `src/rin_memory/stack_config.py:157` | `if host != "127.0.0.1": raise StackConfigError(...)` |
| `src/rin_memory/service/server.py:1025` | `allowed_hosts` 写死 `("127.0.0.1","localhost","::1")` |
| `dsh-rin-memory/config.js:115`、`memory-client.js:123` | 两处 `hostname !== '127.0.0.1'` 直接抛错 |

**隧道方案让客户端的视角仍然是 127.0.0.1**，上述 4 处全部不用碰。这是它比「改绑定地址」更省、
也更容易回滚的根本原因。N.E.K.O 侧本来就只读配置里的 `url` 字段
（`main_logic/remote_memory.py`，`NEKO_COMPANION_CONFIG` 指向的 JSON），天然零改动。

---

## 2. 目标拓扑

```
        ┌──────────────────── 服务器（常驻） ────────────────────┐
        │  rin-memory 服务  127.0.0.1:48931               │
        │  ├─ memory/  host/  state/   ← 唯一数据根              │
        │  └─ state/client_tokens.json ← 客户端令牌             │
        └───────────────────────▲───────────────────────────────┘
                                │ SSH（加密，不开放公网端口）
                                │ ssh -L 48931:127.0.0.1:48931
        ┌───────────────────────┴───────────────────────────────┐
        │ 本机 Windows                                           │
        │  ├─ N.E.K.O  ──────────► http://127.0.0.1:48931        │
        │  ├─ DSH 记忆插件 ──────► http://127.0.0.1:48931        │
        │  └─ 浏览器管理台 ──────► http://127.0.0.1:48931        │
        └────────────────────────────────────────────────────────┘
```

客户端看到的永远是 `127.0.0.1:48931`，与现在的配置**完全相同**。

---

## 3. 执行步骤（等真正动手时按序做）

### 步骤 0：备份与冻结

```powershell
# 本机停服务与本机宿主
cd F:\game\projectNEKO\rin-memory
.\tools\stack.ps1 stop -Config F:\game\projectNEKO\companion-runtime\config\stack.toml

# 整目录备份（memory 11 MB + host 65 MB + logs 3 MB）
Compress-Archive -Path F:\game\projectNEKO\companion-runtime\* `
  -DestinationPath F:\game\projectNEKO\companion-runtime-backup-$(Get-Date -Format yyyyMMdd-HHmm).zip
```

迁移期间**本机服务必须停**，否则会出现两个写者。

### 步骤 1：服务器准备

```bash
# 选一个长期目录，例如 /srv/rin-memory
sudo mkdir -p /srv/rin-memory && sudo chown "$USER" /srv/rin-memory

# 取源码（仓库已公开）
git clone https://github.com/lxl8182/rin-memory.git /srv/rin-memory/src
cd /srv/rin-memory/src
uv build --wheel
uv venv .venv --python 3.11
uv pip install --python .venv/bin/python dist/rin_memory-0.1.0.dev7-py3-none-any.whl
```

> `tools/stack.ps1` 是 PowerShell 包装，**服务器上用已安装的 `rin-stack` CLI 代替**：
> `rin-stack init|start|stop|status --config <绝对路径>`。该 CLI 不依赖工作目录。

### 步骤 2：迁移数据根

把本机的 `memory/`、`host/`、`state/` 整体传到服务器（`logs/` 可不带）：

```bash
# 本机执行
scp -r F:/game/projectNEKO/companion-runtime/memory \
       F:/game/projectNEKO/companion-runtime/host \
       F:/game/projectNEKO/companion-runtime/state \
       user@server:/srv/rin-memory/
```

服务器上确认 `state/client_tokens.json` 存在且权限为 600：

```bash
chmod 600 /srv/rin-memory/state/client_tokens.json
```

该文件含两个键（值是从未提交过的令牌）：

- `COMPANION_NEKO_TOKEN` → 给 `[clients.neko-desktop]`
- `COMPANION_DSH_TOKEN` → 给 `[clients.dsh-desktop]`

### 步骤 3：服务器端 stack.toml

以本机 `companion-runtime/config/stack.toml` 为模板，**必须改这三处**：

| 字段 | 本机值 | 服务器值 | 为什么 |
|---|---|---|---|
| `runtime.python` | `F:\...\Scripts\python.exe` | `/srv/rin-memory/src/.venv/bin/python` | 平台路径 |
| `runtime.package_version` | `0.1.0.dev4` | 与服务器实际安装版本一致 | 不一致会直接启动失败 |
| `host.enabled` | `true` | **`false`** | 否则会尝试拉起 N.E.K.O 宿主；服务器上没有 N.E.K.O，必然失败 |

`[service] host` 保持 `'127.0.0.1'`、`port` 保持 `48931`。`[clients.*]` 授权段原样保留。

### 步骤 4：起服务

```bash
rin-stack init   --config /srv/rin-memory/config/stack.toml
rin-stack start  --config /srv/rin-memory/config/stack.toml
rin-stack status --config /srv/rin-memory/config/stack.toml
curl -s http://127.0.0.1:48931/v1/health
```

### 步骤 5：常驻化

SSH 隧道会断，服务不能跟着断。用 systemd 托管服务本体；隧道由客户端侧负责重连。

```ini
# /etc/systemd/system/rin-memory.service
[Unit]
Description=rin-memory service
After=network-online.target

[Service]
Type=simple
User=youruser
ExecStart=/srv/rin-memory/src/.venv/bin/rin-stack start --config /srv/rin-memory/config/stack.toml
ExecStop=/srv/rin-memory/src/.venv/bin/rin-stack stop --config /srv/rin-memory/config/stack.toml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> 注意：`rin-stack start` 自身会拉起前台进程并等到就绪，用它做 `Type=simple` 前
> 需实测确认不会立刻返回；若会返回，改用 `Type=forking` 或在服务内部加 `--foreground`。
> **这一步动手时先验证，不要照抄。**

### 步骤 6：客户端建立隧道

```powershell
# 本机，保持常驻
ssh -N -L 48931:127.0.0.1:48931 user@server
```

断线自动重连（任一即可）：

- Windows 计划任务 + `autossh`（WSL 或 Git for Windows 均带 ssh，autossh 需另装）
- 用 `ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3` 降低假死
- 开机自启：`shell:startup` 放一个 `.cmd` 启动上面这条命令

验证：

```powershell
curl -s http://127.0.0.1:48931/v1/health
```

### 步骤 7：两端配置

**N.E.K.O**：`companion-runtime/host/companion_host.json` 的 `url` **不用改**
（已经是 `http://127.0.0.1:48931`），`token` 与 `state/client_tokens.json` 的
`COMPANION_NEKO_TOKEN` 一致即可。`host_root` 与 `state_root` 若指向本机路径，
需确认它们只用于本机补交队列（那是 N.E.K.O 侧状态，**应留在本机**，不迁服务器）。

**DSH 记忆插件**：`cordis.patch.yml` 里 `url: http://127.0.0.1:48931` **不用改**。
令牌二选一：

```yaml
tokenEnv: COMPANION_DSH_TOKEN       # 进程环境变量
tokenFile: F:\...\dsh_tokens.json  # 或本机放一个只含该键的 JSON
```

`tokenFile` 只读取 `tokenEnv` 指向的那一个键，本机放一份只有
`{"COMPANION_DSH_TOKEN": "..."}` 的副本即可，不需要整份服务器令牌文件。

---

## 4. 验收清单

| # | 检查 | 期望 |
|---|---|---|
| 1 | 服务器 `curl http://127.0.0.1:48931/v1/health` | 200，角色 id 与人格一致 |
| 2 | 本机隧道后同样的 curl | 与 1 相同 |
| 3 | 管理台打开 `http://127.0.0.1:48931` | 人格卡、事实、发布页数据都在 |
| 4 | N.E.K.O 发一句话 | 正常回复，且服务器 `memory/` 有新增写入 |
| 5 | DSH 记忆插件状态 | `connected`，不是 `rejected`/`disconnected` |
| 6 | 在管理台更正/删除一条记忆 | 两端后续回答都不再带旧内容 |
| 7 | 断开隧道再恢复 | 客户端进入待补交，恢复后按序补齐，不重复 |
| 8 | 服务器重启后 | 服务自动起来，数据完整，客户端自动重连 |
| 9 | 本机 `companion-runtime/memory` | **确认不再被写入**（避免双写） |

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| **双写**：本机服务忘了停，两边各写一份 | 步骤 0 强制停服；验收项 9 专门检查；真上线后建议把本机 `memory/` 改名归档 |
| SSH 断线导致长时间不可用 | `ServerAliveInterval` + autossh；客户端有持久补交队列（`maxPending` 有界，满了明确报错不静默丢） |
| 令牌泄漏 | `client_tokens.json` 权限 600，不进 Git（已确认从未提交）；本机副本只放自己那一个键 |
| 控制台令牌每次重启重生成 | `ManagementApi.token = secrets.token_urlsafe(32)`，进程重启即失效。**远程管理台因此要重新登录**，这是已知行为，不是 bug |
| 服务器没有图形环境 | 服务是纯 HTTP + 管理台静态页，无 GUI 依赖；`presentation` 段（Live2D/语音）在服务器上不生效，也不需要 |
| 时区/编码差异 | 服务器设 `TZ`；数据文件是 UTF-8 JSON，已验证 |
| 模型调用出口 | 服务器需要能访问 `models.service.base_url`；若本机有代理而服务器没有，需在服务器侧配置 |

---

## 6. 回滚

1. 客户端停隧道、停 DSH 插件的 `enabled`、N.E.K.O 停服。
2. 服务器 `rin-stack stop`，把 `memory/`、`host/`、`state/` 取回本机。
3. 本机 `stack.toml` 的 `host.enabled` 改回 `true`，`runtime.python` 改回 Windows 路径。
4. `.\tools\stack.ps1 start`，跑验收项 1–6。

因为**代码零改动**，回滚就是换数据根，没有版本兼容问题。

---

## 7. 以后可能想升级的点（不在本方案内）

如果哪天嫌 SSH 隧道要手工保活，可以换成 Tailscale 直连 —— 但那**必须改代码**：
`stack_config.py` 的 host 校验、`server.py` 的 `allowed_hosts`、DSH 插件两处 loopback 判断，
外加把 `allow_origin` 从 CLI-only 提升到 `stack.toml`。约半天。**现在不做。**

公网 HTTPS 另需：TLS 终结（反代）、令牌轮换、限流、审计、备份策略。
项目文档已把这个列为后续项：`docs/standalone-service.md:241`、`docs/s05-new-system-plan.md:39`。

---

## 8. 与记忆系统优化工作的关系

本方案**刻意不碰记忆逻辑**。等优化完成后再执行，好处是：

- 优化期间数据仍在本机，调试、导出、重置都最方便（`AGENTS.md` §13 允许随时清空重建）。
- 优化完成后只做一次搬迁，不用来回搬两趟。
- 优化如果改了数据格式，迁移在格式稳定后进行，减少一次转换。

**执行前需重新确认的事实**：服务器上安装的版本号要与 `runtime.package_version` 对齐，
且 `host.enabled` 必须为 `false`。这两点是最容易踩的坑。
