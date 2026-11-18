# 周期计划 → 工单拆分

把客户 `WorkOrderInfoServiceImpl.addOrderByPlanNew` 的**拆单**逻辑翻成 Python。

只做一件事：**把一条计划拆成若干张工单，每张工单保证「存在一个执行日能做完」。**
不选日期、不查占用、不做超量均摊，所以工单的 `start_date` / `end_date` 一律为 `null`。

**不访问任何数据库。** Java 里要查三张表才能拿到的数据（设备台账 / 机场台账 / 承载力配置），
全部随输入报文一起传进来。

```bash
python build_mock.py                             # 生成 mock_data/split_input.json
python main.py                                   # 完整流程：拆单 → 取旧单 → 重排
python -m unittest discover -p "test_*.py" -v    # 全量回归
```

---

## 1. 输入：一个自包含报文

```python
from split_work_orders import split_all, verify

orders, details, results = split_all(payload)   # payload 是 dict
problems = verify(results)                       # 空列表 = 守恒校验通过
```

报文结构（在你的计划报文基础上只加了两块，其余是 `deviceList` 上补字段）：

```jsonc
{
  "planSetInfo": { ... },                            // ← 你原有，不用动
  "planStrategyList": [                              // ← 你原有，deviceList 补 6 个字段
    {
      "planStrategyGuid": "...",
      "jobObjectGuid": "...",                        // 线路 GUID
      "isLine": "0",                                 // 原样保留，拆单不读它
      "timePeriod": "08:30:00-12:30:00;12:45:00-18:30:00",
      "deviceList": [
        {
          "jobObjectDeviceGuid": "tw-001",           // ← 原有
          "jobObjectDeviceName": "苍岭线001号塔",      // ← 原有
          "span": "880",                             // ← 原有（档距米数，通道用）
          "coveringAirports": "ap-a,ap-b",           // ★ 新增：台账覆盖机场，逗号分隔，首位=最优
          "filter": 0,                               // ★ 新增：机场过滤位
          "filterTd": 0,                             // ★ 新增：通道过滤位
          "isDeleted": 0,                            // ★ 新增：台账删除标记
          "sort": 1,                                 // ★ 新增：装箱顺序（缺省用数组下标）
          "psrEquipCode": "PSR-001"                  // ★ 新增：设备编码（写进明细）
        }
      ],
      "planList": [ { ... } ]                        // ← 你原有，不用动
    }
  ],
  "airports": {                                      // ★ 新增：机场台账
    "ap-a": { "airportName": "苍岭机场A", "airportSn": "SN-M350H-001", "airportModel": "M350H" }
  },
  "capacityTable": [                                 // ★ 新增：承载力配置
    { "airportModel": "M350H", "speciality": "tms", "infrared": false,
      "item": "本体", "voltage": "110", "capacity": 4.0 }
  ]
}
```

### 1.1 必须新增的字段清单

| 位置 | 字段 | Java 里的来源 | 作用 |
|---|---|---|---|
| `deviceList[]` | `coveringAirports` | `DeviceTmsTowerInfo.coveringAirports` | 分机场；首位机场是唯一可执行机场 |
| `deviceList[]` | `filter` | `filter` | 机场过滤位，非 0 转人工 |
| `deviceList[]` | `filterTd` | `filterTd` | 通道专用过滤位 |
| `deviceList[]` | `isDeleted` | `isDeleted` | 非 0 的设备跳过 |
| `deviceList[]` | `sort` | `sort` | 装箱顺序，缺省用数组下标 |
| `deviceList[]` | `psrEquipCode` | `psrEquipCode` | 写进工单明细 |
| 顶层 | `airports` | 查 `EquipAirportInfo` | 机场名 / SN / **型号**（型号是承载力匹配键） |
| 顶层 | `capacityTable` | `AirportPlanScheduler.capacity()` 的数据源 | 承载力数值 |

`span` 你原来就有，通道类型会用它；**为空会触发整组保底**。

### 1.2 设备只看 `deviceList`，`isLine` 不参与

**报文给多少设备就拆多少，不看 `isLine`，不看 `jobObjectNum`，不去台账补数据。**

Java 里 `isLine=0` 会去台账读该线路全部杆塔、`isLine=1` 只读策略设备；
本脚本不区分这两种情况，一律以 `deviceList` 为准。所以：

- `isLine` 字段留着不影响，代码里没有一处读它
- `deviceList` 给 20 基就按 20 基拆，给 34 基就按 34 基拆
- `jobObjectNum` 与 `deviceList` 条数不一致时不校验、不报错（以 `deviceList` 实际条数为准）
- 台账里的设备不需要另传，`deviceList` 上的 6 个字段已经够了

---

## 2. 拆单流程

```
planList（已按频次展开的子计划）
  │
  ├─① 取设备：直接读 strategy.deviceList
  │     跳过 isDeleted≠0；按 sort 升序
  │
  ├─② 剔除（Java 6787-6790）
  │     通道(workNature=2) 且 coveringAirports 为空 → 剔除，不转人工
  │
  ├─③ 分流（Java 6872-6874、7111-7116）
  │     首位机场非空 且 过滤通过 → 机场桶
  │     否则                      → 人工
  │
  ├─④ 按首位机场分桶（Java 6191-6197）
  │
  ├─⑤ 估时（Java 6457-6710）
  │     每台秒数 = ceil(工作量 ÷ 承载力 × 理论日作业秒数)
  │     失败 → 整组保底 1 小时且不拆
  │
  └─⑥ 装箱（Java 5093-5118）
        limit = min(日配额, 时段总长)
        逐台累加，超 limit 就封单开下一张
        每个 Slice → 一张工单
```

### 2.1 过滤条件

```python
# tms + workNature=2(通道) -> 看 filterTd；其余 -> 看 filter；均须 == 0
if speciality == "tms" and work_nature == "2":
    return filterTd == 0
return filter == 0
```

### 2.2 承载力匹配键（Java 6467-6471）

| 维度 | 取值 |
|---|---|
| 机场型号 | `airports[机场GUID].airportModel` |
| 专业 | `plan.speciality`（`tms` / `dms` / `sms`） |
| 红外 | `workNatureSub ∈ {s-hwxs, t-hwxs, d-hwxs}` 的布尔值 |
| 作业类型 | `本体` / `通道` / `耐张` / `复合绝缘子` / `电缆终端` / `设备杆` / `融合例行` / `红外专项` |
| 电压 | 去掉 `AC` 前缀与 `kV` 后缀的纯数字（`AC110kV` → `110`） |

`pictureType` **不参与**承载力匹配，只写进工单的 `day_or_night`。

### 2.3 估时

```
理论日作业秒数：可见光 28800(8h) / 红外 21600(6h)
工作量：本体 = 1（按设备数）    通道 = span/1000（按公里）
每台秒数 = ceil(工作量 ÷ capacity × 理论日作业秒数)
```

任何估时异常（承载力查不到、档距缺失、变电分支未实现）→ **整组保底**：

```python
task.whole = True
task.seconds = [3600, 0, 0, ...]      # 整组只记 1 小时，不是每台 1 小时
schedule_status = "1"
schedule_reason = "<具体原因>，保底1小时"
```

### 2.4 装箱（拆单核心）

```
limit = min(28800 × 80%, plan.timePeriod 总长)      # 可见光 → min(23040, 时段)
        或 min(21600 × 80%, plan.timePeriod 总长)   # 红外   → min(17280, 时段)

逐台累加预计秒数：
    若 当前单已有设备 且 累加后 > limit → 封单，开下一张
    否则 → 加入当前单
```

三个要点：

1. **`limit` 会和计划时段取小**：`time_period` 只有 4 小时，单张容量就是 4 小时，不是 6.4 小时
2. **单台设备自身超限时独占一张单**（Java 原样行为），此时 `second_num > capacity_limit`
3. **`whole=True` 完全不拆**

### 2.5 输出：工单自包含 + 计划原样透传

工单带上重排所需的全部字段，**重排不依赖 `plan`**。`pre_start_date` / `pre_end_date` /
`time_period` 是生成时从计划继承的工单列（Java `WorkOrderInfo` 本来就有这三列）。

```jsonc
{
  "work_order_guid": "tms_2026_xxx",
  "work_order_no": "ZQ-{planNo}-{机场名}-{序号}",
  "plan_guid": "p-a-0003",                    // 追溯用，重排不读
  "inspection_method": "1",                    // 1 机场 / 2 人工
  "order_source": "5",
  "work_order_status": "9",
  "cycle_airport_guid": "ap-a",                // 只能在这个机场排
  "parent_device_num": 3,                      // 描述，重排忽略
  "second_num": 21600,                         // 占多久
  "capacity_limit": 23040,                     // 一天最多能干多少
  "schedule_status": "0",
  "schedule_reason": null,
  "pre_start_date": "2026-11-06 00:00:00",     // 能挪的范围
  "pre_end_date": "2026-11-06 23:59:59",
  "time_period": "08:30:00-17:30:00",          // 每天几点到几点
  "start_date": null,                          // 重排回填
  "end_date": null,
  "plan": { ...计划原始对象，原样... }
}
```

明细：`{ "work_order_guid": "...", "device": { ...deviceList 原始元素... } }`

人工工单：`work_order_no = RX-{planNo}-1`，`cycle_airport_guid` / `second_num` / `capacity_limit` 为 `null`。

**拆单实际读的计划字段只有 6 个**：`speciality` / `workNature` / `workNatureSub` /
`voltageLevel` / `timePeriod` / `preStartDate`，另外 `preEndDate` 用于继承。
其余字段一律不读、不改、原样带走。

### 2.6 出单顺序（与 Java 一致）

```
先出所有人工工单（按计划顺序）
再出机场工单：变电 → 有时段 → 专业 → one_day 升序 → 预开始时间 → id → GUID
```

所以工单序号从 `人工工单数 + 1` 开始，控制台按计划分组打印时编号会跳，这是 Java 原样行为。

---

## 3. 守恒校验

```
机场工单设备数 + 人工工单设备数 + 通道剔除数 = 台账号内设备数
```

附加：每台设备只能出现在一张工单里；明细数 = 工单设备数；`isDeleted≠0` 的设备单独记账。

`verify()` 每次运行自动校验，失败返回非 0。

---

## 4. 重排怎么接

**重排的粒度是「一张工单」，不是杆塔。** 一张工单是不可分单元，重排只决定它整体放到哪天，
不看它内部装了几台设备。`parent_device_num` 只是描述，重排时忽略；`details` 仅用于展示。

拆单产出**不落库**，直接和已有日期的旧工单放进同一个待排集合。字段名已经对齐：

| 字段 | 新工单 | 旧工单 |
|---|---|---|
| `work_order_guid` / `work_order_no` | 拆单生成 | 已有 |
| `cycle_airport_guid` | 首位机场 | 已有 |
| `second_num` | 拆单估时 | 已有 |
| `capacity_limit` | `plan_limit(plan)` | `plan_limit(plan)` |
| `pre_start_date` / `pre_end_date` | 在 `plan` 里 | 在 `plan` 里 |
| `time_period` | 在 `plan` 里 | 在 `plan` 里 |
| `start_date` / `end_date` | `null`（等重排填） | 已有值 |
| `plan` | 原样 | 原样 |

```python
from split_work_orders import split_all, plan_limit

orders, details, _ = split_all(payload)          # 新工单
for order in orders:                              # 旧工单也套同一口径
    order["capacity_limit"] = plan_limit(order["plan"])
```

`plan_limit(plan)` 就是拆单装箱用的 `min(日配额, 时段总长)`，新旧统一用它能保证
「能不能一天做完」的判断一致：

```python
plan_limit({"workNatureSub": "t-rcxs", "timePeriod": "08:30:00-12:30:00"})  # 14400
plan_limit({"workNatureSub": "t-rcxs", "timePeriod": "12:45:00-18:30:00"})  # 20700
plan_limit({"workNatureSub": "t-rcxs", "timePeriod": "08:30:00-17:30:00"})  # 23040（受配额）
```

> 注意：旧工单如果只有 `planGuid` 拿不到完整 `plan` 对象，就没有窗口和时段。
> 重排前必须把 plan 补齐（窗口 / 时段 / speciality / workNature / workNatureSub 这 5 个字段就够）。
>
> **取旧工单时不需要设备明细**：重排只认 `second_num` 这个总数，不关心里面是 3 台塔还是 30 台塔。
>
> 因为工单整体不可分，`second_num > capacity_limit` 的工单**重排永远排不进去**，
> 无论挪到哪个月。拆单阶段这种单只会出现在「单台设备自身超过时段容量」的情形。

---

## 5. 文件

| 文件 | 说明 |
|---|---|
| `main.py` | **统一入口**：拆单 → 取旧单合并 → 重排，一次跑完输出最终结果 |
| `split_work_orders.py` | 拆单实现（唯一需要集成的拆单文件） |
| `build_mock.py` | 生成 `mock_data/split_input.json` |
| `test_split.py` | 35 条规则 + 输入契约测试 |
| `test_customer_data.py` | 7 条客户真实数据回归 |
| `mock_data/split_input.json` | 自包含输入样例 |
| `out/work_orders.json` | 拆单结果 |

---

## 6. mock 数据覆盖的分支

策略 A：输电本体 22 基（`workNature=1`, 可见光）

| 杆塔 | 覆盖 | filter | 分流 | 预期 |
|---|---|---|---|---|
| #001-012 | ap-a | 0 | 机场 A | M350H 4/天 → 7200s/台 → 每张 3 台，4 张 |
| #013-017 | ap-b | 0 | 机场 B | M30T 8/天 → 3600s/台 → 18000s，1 张 |
| #018-019 | ap-c | 0 | 机场 C | 型号未配置 → 整组保底，1 张 |
| #020 | ap-a | **1** | 人工 | 过滤不通过 |
| #021-022 | 无 | 0 | 人工 | 无覆盖 |
| 人工 3 基 | | | | 合 1 张 |

策略 B：输电通道 5 基（`workNature=2`），策略两段时段展开成 4 条单段子计划

| 杆塔 | 覆盖 | 分流 | 预期 |
|---|---|---|---|
| #001-003 | ap-a | 机场 A | 按档距 880/920/1040 米折算，每张 1 台 |
| #004-005 | 无 | **剔除** | 通道无覆盖不转人工 |

结果：**7 个计划 → 33 张工单（30 机场 + 3 人工），78 条明细**，守恒通过。

> 上午段 `08:30-12:30` 只有 14400s < 配额 23040s，所以上午计划的 `limit = 14400`；
> 下午段 `12:45-18:30` 是 20700s，`limit = 20700`。**`limit` 随时段变化**。
> 1040 米那基在上午段预计 14976s > 14400s，单台超容，独占一张单。

---

## 7. 待替换的两处数据

| 项 | 位置 | 说明 |
|---|---|---|
| 承载力数值 | `split_input.json` 的 `capacityTable` | mock 里是占位值，真实值需要平台配置导出 |
| 配额比例 | `QUOTA_RATIO = 0.80` | 文档 §4.2/§4.3 口径，平台可配就改成配置项 |

---

## 8. 未实现（不在拆单职责内）

| 不做 | 说明 |
|---|---|
| 选日期、填 `start_date`/`end_date` | 排班职责 |
| 机场占用、冲突检查 | 排班职责 |
| 超量均摊、`未能排下` 判定 | 排班职责 |
| 人工按周错峰、跨月分量 | 排班职责 |
| 未巡设备过滤 | 需工单库图片数，代码里留了 `filter_plan_devices()` 钩子 |
| 变电整站航点估时 | 需航迹点数据，当前显式抛 `NotImplementedError`，不静默降级 |
| 配电台账 | 当前只支持输电 `tms` |

---

## 9. 三类巡检支持

| 专业 | 说明 | 估时口径 | 拆单规则 |
|---|---|---|---|
| `tms` | 输电 | 本体按设备数；通道按台账档距米数→公里 | 按 `limit` 拆 |
| `dms` | 配电 | 同输电（本体按设备数，通道按米数） | 按 `limit` 拆 |
| `sms` | 变电 | **整站有效航点求和**算一次 | **整单不拆** |

### 变电（sms）额外要求

必须在报文顶层提供航点数据 `deviceRoutes`：

```jsonc
"deviceRoutes": [
  {"deviceGuid": "st-0001", "deviceType": "sms", "trackType": "11",
   "taskPoint": 40, "validating": "1", "isDeleted": 0}
]
```

匹配条件（Java 6676-6681）：`deviceGuid` = 计划 `jobObjectGuid`、`deviceType=sms`、
`trackType` 匹配、`validating=1`、未删除，对 `taskPoint` 求和。

`trackType` 解析（Java 6671-6674）：优先 `plan.trackType`；空则红外→`309398410241898018`，
否则按 `workNatureSub` 映射 `s-lhxs→12` / `s-trcxs→363569449772114767` / `s-rcxs→11`。

航点为 0 时**显式报错走整组保底**（Java 不校验，会产出 0 秒工单）。

### 承载力配置要覆盖三个专业

```jsonc
{"airportModel":"M350H","speciality":"dms","infrared":false,"item":"本体","voltage":"10","capacity":6.0},
{"airportModel":"M350H","speciality":"sms","infrared":false,"item":"融合/例行","voltage":"500","capacity":100.0}
```

注意电压是纯数字：配电 `AC10kV`→`10`，变电 `AC500kV`→`500`。

---

## 10. 重排衔接模块 `rework_orders.py`

只负责「拉旧工单 → 过滤 → 归一化 → 和新工单合并成待排集合」。不重排、不落库。

```bash
# 一步到位：拆单 + 取范围 + 拉旧单 + 合并 + 重排（最终结果 out/result.json）
python main.py --existing-file out/existing_orders.json --out out/result.json

# 在线：拆单 + 取范围 + 拉旧单 + 合并
python rework_orders.py --input mock_data/split_input.json --out out/rework_set.json

# 离线：用本地文件代替接口响应
python rework_orders.py --existing-file out/existing_orders.json --out out/rework_set.json

# 只检查旧工单过滤与归一化
python rework_orders.py --normalize-only --existing-file out/existing_orders.json
```

### 时间范围

由新拆工单的 `pre_start_date` 最小值 ~ `pre_end_date` 最大值决定，自动传给接口的
`startTime` / `endTime`，其余参数用默认值。

### 过滤规则

| 状态 | 含义 | 处理 |
|---|---|---|
| `11` | 已作废 | **丢弃**（Java 参与占用的只有 8/9/12/16） |
| `12` | 延期 | **丢弃**（接口未返回周期时间窗口，暂时无法重排） |
| `8` / `9` / `16` | 其余 | 保留 |

### 字段归一化

业务 DTO → 待排结构：

| 业务字段 | 待排字段 |
|---|---|
| `workOrderGuid` | `work_order_guid` |
| `cycleAirportGuid` | `cycle_airport_guid` |
| `secondNum` | `second_num` |
| `startDate` / `endDate` | `start_date` / `end_date` |
| — | `pre_start_date` / `pre_end_date` **保持 `null`**（旧工单没有可调整窗口） |
| `airportModel` / `workNatureSub` / `cycleVoltageLevel` | 保留，用于算 `capacity_limit` |
| 原始 JSON | `raw` 原样带走 |

时间格式统一成 `YYYY-MM-DD HH:MM:SS`，兼容 `T` 分隔和纯日期。

### 两种工单的约束来源不同

| | 可放窗口 | 现状基准 | 说明 |
|---|---|---|---|
| **新工单** | `pre_start_date ~ pre_end_date`（继承计划） | 无（`start/end` 为 null） | 必须排在计划窗口内 |
| **旧工单** | **无** | `start_date ~ end_date` | 重排基准就是当前位置，不伪造窗口 |

本次查询的时间段只记在输出的 `summary.window` 里，**不下发到任何工单字段**。

### 接口鉴权（已验证可用）

网关要**两个头一起发**，缺一不可：

```python
{
    "authorization": "Basic c2FiZXI6c2FiZXJfc2VjcmV0",   # 客户端凭证
    "zxhc-auth":     "bearer eyJhbGciOi..."              # 用户令牌（本产品自定义头名）
}
```

环境变量覆盖：

```bash
export MAIN_CYCLE_BASIC="Basic c2FiZXI6c2FiZXJfc2VjcmV0"
export MAIN_CYCLE_TOKEN="bearer eyJhbGciOi..."
export MAIN_CYCLE_AUTH_HEADER="zxhc-auth"
export MAIN_CYCLE_BASE="http://10.10.2.101:8000"
```

> 注意：不是 `Blade-Auth`（那是 BladeX 上游默认），本产品把令牌头改成了 `zxhc-auth`。
> `MAIN_CYCLE_TOKEN` 会过期，过期后换一个即可，`MAIN_CYCLE_BASIC` 不用动。

### 实测数据量

| 场景 | 结果 |
|---|---|
| 单月（2026-10） | 390 条，3 页 |
| 拆单窗口（2026-11-01~11-14） | 720 条 |
| 过滤后可参与重排 | **16 条**（作废 4 / 延期 695 / 状态6 5） |

---

## 11. 重排算法 `rework_algorithm.py`

把三份文档里的重排规则实现成可运行的排产器。只做机场工单整单重排 + 人工错峰，不落库。

```bash
python rework_algorithm.py --input out/rework_set.json --out out/rework_result.json
python rework_algorithm.py --input out/rework_set.json --enforce-quota   # 按 80% 日配额约束
```

### 8 层规则落地

| 层 | 文档依据 | 实现 |
|---|---|---|
| L1 判定入口 | 文档2 §四 / FR05 | 先固定旧安排试排，全排下 → `normal` |
| L2 对象筛选 | 接口 5.2 | 状态 9 非变电 → 可重排；8/12/16 固定占用（16 无时长不占位）；变电历史固定 |
| L3 硬约束 | 文档2 §五 / FR06 | 按自身窗口/时段/机场；整单不拆；机场不降级；已完成事实保持 |
| L4 链式重排 | FR06 | 回溯抢占 + 因果链记录 + 防循环 + 深度上限 |
| L5 占用 | 文档2 §六 | 每机场共享日历；挪动即补齐占用；分支失败恢复原占用 |
| L6 强插 | FR07 | 排不下才出建议，点名受保护占用者，不越过 |
| L7 变更集 | 接口 5.3 | 输出每张挪动旧单的 old→new 时间与抢占方 |
| L8 人工错峰 | 接口 6.1 | 最大余数法跨月分量 + 同计划周分散 + 全局周负载均衡 |

### 入口形态

输出 `entry` 三选一：

- `normal` —— 旧安排不变即排完
- `insert_rework` —— 按规则插单，产生一条重排链
- `force_insert` —— 排不妥，给出强插建议（含受保护占用者清单）

### 输出结构：最终工单分两组

`rework_result.json` 把排班后的最终效果拆成两组，再加附注：

```jsonc
{
  "entry": "insert_rework",
  "window": {"start": "...", "end": "..."},

  // ① 新增组：所有新工单，start_date/end_date 已排定；排不下的为 null
  "new": [
    { "...原新工单字段...", "start_date": "2026-12-02 15:30:00", "end_date": "2026-12-02 17:30:00" }
  ],

  // ② 修改组：按 work_order_guid 修改的旧工单，只带新起止时间
  "update": [
    { "work_order_guid": "tms_2026_xxx", "work_order_no": "...",
      "start_date": "2026-12-04 15:30:00", "end_date": "2026-12-04 17:30:00" }
  ],

  // 附注
  "unplaced_new": [...],
  "force_proposals": [...],
  "rework_chain": [...],
  "warnings": [...]
}
```

- `new`：机场新单带排定的 `start_date/end_date`；人工新单带周窗口（`week_start/week_end` 同样落到这两个字段）；未排下的保持 `null`
- `update`：被挪动的旧机场单 + 重排的旧人工单，消费者按 `work_order_guid` 直接 UPDATE 时间

### 关键设计约定

1. **占用用 `secondNum`，不用 `endDate-startDate`**（测试环境时间跨度不可信）。
2. **旧工单没时段时用默认窗口**：可见光 `08:30-17:30`、红外 `05:00-08:00;19:00-22:00`。
3. **80% 日配额是可选项**（`--enforce-quota`），默认关。文档要求正常排班受配额约束，
   生产打开即可；关掉便于隔离「链式重排」逻辑本身。
4. **旧工单没有 pre 窗口时**，可移动范围退回本次重排整体窗口（生产数据有 pre 字段时优先用它）。
5. 重排粒度是**整张工单**：`seconds` 固定，工单要么整体放进某一天，要么不动。
6. 测试 `test_rework_algorithm.py` 里用文档2 的 C→B→D 例子做了逐层断言。

> 当前 `rework_set.json` 里新工单是 mock 机场（ap-a/b/c），旧工单是真实机场 GUID，
> 两边机场不同所以重排链为 0 步 —— 这是测试环境数据错位，不是算法问题。

---

## 12. 文档符合性核对结论（已验证）

结合三份文档的重排规则逐条核对 `rework_algorithm.py`，本轮修掉 4 处不符：

| # | 文档依据 | 原实现 | 修正 |
|---|---|---|---|
| 1 | 接口 6.1「同计划周负载最少 → 全局周负载 → 均匀目标」 | 只按全局周负载选周，缺同计划层 | 三级排序：同计划负载 → 全局负载 → 周早晚 |
| 2 | 接口 5.2「历史工单预执行窗口须与请求范围相交」 | 状态 9 非变电一律可重排 | 加窗口相交判定，不相交不进入本次任务 |
| 3 | 文档2 §五「每步按当前工单自身优先级」 | 抢占候选按 guid 字典序 | 按 `_priority` 排序（变电→时段→专业→oneDay→窗口→时间） |
| 4 | 文档2 §六 / Java 6572「未能排下的保留窗口不计真实占用」 | 一律按 start/end 占位 | **状态 9** 且 `scheduleReason` 含「未能排下」才不占日历 |

新增 3 条测试锁定（`test_rework_algorithm`）：

- `test_reworkable_window_must_intersect_range`
- `test_unplaced_reason_not_occupied`
- `test_priority_smaller_window_first`

### 仍有意留白 / 待平台口径的项

| 项 | 文档依据 | 现状 |
|---|---|---|
| 「时间调整」出口 | 文档2 §四（方式及上限待确认） | 未实现，暂不并入强插 |
| 强插前展示「直接/间接受影响计划与工单、拟更新清单」 | FR07 | 只回 order + 受保护占用者，未列全量影响链 |
| 人工「班组产能核验」 | FR08 | 无产能数据，只做周分散 |
| 「参与占用 ≠ 允许被抢占」的额外允许标志 | FR06 | 未建模，默认状态 9 非变电全部可被抢占 |
| 排班缓存/事务边界、状态码回写 | 接口 5.3 / 6.2 | 不落库，属平台侧 |
| `88%/80% 配额` 默认开关 | 接口 4.3 | 做成 `--enforce-quota` 可选项，默认关 |
