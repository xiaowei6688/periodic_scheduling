# 周期计划 → 工单拆分 → 新旧工单重排

把客户 Java 侧的「计划拆工单」逻辑用 Python 重做，并接上「新工单插入 + 旧工单链式重排 + 人工错峰」的排期算法。

- **排期规则**以两份需求文档为准：《周期计划智能调度需求文档.docx》《临时周期计划调度需求确认1110(1).docx》。
- **拆单口径**参考 `createOrderByPlansNew` 接口业务逻辑说明。
- 只做计算，**不落库**。整条链路一次跑完，输出结构化结果，由业务平台按结果保存（平台自行生成正式 id，本工程生成的 `work_order_guid` 只是临时关联句柄）。

```bash
python build_mock.py                             # 生成 mock_data/split_input.json
python main.py                                   # 完整流程：拆单 → 取旧单 → 重排
python main.py --existing-file out/existing_orders.json --out out/result.json
python -m unittest discover -p "test_*.py" -v    # 全量回归
```

---

## 1. 文件

| 文件 | 说明 |
|---|---|
| `main.py` | **统一入口**：拆单 → 取旧单合并 → 重排，输出最终结果 |
| `split_work_orders.py` | 拆单：计划报文 → 工单 + 明细（承载力内置） |
| `rework_orders.py` | 旧工单拉取 → 过滤归一化 → 与新单合并成待排集合 |
| `rework_algorithm.py` | 重排算法：插入 / 链式重排 / 强插建议 / 人工错峰 |
| `build_mock.py` | 生成 `mock_data/split_input.json`、`split_input_multi.json` |
| `输入参数说明.md` | 输入报文字段含义与 JSON 结构 |
| `输出结构说明.md` | 输出 `out/result.json` 的 JSON 结构说明 |
| `test_*.py` | 拆单、重排、管线回归测试 |

---

## 2. 完整流程与统一入口

```
拆单报文
    │ split_all()           设备分流 → 机场/人工成单（start_date/end_date 为 null）
    ▼
新工单
    │ collect_rework_set()  定窗口 → 拉旧工单 → 过滤归一化 → 合并待排集合
    ▼
待排集合（新 + 旧）
    │ rework()             先固定旧安排试排 → 链式重排 → 强插建议；人工按周错峰
    ▼
out/result.json
```

程序化调用：

```python
from main import run_pipeline

out = run_pipeline(payload, existing_rows=None, enforce_quota=True)
# out = {"planSetInfo": {...}, "planStrategyList": [...], "oldWorkOrders": [...], "entry": "...", ...}
```

CLI 参数（`main.py`；`rework_algorithm.py` 同义）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | `mock_data/split_input.json` | 拆单输入报文 |
| `--out` | `out/result.json` | 完整流程结果 |
| `--existing-file` | 无 | 离线模式：本地旧工单 JSON 代替在线接口 |
| `--save-existing` | 无 | 在线拉到的旧工单原样另存 |
| `--enforce-quota / --no-enforce-quota` | 开 | 正常排班是否受 80% 日配额约束 |
| `--skip-weekday` | 空 | 跳过星期，逗号分隔，1=周一…7=周日 |
| `--holiday` | 空 | 跳过节假日，逗号分隔 `YYYY-MM-DD` |

---

## 3. 拆单要点

- **设备只看 `deviceList`**：报文给多少拆多少；`isLine`、`jobObjectNum` 不参与。
- **分流**：`coveringAirports` 去空去重保序，首位是最优机场；过滤通过 → 机场，无覆盖/过滤不通过 → 人工；输配电通道无覆盖 → 直接剔除、不转人工。
- **承载力内置**：`airport_capacity()` 按机场型号 `jc_03`（三代）/`jc_08`（二代）/`jc_09`（=三代×2）+ 专业 + 红外 + 作业类型 + 电压查表；型号未配置 → 整组保底 1 小时。
- **估时**：`ceil(工作量 ÷ 理论日承载力 × 理论日作业秒数)`。本体按设备数、可见光通道按公里、变电按整站有效航点一次。
- **装箱**：`limit = min(80% 日配额, 计划时段总长)`；逐台累加，超限封单开下一张；变电 / 保底任务整单不拆。
- **出单顺序**：先人工单，再机场单（变电 → 有时段 → 专业 → one_day → 预开始 → id → guid）。
- **守恒校验**：`机场设备 + 人工设备 + 通道剔除 = 台账设备`，同一设备只出现一次。

---

## 4. 重排要点（按两份需求文档）

| 层 | 文档依据 | 实现 |
|---|---|---|
| L1 入口 | FR05 / 临时§三 | 先固定旧安排试排；全部新单排下 → `normal` |
| L2 筛选 | FR06 / 临时§五 | 状态 9 非变电可重排；8/16 固定占位；变电固定不可挪动 |
| L3 约束 | 临时§六 | 自身窗口 / 时段 / 机场；整单不拆；机场不降级；跳过配置星期与节假日；变电可见光 08:30 起 |
| L4 链式重排 | FR06 | 抢占 → 被抢占单按自身规则重排 → 继续；防循环；链长不设上限 |
| L5 占用 | 临时§六 | 同机场共享日历；失败恢复原占用；“未能排下”的保留窗口不计真实占用 |
| L6 强插 | FR07 | 排不妥才给建议；列出直接/间接影响 + 拟更新 + 受保护占用者，不越过 |
| L7 变更集 | 临时§五 | 只输出被挪动旧单的 old→new 时间；机场变化时另带 `cycle_airport_guid` |
| L8 人工错峰 | FR08 | 状态 9 普通人工，按计划分组：跨月最大余数 → 周错峰（同计划负载 → 全局负载） |

输出 `entry` 三态：

- `normal` —— 旧安排不变即可排完
- `insert_rework` —— 插单并产生一条重排链
- `force_insert` —— 规则内排不妥，给强插建议

> 未排下的新单保持 `start_date/end_date = null`，与 `unplaced_new` 清单对应。

---

## 5. 输入 / 输出

- 输入报文：`planSetInfo + planStrategyList + airports`；**不再传 `capacityTable`**。字段含义与 JSON 结构见 [输入参数说明.md](输入参数说明.md)。
- 输出：按**输入报文格式**回组，`out/result.json`。

```jsonc
{
  "planSetInfo": {
    // ...原计划集字段...
    "planPackageCount": 2,   // 计划包数量 = planStrategyList 条数
    "planCount": 7,          // 计划数量 = 各 planList 条数之和
    "workOrderCount": 16     // 拆分工单数量（汇总）
  },
  "planStrategyList": [      // 计划包列表
    {
      // ...原策略字段...
      "planCount": 3,         // 本计划包下的计划数量
      "workOrderCount": 12,   // 本计划包下的新工单数量
      "planList": [           // 计划列表
        {
          // ...原计划字段...
          "workOrderCount": 4,     // 本计划下拆出的工单数量
          "workOrders": [ { "...新工单字段...", "start_date": "...", "end_date": "...", "details": [...] } ]
        }
      ]
    }
  ],
  // 旧工单单独一级，与 planStrategyList 平级；动过的旧单 startDate/endDate 已回写
  "oldWorkOrders": [ { "...旧工单DTO...", "startDate": "...", "endDate": "..." } ],

  // 附注
  "entry": "normal",              // normal / insert_rework / force_insert
  "forceProposals": [],           // entry=force_insert 时的强插建议
  "warnings": []
}
```

新工单关键字段：`work_order_guid / work_order_no / plan_guid / inspection_method(1机场,2人工) /
cycle_airport_guid / parent_device_num / second_num / capacity_limit / schedule_status /
schedule_reason / pre_start_date / pre_end_date / time_period / start_date / end_date / details`。
未排下的新工单保持 `start_date/end_date = null`，仍挂在所属计划的 `workOrders` 里。

> `planGuid` 只用于把工单追溯到所属计划，不参与 id 生成。平台若按“计划包→计划→工单”树结构下发并自行生成 id，`planGuid` 可选；工单的 `plan_guid` 会为空，其余算法不受影响。

---

## 6. 内置口径

| 项 | 值 |
|---|---|
| 正常日配额比例 | `QUOTA_RATIO = 0.80` |
| 理论日作业秒数 | 可见光 `28800`（8h）/ 红外 `21600`（6h） |
| 无时段默认窗口 | 可见光 `08:30-17:30`；红外 `05:00-08:00;19:00-22:00` |
| 变电可见光起排 | `08:30` |
| 承载力 | `airport_capacity()`：`jc_03/jc_08/jc_09` 三型号 |
| 人工入组口径 | 状态 `9`、普通人工，排除 `补巡待确认排班` |

---

## 7. 已知留白 / 待平台口径

| 项 | 文档依据 | 现状 |
|---|---|---|
| 旧单 `pre_start_date/pre_end_date/timePeriod` | FR06 / 临时§五 | 接口补齐后自动生效；未提供按现状兼容 |
| 旧单台账首位机场 | 临时§六 | 从 `raw.workOrderDetails/details` 读取，共同首位机场才换 |
| 人工普通/补巡状态码 | FR08 | 按平台既有口径：状态 9、排除 `补巡待确认排班` |
| 班组产能核验 | FR08 | 无飞手数量/级别数据，只做周分散 |
| 补巡覆盖 / 未巡图片数筛选 | 临时§六 | 数据不在报文里，留 `filter_plan_devices()` 钩子 |
| 变电整站航点估时 | 临时§六 | 航点数据到位时按整站一次估时；缺数据走整组保底 |
| 时间调整出口 | 临时§四 | 文档本身待确认（顺延/延长、7 天讨论值） |

---

## 8. 测试

```bash
python -m unittest discover -p "test_*.py" -v
```

覆盖：拆单规则与守恒、客户真实数据回归、重排 8 层规则、统一入口冒烟、文档缺口专项。