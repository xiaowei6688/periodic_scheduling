# 周期计划 → 工单拆分

把客户 `WorkOrderInfoServiceImpl.addOrderByPlanNew` 的**拆单**逻辑翻成 Python。

只做一件事：**把一条计划拆成若干张工单，每张工单保证「存在一个执行日能做完」。**
不选日期、不查占用、不做超量均摊，所以工单的 `start_date` / `end_date` 一律为 `null`。

**不访问任何数据库。** Java 里要查三张表才能拿到的数据（设备台账 / 机场台账 / 承载力配置），
全部随输入报文一起传进来。

```bash
python build_mock.py                             # 生成 mock_data/split_input.json
python split_work_orders.py                      # 拆单
python -m unittest discover -p "test_*.py" -v    # 41 条测试
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

### 2.5 输出：拆单字段 + 原样透传

工单只输出拆单自己算出来的字段，**计划参数整块原样带在 `plan` 里**，不挑选、不改名、不补默认值：

```jsonc
{
  "work_order_guid": "tms_2026_xxx",          // 拆单生成
  "work_order_no": "ZQ-{planNo}-{机场名}-{序号}",
  "plan_guid": "...",                          // 仅作索引，完整对象在 plan 里
  "inspection_method": "1",                    // 1 机场 / 2 人工
  "order_source": "5",                         // 自动生成
  "work_order_status": "9",                    // 排队中
  "cycle_airport_guid": "ap-a",                // 执行机场
  "parent_device_num": 3,                      // 这张单装几台设备
  "second_num": 21600,                         // 预计作业秒数
  "capacity_limit": 23040,                     // 单张工单工作量上限
  "schedule_status": "0",                      // 仅保底时为 1
  "schedule_reason": null,
  "start_date": null,                          // 排班阶段回填
  "end_date": null,
  "plan": { ...计划原始对象，70 个字段原样... }
}
```

明细同理，设备元素整块原样回传：

```jsonc
{
  "work_order_guid": "tms_2026_xxx",
  "device": { ...deviceList 原始元素... }
}
```

人工工单：`work_order_no = RX-{planNo}-1`，`cycle_airport_guid` / `second_num` / `capacity_limit` 为 `null`。

**拆单实际读的计划字段只有 6 个**：`speciality` / `workNature` / `workNatureSub` / `voltageLevel` / `timePeriod` / `preStartDate`。
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
| `split_work_orders.py` | 拆单实现（唯一需要集成的文件） |
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
