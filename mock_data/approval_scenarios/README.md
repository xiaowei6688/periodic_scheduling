# 三种确认场景 Mock

页面手工测试请直接载入三份完整版 JSON。每份文件都包含完整计划报文和
`mockExistingRows`，不依赖在线旧工单接口：

| 场景 | 旧工单文件 | 预期 entry | 预期确认按钮 |
|---|---|---|---|
| 全部排妥 | `normal_full.json` | `normal` | 确认创建机场与人工工单 |
| 重排后排妥 | `reworked_full.json` | `insertRework` | 确认创建并更新旧单 |
| 规则内排不完 | `force_insert_full.json` | `forceInsert` | 确认强插 |

三种场景的取消按钮均为“取消”。

页面测试方法：打开“排班调试”，点击“载入本地 JSON”，选择上述任一完整版文件，
然后直接提交。三份数据在“执行每日配额限制”开启或关闭时都能稳定触发对应场景。

也可以使用命令行验证：

```bash
uv run python main.py \
  --input mock_data/approval_scenarios/normal_full.json \
  --existing-file mock_data/approval_scenarios/normal_existing.json \
  --out out/approval-normal.json

uv run python main.py \
  --input mock_data/approval_scenarios/reworked_full.json \
  --existing-file mock_data/approval_scenarios/reworked_existing.json \
  --out out/approval-reworked.json

uv run python main.py \
  --input mock_data/approval_scenarios/force_insert_full.json \
  --existing-file mock_data/approval_scenarios/force_insert_existing.json \
  --out out/approval-force-insert.json
```

`reworked_existing.json` 构造两轮连续重排：新机场工单占用第一张旧单的原时段，第一张旧单再占用第二张旧单的原时段，第二张旧单最终移动到第三天。

`force_insert_existing.json` 使用状态 `8` 的受保护旧单占满新机场工单唯一允许时段，算法不能移动该旧单，因此输出强插建议。
