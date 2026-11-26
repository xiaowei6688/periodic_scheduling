# 前端 WebSocket 对接说明

WebSocket 地址：`/v1/chat/ws`。

本文只说明前端传参、事件类型、事件字段和取值方式。业务报文字段见 [输入参数说明](输入参数说明.md)，排班结果字段见 [输出结构说明](输出结构说明.md)。

## 1. 前端传参

### 1.1 发起排班

```jsonc
{
  "type": "work_order_schedule",
  "thread_id": "schedule-7d10d8e4",
  "payload": {
    "planSetInfo": {},
    "planStrategyList": [],
    "airports": []
  },
  "message": "优先关注本次窗口内的机场作业冲突",
  "enforce_quota": true,
  "skip_weekdays": [7],
  "holidays": ["2026-12-01"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 固定为 `work_order_schedule` |
| `thread_id` | string | 建议 | 前端生成的会话 ID；后续审批继续使用 |
| `payload` | object | 是 | 完整计划报文 |
| `payload.planSetInfo` | object | 是 | 计划集信息 |
| `payload.planStrategyList` | array | 是 | 计划策略及子计划 |
| `payload.airports` | array | 是 | 机场台账 |
| `message` | string | 否 | 本次排班的上下文说明，最长 2000 个字符 |
| `enforce_quota` | boolean | 否 | 是否执行配额约束，默认 `true` |
| `skip_weekdays` | number[] / null | 否 | 不排班的星期，取值 `1` 至 `7` |
| `holidays` | string[] / null | 否 | 不排班日期，格式 `YYYY-MM-DD` |

不要传 `existing_rows`，旧工单由服务端分页获取。

### 1.2 提交审批决定

收到 `approval_required` 后，使用用户所点按钮的数据发送：

```jsonc
{
  "type": "approval_decision",
  "approval_id": "approval-3e62",
  "thread_id": "schedule-7d10d8e4",
  "turn_id": "6e27ebccfb174a149a9fc87b48f1d821",
  "action": "approve",
  "comment": "",
  "values": {}
}
```

| 字段 | 类型 | 必填 | 取值方式 |
|---|---|---|---|
| `type` | string | 是 | 固定为 `approval_decision` |
| `approval_id` | string | 是 | `approval_required.approval_id` |
| `thread_id` | string | 是 | `approval_required.thread_id` |
| `turn_id` | string | 是 | `approval_required.turn_id` |
| `action` | string | 是 | 所点 `actions[]` 项的 `action` |
| `comment` | string | 否 | 所点 `actions[]` 项的 `comment` |
| `values` | object | 否 | 动态表单值；排班确认通常传 `{}` |

## 2. 事件类型

| `type` | 含义 | 前端处理 |
|---|---|---|
| `start` | 开始执行 | 保存 `thread_id`、`turn_id` |
| `step` | 步骤进度 | 按 `id` 新增或更新步骤 |
| `delta` | 流式文本 | 按 `message_id` 追加 `text` |
| `approval_required` | 等待人工确认 | 展示总结、结果和动态按钮 |
| `resumed` | 审批后恢复 | 显示处理中状态 |
| `done` | 本轮结束 | 读取最终排班 Artifact |
| `error` | 执行失败 | 展示 `message` 并结束加载 |

## 3. 事件字段与取值

所有事件都可能包含 `type`、`thread_id` 和 `turn_id`。

### 3.1 `start`

保存会话信息，审批时原样传回：

```js
threadId = event.thread_id;
turnId = event.turn_id;
```

### 3.2 `step`

| 字段 | 说明 |
|---|---|
| `id` | 逻辑步骤 ID；相同 ID 表示同一步骤 |
| `title` | 用户可见步骤名称 |
| `status` | `in_progress`、`completed`、`failed` 或 `paused` |
| `detail` | 步骤摘要或错误详情，可能为空 |
| `agent` | 内部归属标识，不作为用户可见名称 |

相同步骤会重复发送，按 `id` 覆盖：

```js
steps.set(event.id, event);
```

### 3.3 `delta`

| 字段 | 说明 |
|---|---|
| `message_id` | 消息 ID |
| `text` | 本次新增文本片段，不是完整消息 |
| `agent_display_name` | 用户可见 Agent 名称 |
| `agent` | Agent 内部标识 |

按 `message_id` 拼接文本，名称展示 `agent_display_name`：

```js
const oldText = messages.get(event.message_id) || '';
messages.set(event.message_id, oldText + event.text);
const displayName = event.agent_display_name;
```

### 3.4 `approval_required`

| 字段 | 说明 |
|---|---|
| `approval_id` | 审批 ID，提交决定时原样传回 |
| `display_title` | 审批卡标题 |
| `description` | 动态审批总结 |
| `display_arguments.confirmation` | 确认视图、统计和按钮数据 |
| `display_arguments.scheduleSummary` | 简短排班摘要 |
| `display_arguments.scheduleResult` | 完整排班结果 |
| `fields` | 动态表单定义；排班确认通常为空数组 |

`confirmation` 主要字段：

| 字段 | 说明 |
|---|---|
| `variant` | `ready`、`reworked` 或 `force_insert` |
| `title` | 确认标题 |
| `summary` / `summaryLines` | 完整总结 / 分段总结 |
| `inspectionRange` | 巡检日期范围 |
| `airportScheduleRange` | 机场工单实际排期日期范围 |
| `counts` | 计划、工单、影响旧单等数量 |
| `actions` | 前端需要展示的按钮列表 |

`actions[]` 包含 `key`、`label`、`action`、`comment`。前端不自行判断按钮，直接遍历：

```js
const args = event.display_arguments;
const confirmation = args.confirmation;
const scheduleSummary = args.scheduleSummary;
const scheduleResult = args.scheduleResult;

for (const button of confirmation.actions) {
  renderButton(button.label, () => sendApproval({
    approval_id: event.approval_id,
    thread_id: event.thread_id,
    turn_id: event.turn_id,
    action: button.action,
    comment: button.comment,
    values: {}
  }));
}
```

| `variant` | 按钮 |
|---|---|
| `ready` | 确认创建机场与人工工单、取消 |
| `reworked` | 确认创建并更新旧单、取消 |
| `force_insert` | 确认强插、修改时间、取消 |

### 3.5 `resumed`

审批决定已接收，服务端正在继续执行，无额外业务字段。

### 3.6 `done`

| 字段 | 说明 |
|---|---|
| `answer` | 最终文本总结 |
| `artifacts` | 结构化结果列表 |
| `artifacts[].type` | Artifact 类型 |
| `artifacts[].data` | Artifact 完整数据 |

取得最终排班结果：

```js
const artifact = event.artifacts?.find(
  (item) => item.type === 'scheduled_work_orders'
);
const finalSchedule = artifact?.data;
```

只有用户点击的按钮 `action === "approve"`，并随后收到包含 `scheduled_work_orders` 的 `done` 时，前端才使用 `finalSchedule` 调用正式业务接口。取消或修改时间时不要创建工单。

### 3.7 `error`

`message` 是可直接展示的错误信息。收到后结束加载状态，不调用正式工单创建接口。
