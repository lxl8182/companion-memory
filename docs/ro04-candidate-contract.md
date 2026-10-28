# RO04-B 接口准备:四类候选入口合同

更新:2026-10-28。本文件是 **接口合同**,不是实现说明。

对应任务:RO04-B 接口准备(T8)。真正的切换由 T7 完成,T7 之后由 Lead 联合验收。
本文件记录的是本卡交付的三件接口事实:绑定恢复、通用投递、四类候选如何进入新链路。

> **本卡不关闭旧链路。** 旧 `/v1/proactive/*`(poll / use / check / complete / release / observe)
> 继续按现有合同服务所有消费者;插件 `dsh-rin-memory/proactive.js` 一字未改,
> 其中 `:91` 的 `/v1/proactive/poll` 仍是当前的主动消费入口。QQ 退出该消费点由 T7 在插件内完成。

---

## 1. 为什么分两步做

一次拆成两步,是为了避免验收循环:

| 步骤 | 谁做 | 做什么 | 结束条件 |
|---|---|---|---|
| 接口准备(本卡,T8) | bridge | 绑定恢复、通用投递、本文合同 | bridge 行为无变化,新接口有模拟适配器验收 |
| 切换(T7) | 插件 | 候选池、素材、排期、生成,退出旧消费 | 插件侧闭环可跑 |
| 联合验收 | Lead | 单一调度器、四类候选闭环、旧输出有归属 | 端到端通过 |

如果 T8 先把旧消费关掉,新链路未就绪就没有可用路径;如果 T7 又要等 T8 的"切换后完整验收",
两边互等。所以 T8 只准备接口,T7 才切,验收放到最后一次性看。

---

## 2. 绑定恢复接口(bridge 已交付)

### 2.1 事实与边界

身份只有一个来源:bridge 配置里的 admin 身份,经 `AccessGate.isOwner` 判定。
昵称、群名片、`qq-` 前缀、会话字符串形状都不是身份证据,因此恢复接口**不接受**这类输入。

```ts
// bridge-events.ts
interface QqBindingRecord {
  sessionKey: string          // 'private:<userId>' | 'group:<groupId>',必须等于 sessionKeyOf()
  scope: 'private' | 'group'
  owner: true                 // 恒为 true:个人绑定只跟随已校验拥有者
  userId: string | number
  groupId?: string | number
  messageId?: string | number // 确认该绑定的入站平台消息 id,仅用于事后核对
  observedAt?: number
}
```

记录里没有正文:`bindingRecords()` 导出的只有身份字段,入站原话不进这个文件。

### 2.2 调用顺序

```ts
const events = await ctx.get('qqBridgeEvents')       // 桥接未加载时不存在,调用方自己降级

events.setIdentityVerifier(record =>               // 重启后用**当前**配置复核,
  gate.isOwner({ user_id: record.userId }),        // 不信任存档里 owner: true
)
events.setSessionBinder(record =>                  // DSH 会话 id 每次启动都会变:
  executor.prepareSession(record.sessionKey),      // 恢复的是身份,会话要新建
)
```

插件侧(消费方)重启后自己读自己的绑定文件,然后:

```ts
const report = events.restoreBindings(saved)       // 同步,不建会话
// report = { restored, kept, rejected: [{ sessionKey, reason }] }
// 对 rejected 里的 key:删掉本地记录,并等真实入站重新建立身份。

await events.ensureSessions()                      // 显式调用才建会话
// { bound, failed: [{ sessionKey, reason }] }
```

### 2.3 拒绝原因(全部进 rejected,不抛错)

| reason | 含义 | 处理 |
|---|---|---|
| `invalid_scope` | 不是 private/group | 丢弃记录 |
| `not_owner` | 记录自己声明 `owner !== true` | 丢弃记录 |
| `missing_user` / `missing_group` | 平台 id 缺失 | 丢弃记录 |
| `session_key_mismatch` | 会话键与本类生成规则不一致(含昵称、前缀伪造) | 丢弃记录 |
| `identity_unverified` | 没人注册复核钩子 | 接线错误,不是身份结论 |
| `identity_not_owner` | 当前配置已不认这个身份(如改过 admin) | 丢弃记录并等真实入站 |

已由真实入站确认的绑定不会被文件覆盖(`kept`);恢复只补空的。

### 2.4 不接受的行为

- 不从历史消息里推断"可能是主人";
- 不按昵称、群号或会话前缀猜身份;
- 没有合法绑定就不投递,等真实入站 —— `deliverByIndex` 返回 `not_bound`。

---

## 3. 通用投递接口(bridge 已交付)

### 3.1 合同

正文可以来自任何非候选来源(任务结果、约定提醒、新素材、普通补充)。
bridge **不判断该不该发**,只做三件桥接才有资格做的事:确认绑定仍是当前合法主人、
复用同一份 delivery-store 让已送达段不重发、把平台回执按原索引报回去。

```ts
interface QqDeliveryRequest {
  sessionKey: string                 // 已合法绑定的会话
  sessionId?: string                 // 缺省用本进程已绑定的会话
  outputId: string                   // 稳定输出标识:同 id + 同索引重投不会产生第二条消息
  parts: Array<{ index: number; text: string }>   // 索引由核心固定,bridge 不再二次切分
  source?: string                    // 只用于诊断,不影响投递语义
}

interface QqDeliveryOutcome {
  status: 'completed' | 'failed' | 'cancelled' | 'unavailable'
  parts: Array<{ index: number; ok: boolean; platformId?: string; error?: string }>
  pending: number[]                  // 仍未送达的索引(升序):用同一 outputId 续投这些
  error?: string                     // unavailable 时的原因
}
```

### 3.2 与核心 /v1/outputs 的关系

分段边界由 **核心** 决定,不在这里产生第二套切法:

```
① POST /v1/outputs            { text, part_sizes, kind, cause, output_id?, request_id? }
   → { output_id, parts_total, pending_parts: [{ index, chars, offset, text }] }

② bridge.deliverByIndex({ sessionKey, outputId: output_id,
                           parts: pending_parts.map(p => ({ index: p.index, text: p.text })) })
   → { status, parts, pending }

③ POST /v1/outputs/deliver    { output_id, kind: 'sent',
                                parts: parts.filter(p => p.ok).map(p => ({ index: p.index, platform_id: p.platformId })) }
   → 只有被点名的分段进入 common conversation(recent.json)
```

- `part_sizes` 是**正整数列表,总和等于规范正文的 Unicode 码点数**;校验失败返回 400。
- `kind` ∈ `proactive | task_feedback | reply | notice`;默认 `notice`。
- `content_class` 固定 `companion_output`:不进事实提炼、不增加用户偏好支持。
- 部分失败:延续同一 `output_id` 与同一索引集合,只补 `pending`。
- 已过期不再发:`POST /v1/outputs/deliver` 用 `kind: 'interrupted'` 收口,
  或 `POST /v1/outputs/invalidate`(来源被删)清正文留 id。
- 读侧:`GET /v1/outputs?output_id=…` 返回 `pending_parts`;`GET /v1/outputs?pending=1` 列待投递。

bridge 侧另提供只读投影,供重启后对账(不重新生成):

```ts
new DeliveryStore(path).outputView(outputId)
// { output_id, state, parts: [{ index, text, state, platformId?, error? }], pending: [], retryable }
new DeliveryStore(path).pendingOutputs()   // 所有仍可续投的输出
```

`unknown`(平台状态不明)不计入 `pending`,也不自动重发:没有确认的发出去可能已经到达,
重发就是两条气泡。

### 3.3 不复制的东西

| 不属于 bridge | 属于谁 |
|---|---|
| 候选池、选择、去重、冷却 | 插件(T7) |
| 素材缓存与阅读范围 | 插件(T7) |
| 画像 / 兴趣信号 / 「关于主人」段落 | RIN 核心 |
| 正文生成与人格 | 插件 + RIN 上下文 |

bridge 只持有传输事实:谁合法、哪一段到了、还差哪一段。

---

## 4. 四类候选入口合同

四类候选统一进**一个**池、走**一条**投递链路。采集器只产候选,不独立调发送器、不另起循环。

### 4.1 候选字段(T7 侧候选池,与核心现有 `proactive_candidates.json` 对齐)

核心已有字段(可直接重用,不要新建平行结构):

```text
candidate_id / cause / intent / maturity_source / text / why
subject / scene / status / due_at / agreed_at / honor_agreed_time
source_keys[] / fact_ids[] / reflection_ids[]
thread_id / thread_revision / episode_id / output_id
eligibility / merged_into / absorbed_into
cancelled_reason / stale_reason / rewrite_history[]
created_at / updated_at / sent_at
lease_id / lease_owner / lease_session / lease_request / lease_until
```

已有枚举:

```text
cause            : thread_due | agreement | task_result | interest_news | shared_finding
intent           : continue_topic | interest_share | new_idea | creation
maturity_source  : explicit_agreement | task_result | autonomous_inference
status           : pending | postponed | merged | cancelled | sent | stale | invalidated
```

T7 还需另存(不写进核心候选行,属于调度侧):`session_binding`、`material_refs[]`、
`memory_refs[]`、`profile_version`、`persona_version`、`context_basis`、`expires_at`。

### 4.2 四类候选的入口

| 类别 | 触发事实 | `cause` | `intent` | `maturity_source` | 必填来源字段 | 额外要求 |
|---|---|---|---|---|---|---|
| **明确约定** | 主人明确说过"明天面试完告诉我"、约定提醒/问候 | `agreement` | `continue_topic` | `explicit_agreement` | `agreed_at`、`source_keys`(原话)、`honor_agreed_time=true` | 时间锚点来自原话,不用处理时间;主人提前聊过结果 → `cancelled` + `cancelled_reason` |
| **未完事项** | 待关心事项到达跟进时间 | `thread_due` | `continue_topic` | `autonomous_inference` | `thread_id`、`thread_revision`、`due_at`、`episode_id`(有则填) | 需过 M4 资格门(`eligibility`);同一 thread 只保留一个 live 候选 |
| **任务结果** | 后台任务产生真实完成/失败结果 | `task_result` | `new_idea` 或 `creation` | `task_result` | `source_keys`(任务标识)、`output_id`(有正文时) | 只报真实结果,不虚构;失败也要报真实失败原因 |
| **新素材** | 读到未分享、内容足够且仍有效的材料 | `interest_news` | `interest_share` | `autonomous_inference` | `material_refs[]`、`source_keys`(规范 URL)、`eligibility` | 只读到标题时不得生成"看完视频"这类表述;不得写成"用户喜欢 X" |

补充:`shared_finding`(共同经历产生发现)按"新素材"同样处理,只是素材来自对话而非外部来源。

### 4.3 素材记录字段(T7 侧缓存)

```text
material_id            稳定标识(内容指纹的短哈希)
canonical_url          规范 URL
source                 来源(站点/服务)
title / summary        标题、简介
published_at           发布时间
fetched_at             获取时间
read_scope             title | summary | body | transcript   ← 决定允许说什么
topics[]               主题标签
content_fingerprint    内容指纹(去重与失效用)
```

`read_scope` 是硬约束:只到 `title` 就只能说"看到一条讲 X 的",不能说内容细节,
更不能说"看完了"。素材失效(`expires_at` 过、来源删除、指纹不再匹配)时,
对应候选进 `stale` 或 `cancelled`,不静默改成别的话题。

### 4.4 从候选到一次投递

```text
采集器 / 事项推进 / 任务完成
  → 写入候选池(带 cause、maturity_source 与来源引用)
  → 选一个候选并领取租约(candidate_id + lease_id)
  → 一次模型请求:是否说 + 选题与正文 + 下一次安排
      ├─ [PASS]                 → release(reason: model_pass),可同时安排下次
      ├─ 有正文                 → 见下
      └─ 解析不了时间            → 报调度状态,不静默猜时间
  → POST /v1/outputs            取 output_id 与 pending_parts
  → bridge.deliverByIndex       按索引分段投递,拿逐段回执
  → POST /v1/outputs/deliver    只回报已送达片段
  → 计划标记 consumed(已消费必须持久化,重启不复活)
```

选中依据要能落盘:模型选中了哪个 `candidate_id` / `material_id` 必须被保存
(用于标记已用素材、去重、来源校验),不能只拿回正文。选择元数据不发 QQ。

### 4.5 明确的非目标

- 不做第二套候选生成器、第二套发送器、第二套循环;
- 不在 bridge 里判断候选该不该发(见 §3.3);
- 不把模型选择的元数据发到 QQ;
- 不在本卡关闭旧 `/v1/proactive/*`。

---

## 5. 本卡与后续的边界

| 项 | 本卡(T8) | 后续 |
|---|---|---|
| 绑定恢复接口 | ✅ 已交付(bridge + 模拟适配器验收) | 插件侧持久化与调用(T7) |
| 通用投递接口 | ✅ 已交付(按索引、部分失败可续) | 候选来源接入(T7) |
| 候选入口合同 | ✅ 本文 | 实现(T7) |
| 旧 `/v1/proactive/*` 消费 | **不动** | T7 在 `proactive.js` 内退出 |
| 排期 / 一次性唤醒 | 不做 | T7 |
| 联合验收 | 不做 | Lead,在 T7 之后 |

---

## 6. 验收记录(本卡)

模拟适配器,不做真实 QQ 发送。见 `dsh-qq-bridge/test/ro04-bindings.test.ts`(13 项):

- 重启后无新发言:恢复绑定 → 建会话 → 投递两段,全部成功;
- 惰性:只恢复不建会话,未调用 `ensureSessions` 时 binder 未被调用;
- 拒绝猜身份:白名单成员、伪造会话键、`owner: false` 全部进 rejected;
- 未接线时行为不变:走原拒绝路径,旧 `prepare`/`binding` 路径语义不变;
- 分段投递:按索引发送(乱序输入也按索引出气泡),失败段进 `pending`,重启只补未送达段;
- 复查拒绝(`cancelled`)保留正文,稍后可再投;
- 畸形请求在触达传输层前被拒;`unknown` 状态不进 `pending`(不重复气泡)。
