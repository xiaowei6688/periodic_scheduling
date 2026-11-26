# 前端 WebSocket 对接说明

本文说明前端如何通过 WebSocket 提交结构化周期工单排班请求、展示实时步骤、处理三种人工确认视图，并在确认后取得完整排班结果。

输入计划报文字段见 [输入参数说明](输入参数说明.md)，最终排班数据字段见 [输出结构说明](输出结构说明.md)。

## 1. 接口概览

| 项目 | 说明 |
|---|---|
| 地址 | `/v1/chat/ws` |
| 本地示例 | `ws://localhost:8000/v1/chat/ws` |
| HTTPS 环境 | 使用 `wss://` |
| 数据格式 | 双向 JSON 文本消息 |
| 一次排班流程 | `work_order_schedule` → 流式事件 → `approval_required` → `approval_decision` → `done` |
| 会话标识 | `thread_id`，前端生成并在同一次业务会话中复用 |
| 执行轮次 | `turn_id`，由服务端生成，审批时必须原样带回 |

建议一个浏览器页面只维护一条 WS 连接。同一个 `thread_id` 的请求会串行执行，前端在收到 `done`、`approval_required` 或 `error` 前不要重复提交新的排班请求。

## 2. 认证与连接

浏览器原生 `WebSocket` 不能自定义 `Authorization` 请求头，因此配置了 API Key 时，通过查询参数传递：

```js
const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const token = 'your-api-token';
const url = `${protocol}//${location.host}/v1/chat/ws` +
  `?access_token=${encodeURIComponent(token)}`;
const socket = new WebSocket(url);
```

未配置服务端 `API_KEYS` 时允许本地匿名连接，可以省略 `access_token`。

非浏览器客户端也可以使用请求头：

```http
Authorization: Bearer your-api-token
```

认证失败时，服务端以关闭码 `4401` 断开连接，原因是 `认证失败`。生产环境应使用 `wss://`，并使用短期或受限访问令牌。

## 3. 完整时序

```text
前端                                      服务端
  |                                         |
  |-- work_order_schedule ----------------->|
  |<------------------------------- start --|
  |<-------------------------------- step --|  0..N 条，同一 id 更新状态
  |<------------------------------- delta --|  0..N 条，按 message_id 拼接
  |<------------------- approval_required --|  暂停，等待用户决定
  |                                         |
  |-- approval_decision ------------------->|
  |<----------------------------- resumed --|
  |<-------------------------------- step --|  审批结果及恢复后的步骤
  |<------------------------------- delta --|  恢复后的总结文本
  |<-------------------------------- done --|  完整 Artifact
  |                                         |
```

注意：首次收到 `approval_required` 后不会同时收到 `done`。必须先提交审批决定，流程恢复后才会产生最终 `done`，或者再次产生新的 `approval_required`。

## 4. 提交排班请求

### 4.1 请求结构

```jsonc
{
  "type": "work_order_schedule",
  "thread_id": "schedule-7d10d8e4-5a8e-4a2b-a763-2df6e956f990",
  "payload": {
    "planSetInfo": {},
    "planStrategyList": [],
    "airports": []
  },
  "enforce_quota": true,
  "skip_weekdays": [7],
  "holidays": ["2026-12-01"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 固定为 `work_order_schedule` |
| `thread_id` | string | 建议必填 | 前端会话 ID；同一轮审批必须保持一致 |
| `payload` | object | 是 | 完整排班输入，必须包含 `planSetInfo`、`planStrategyList`、`airports` |
| `enforce_quota` | boolean | 否 | 是否执行配额约束，默认 `true` |
| `skip_weekdays` | number[] / null | 否 | 不排班的星期，取值 `1` 至 `7` |
| `holidays` | string[] / null | 否 | 不排班日期，格式 `YYYY-MM-DD` |

`work_order_schedule` 使用严格字段校验，不要额外传递 `existing_rows`。旧工单由服务端接口分页获取。

完整 `payload` 字段和示例见 [输入参数说明](输入参数说明.md)。可直接用于联调的三类完整数据位于仓库根目录 `mock_data/approval_scenarios/`。

### 4.2 thread_id 规则

前端建议在进入一次新的排班业务时生成 UUID：

```js
const threadId = crypto.randomUUID();
```

- 同一个排班请求及其审批决定必须使用相同的 `thread_id`。
- 新建一轮互不相关的排班时使用新的 `thread_id`。
- 如果请求未传 `thread_id`，服务端会生成会话 ID，并在 `start.thread_id` 中返回；后续审批必须使用该值。

## 5. 服务端事件

所有事件都有 `type`。业务执行事件还会携带 `thread_id` 和 `turn_id`。

### 5.1 start

表示服务端接受请求并开始本轮执行。

```json
{
  "type": "start",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821"
}
```

前端应保存服务端返回的 `thread_id` 和 `turn_id`，并将页面状态切换为“计算中”。

### 5.2 step

表示实时业务步骤。

```json
{
  "id": "8511ce5ee6de4afeb595cf4151dbd230",
  "type": "step",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "agent": "split_and_schedule",
  "title": "执行工单排班规则",
  "status": "in_progress",
  "detail": "新工单 27 张，旧工单 9 张"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 逻辑步骤 ID |
| `agent` | string | 步骤归属方内部标识，仅用于分组 |
| `title` | string | 已处理好的用户可见标题，前端直接展示 |
| `status` | string | `in_progress`、`completed`、`failed` 或 `paused` |
| `detail` | string | 可选的步骤摘要或错误详情 |

同一个步骤会以相同 `id` 先后发送 `in_progress` 和 `completed`/`failed`。前端必须按 `id` 更新原步骤，不能每条事件都新增一行。

```js
const steps = new Map();

function handleStep(event) {
  steps.set(event.id, event);
  renderSteps([...steps.values()]);
}
```

`title` 已经是展示文案，例如“总调度分析任务中”“进行「工单拆分与排班」”，不要根据 `agent` 再拼接内部英文名称。

### 5.3 delta

表示 Agent 的流式文本片段。

```json
{
  "type": "delta",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "message_id": "msg-95d6",
  "agent": "split_and_schedule",
  "agent_display_name": "工单拆分与排班",
  "text": "已完成排班计算"
}
```

前端按 `message_id` 追加 `text`，同一消息不要重复创建气泡：

```js
const messages = new Map();

function handleDelta(event) {
  const content = (messages.get(event.message_id) || '') + event.text;
  messages.set(event.message_id, content);
  renderAgentMessage(event.message_id, event.agent_display_name || '助手', content);
}
```

用户可见名称优先使用 `agent_display_name`，不要展示 `agent` 中的内部英文标识。

### 5.4 approval_required

表示排班计算完成，等待用户确认。WebSocket 返回的是单个扁平审批事件，不是 `approvals` 数组。

```jsonc
{
  "type": "approval_required",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "approval_id": "approval-3e62",
  "agent": "split_and_schedule",
  "agent_display_name": "工单拆分与排班",
  "tool_name": "confirm_work_order_submission",
  "display_title": "确认创建并更新旧单",
  "description": "12 个计划已完成排期……",
  "arguments": {
    "schedule_artifact_id": "artifact-8fa7"
  },
  "display_arguments": {
    "confirmation": {
      "variant": "reworked",
      "title": "确认创建并更新旧单",
      "sectionTitle": "本次确认",
      "summary": "12 个计划已完成排期……",
      "summaryLines": ["12 个计划已完成排期……"],
      "inspectionRange": {
        "start": "2026-11-21",
        "end": "2026-11-22"
      },
      "airportScheduleRange": {
        "start": "2026-11-21",
        "end": "2026-11-22"
      },
      "counts": {
        "planTotal": 12,
        "newTotal": 27,
        "airport": 24,
        "manual": 3,
        "directAffected": 3,
        "indirectAffected": 6,
        "oldUpdates": 9
      },
      "actions": [
        {
          "key": "approve",
          "label": "确认创建并更新旧单",
          "action": "approve",
          "comment": ""
        },
        {
          "key": "cancel",
          "label": "取消",
          "action": "reject",
          "comment": ""
        }
      ],
      "approveLabel": "确认创建并更新旧单",
      "modifyLabel": null,
      "rejectLabel": "取消"
    },
    "scheduleSummary": "计划 12 个 · 新工单 27 张……",
    "scheduleResult": {}
  },
  "fields": []
}
```

排班确认卡应优先读取：

```js
const displayArguments = event.display_arguments || {};
const confirmation = displayArguments.confirmation;
const scheduleResult = displayArguments.scheduleResult;
```

- `confirmation`：确认卡标题、动态总结、统计和按钮定义。
- `scheduleResult`：排期方案、受影响旧工单、影响链和强插建议的数据源。
- `scheduleSummary`：可用于紧凑摘要或日志展示。
- `arguments`：工具调用参数，不适合作为排班详情展示数据。

#### 三种确认视图

| `variant` | 场景 | `actions` |
|---|---|---|
| `ready` | 全部按规则排妥，不更新旧单 | 确认创建机场与人工工单、取消 |
| `reworked` | 存在影响链，旧工单也已重排完成 | 确认创建并更新旧单、取消 |
| `force_insert` | 规则内排不下，已生成强插候选 | 确认强插、修改时间、取消 |

前端必须遍历 `confirmation.actions` 渲染按钮，每个元素结构如下：

```json
{
  "key": "modify_time",
  "label": "修改时间",
  "action": "reject",
  "comment": "修改时间"
}
```

- `key`：稳定的前端按钮标识，可用于样式和埋点。
- `label`：按钮展示文案。
- `action`：提交审批时使用的 `approve` 或 `reject`。
- `comment`：提交审批时原样带回；没有原因时为空字符串。

前端不需要根据 `variant`、按钮文案或工单数量推断审批行为。`approveLabel`、`modifyLabel`、`rejectLabel` 仅作为旧调用方兼容字段，新前端不要使用。

`counts` 会随排班结果动态变化，前端应容忍新增字段，并对缺失数字使用 `0` 作为展示默认值。

### 5.5 resumed

前端提交审批决定后，服务端表示已开始恢复执行：

```json
{
  "type": "resumed",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821"
}
```

收到后将审批按钮保持禁用，并显示“正在处理确认结果”。恢复过程可能继续产生 `step`、`delta`、`approval_required` 或 `done`。

### 5.6 done

表示本轮流程结束。

```jsonc
{
  "type": "done",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "answer": "排班计算已完成……",
  "artifacts": [
    {
      "id": "artifact-8fa7",
      "producer": "split_and_schedule",
      "type": "scheduled_work_orders",
      "data": {
        "planSetInfo": {},
        "planStrategyList": [],
        "oldWorkOrders": [],
        "impactChains": {},
        "entry": "insertRework",
        "forceProposals": [],
        "warnings": []
      }
    }
  ]
}
```

前端从 `artifacts` 中按 `type === 'scheduled_work_orders'` 获取最终排班结果：

```js
const artifact = event.artifacts?.find(
  (item) => item.type === 'scheduled_work_orders'
);
const finalSchedule = artifact?.data;
```

用户选择确认后，前端应等待 `done`，再使用 `finalSchedule` 调用正式的工单创建/旧单更新时间接口。不要仅凭按钮点击就提交业务接口；应以 WS 流程成功完成且取得最终结果为准。

用户选择取消或修改时间时，不调用工单创建接口。修改时间按钮应返回原表单，调整巡检起止日期后重新发起一次新的排班请求。

完整 `data` 字段说明见 [输出结构说明](输出结构说明.md)。

### 5.7 error

表示请求校验、审批校验或运行过程失败。

```json
{
  "type": "error",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "message": "请求参数错误"
}
```

收到 `error` 后应：

1. 结束当前加载状态。
2. 展示 `message`，不要展示原始异常堆栈。
3. 重新启用请求按钮。
4. 不调用正式工单创建接口。

## 6. 提交审批决定

审批请求必须在同一条连接上发送，并原样带回 `approval_id`、`thread_id` 和 `turn_id`。

### 6.1 确认

```json
{
  "type": "approval_decision",
  "approval_id": "approval-3e62",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "action": "approve",
  "values": {},
  "comment": ""
}
```

### 6.2 取消

```json
{
  "type": "approval_decision",
  "approval_id": "approval-3e62",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "action": "reject",
  "values": {},
  "comment": ""
}
```

### 6.3 修改时间

“修改时间”属于拒绝当前排班方案，并携带明确原因：

```json
{
  "type": "approval_decision",
  "approval_id": "approval-3e62",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "action": "reject",
  "values": {},
  "comment": "修改时间"
}
```

### 6.4 动态审批字段

通用审批事件可能带有 `fields`。排班确认目前通常为空数组，但前端组件可按以下结构通用渲染：

```jsonc
{
  "name": "reason",
  "label": "确认原因",
  "type": "text",
  "required": true,
  "placeholder": "请输入",
  "options": [
    {
      "value": "value-1",
      "label": "选项一",
      "description": "选项说明"
    }
  ]
}
```

支持的 `type`：`text`、`select`、`radio`、`multi_select`。确认时把表单值放入 `approval_decision.values`；取消时传空对象即可。

## 7. 前端状态机建议

| 当前状态 | 进入事件/动作 | 下一状态 |
|---|---|---|
| `idle` | 发送 `work_order_schedule` | `starting` |
| `starting` | 收到 `start` | `running` |
| `running` | 收到 `step` / `delta` | `running` |
| `running` | 收到 `approval_required` | `awaiting_approval` |
| `awaiting_approval` | 发送 `approval_decision` | `resuming` |
| `resuming` | 收到 `resumed` / `step` / `delta` | `resuming` |
| `running` / `resuming` | 收到 `done` | `completed` |
| 任意执行态 | 收到 `error` 或连接异常 | `failed` |

建议规则：

- `awaiting_approval` 时保留 WS 连接，不要自动重发原排班请求。
- 审批按钮点击一次后立即禁用，避免同一 `approval_id` 重复提交。
- WS 事件不保证断线后重放；连接中断时提示用户重新进入或重新提交，不要自动重复创建业务工单。
- 只有 `done` 且存在 `scheduled_work_orders` Artifact 时，才进入正式业务提交阶段。

## 8. 可直接使用的 JavaScript 示例

```js
class ScheduleWsClient {
  constructor({ baseUrl, token, onState, onStep, onMessage, onApproval, onDone, onError }) {
    this.baseUrl = baseUrl;
    this.token = token;
    this.handlers = { onState, onStep, onMessage, onApproval, onDone, onError };
    this.steps = new Map();
    this.messages = new Map();
    this.threadId = '';
    this.turnId = '';
    this.pendingApproval = null;
    this.lastApprovalAction = null;
  }

  connect() {
    return new Promise((resolve, reject) => {
      const tokenQuery = this.token
        ? `?access_token=${encodeURIComponent(this.token)}`
        : '';
      this.socket = new WebSocket(`${this.baseUrl}/v1/chat/ws${tokenQuery}`);

      this.socket.onopen = () => resolve();
      this.socket.onerror = () => reject(new Error('WebSocket 连接失败'));
      this.socket.onclose = (event) => {
        this.handlers.onState?.('disconnected');
        if (event.code === 4401) {
          this.handlers.onError?.('认证失败');
        }
      };
      this.socket.onmessage = ({ data }) => this.handleEvent(JSON.parse(data));
    });
  }

  sendSchedule({ threadId, payload, enforceQuota = true, skipWeekdays, holidays }) {
    this.threadId = threadId || crypto.randomUUID();
    this.steps.clear();
    this.messages.clear();
    this.pendingApproval = null;
    this.lastApprovalAction = null;
    this.handlers.onState?.('starting');

    this.send({
      type: 'work_order_schedule',
      thread_id: this.threadId,
      payload,
      enforce_quota: enforceQuota,
      ...(skipWeekdays?.length ? { skip_weekdays: skipWeekdays } : {}),
      ...(holidays?.length ? { holidays } : {}),
    });
  }

  decide(action, { comment = '', values = {} } = {}) {
    const approval = this.pendingApproval;
    if (!approval) throw new Error('当前没有待确认的排班方案');

    this.lastApprovalAction = action;
    this.handlers.onState?.('resuming');
    this.send({
      type: 'approval_decision',
      approval_id: approval.approval_id,
      thread_id: approval.thread_id,
      turn_id: approval.turn_id,
      action,
      comment,
      values,
    });
    this.pendingApproval = null;
  }

  send(data) {
    if (this.socket?.readyState !== WebSocket.OPEN) {
      throw new Error('WebSocket 尚未连接');
    }
    this.socket.send(JSON.stringify(data));
  }

  handleEvent(event) {
    if (event.thread_id) this.threadId = event.thread_id;
    if (event.turn_id) this.turnId = event.turn_id;

    switch (event.type) {
      case 'start':
        this.handlers.onState?.('running');
        break;
      case 'step':
        this.steps.set(event.id, event);
        this.handlers.onStep?.(event, [...this.steps.values()]);
        break;
      case 'delta': {
        const text = (this.messages.get(event.message_id) || '') + event.text;
        this.messages.set(event.message_id, text);
        this.handlers.onMessage?.({ ...event, text });
        break;
      }
      case 'approval_required':
        this.pendingApproval = event;
        this.handlers.onState?.('awaiting_approval');
        this.handlers.onApproval?.({
          event,
          confirmation: event.display_arguments?.confirmation,
          schedule: event.display_arguments?.scheduleResult,
        });
        break;
      case 'resumed':
        this.handlers.onState?.('resuming');
        break;
      case 'done': {
        const artifact = event.artifacts?.find(
          (item) => item.type === 'scheduled_work_orders'
        );
        this.handlers.onState?.('completed');
        this.handlers.onDone?.({
          event,
          schedule: artifact?.data || null,
          approved: this.lastApprovalAction === 'approve',
        });
        break;
      }
      case 'error':
        this.handlers.onState?.('failed');
        this.handlers.onError?.(event.message || '排班失败');
        break;
    }
  }
}

const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const client = new ScheduleWsClient({
  baseUrl: `${protocol}//${location.host}`,
  token: sessionStorage.getItem('apiToken') || '',
  onStep: (_event, steps) => renderSteps(steps),
  onMessage: (message) => renderMessage(
    message.message_id,
    message.agent_display_name || '助手',
    message.text,
  ),
  onApproval: ({ event, confirmation, schedule }) => {
    renderApprovalCard({ event, confirmation, schedule });
  },
  onDone: async ({ schedule, approved }) => {
    if (approved && schedule) await submitScheduleToBusinessApi(schedule);
  },
  onError: (message) => showError(message),
});

await client.connect();
client.sendSchedule({ threadId: crypto.randomUUID(), payload });

function renderApprovalActions(container, confirmation) {
  container.replaceChildren();
  for (const action of confirmation.actions || []) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = action.label;
    button.dataset.actionKey = action.key;
    button.onclick = () => client.decide(action.action, {
      comment: action.comment || '',
    });
    container.append(button);
  }
}
```

实际项目中应在用户点击确认后记录“待正式提交”意图，并只在该次流程的 `done` 回调中调用 `submitScheduleToBusinessApi`。取消或修改时间时不要调用该业务接口。

## 9. 联调检查清单

- 能连接 `/v1/chat/ws`，认证失败能识别关闭码 `4401`。
- 请求包含 `planSetInfo`、`planStrategyList`、`airports`。
- `step` 按 `id` 更新，不重复追加同一逻辑步骤。
- `delta` 按 `message_id` 拼接，并展示 `agent_display_name`。
- `approval_required` 按单个扁平事件处理，不读取 `approvals[]`。
- 遍历 `display_arguments.confirmation.actions` 渲染按钮。
- 按按钮的 `action` 和 `comment` 提交决定，不在前端推断。
- 审批决定原样带回 `approval_id`、`thread_id`、`turn_id`。
- 仅在确认后的 `done` 中读取 `scheduled_work_orders` 并调用业务接口。
- `error`、断线和取消场景不会触发正式工单创建。

