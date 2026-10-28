# RO04-B 四类候选、绑定恢复与唯一投递合同

更新:2026-10-29。T8 的绑定恢复和通用投递接口已在本机完成；T7 的插件候选、联系计划和退出旧消费正在联合验收。计划发行核心 `0.1.0.dev11`、插件 `0.3.0`、bridge `1.0.6-neko.7`；本批尚未同步服务器。

QQ 由插件调度，使用通用 context、outputs 和回执。原 `/v1/proactive/*` 接口继续服务其他消费者；QQ 不再消费其候选。旧已生成输出按稳定 `output_id` 接续或明确取消。

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

插件先通过 bridge 的 `planOutput` 确定规范正文与 `part_sizes`，核心校验并固定该分段；投递时不再切分:

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

## 4. 四类候选与联系计划

四类机会进入插件的同一个持久候选池，并共用一次角色决策和一条投递链路。核心继续保存人格、原话、事实、兴趣依据、输出和回执；插件不写核心的旧候选文件。

| 入口 | 插件候选 | 来源依据 |
|---|---|---|
| 明确约定 | `followup`，附明确约定与时间 | 主人原话、事项引用、原始时间 |
| 未完事项 | `followup` | 当前有效事项、来源引用与下一步 |
| 任务结果 | `task_result` | 真实任务终态或咒语执行结果 |
| 新素材 | `interest_share` 或 `discovery` | 实际获取的素材、阅读范围与获取时间 |

候选保存 `candidate_id/character_id/subject/session_binding/kind/topic/reason/material_refs/memory_refs/profile_version/persona_version/context_basis/created_at/due_at/expires_at/status/output_id`。`profile_version` 记录兴趣依据版本；本批不设独立画像服务。相同主体的稳定认识与相处偏好放在已有的 `about_owner` 人格段落。

素材记录使用 `material_id/url/source/title/description/published_at/fetched_at/read_scope/keywords/fingerprint`。当前列表适配器只承诺标题或简介的阅读范围；零记忆也可探索，分享行为不会写成用户喜好。

角色同一轮决定是否发、选题、正文与下次联系；主请求省略 `tools`。联系元数据示例：

```text
正文
<选题>候选编号</选题>
<下次联系>明天20:30｜想问问面试怎么样了</下次联系>
```

联系标签还支持带时区的绝对时间、`等新进展` 和 `暂停`。元数据在通用咒语前剥离，不进 QQ、终端反馈或事实证据；同一助手消息按 ID 幂等采用。剥离后的同一份正文用于输出存档和 QQ 投递。

普通回复没有新标签时保留仍有效的未来计划；到期领取的计划先记为已消费，无新安排就等待新事件，重启不再次领取旧 revision。只有真实新事件或到期计划才触发联系模型，素材采集器没有独立发送循环。

投递顺序为规范正文 → `/v1/outputs` → `deliverByIndex` → `/v1/outputs/deliver`。已开始的生成和本轮气泡完整结束后再处理新输入，异常或卸载释放 `beginOutput` 的等待句柄。源失效、更正、禁提和过期继续在投递前校验。

## 5. 当前交付边界

| 项目 | 本机状态 |
|---|---|
| 绑定恢复、通用按索引投递、段端口 | 已有模拟适配器与构建验证 |
| 兴趣依据与关于主人段落 | 核心提供来源；段落复用现有人格修订与演化 |
| 统一候选、联系计划、旧输出归属 | 本机联合验收完成，待服务器同步 |
| 主请求零工具、标签隐私、分段回执与重启 | 本批统一核对 |
| 服务器安装 | 本机验收后按固定 Git 提交及新 wheel/tgz 手动同步 |

## 6. 验收记录(本卡)

模拟适配器,不做真实 QQ 发送。见 `dsh-qq-bridge/test/ro04-bindings.test.ts`(13 项):

- 重启后无新发言:恢复绑定 → 建会话 → 投递两段,全部成功;
- 惰性:只恢复不建会话,未调用 `ensureSessions` 时 binder 未被调用;
- 拒绝猜身份:白名单成员、伪造会话键、`owner: false` 全部进 rejected;
- 未接线时行为不变:走原拒绝路径,旧 `prepare`/`binding` 路径语义不变;
- 分段投递:按索引发送(乱序输入也按索引出气泡),失败段进 `pending`,重启只补未送达段;
- 复查拒绝(`cancelled`)保留正文,稍后可再投;
- 畸形请求在触达传输层前被拒;`unknown` 状态不进 `pending`(不重复气泡)。
