"""生成拆单测试用的**自包含**输入报文。

只产出 `mock_data/split_input.json`（基础场景）和 `split_input_multi.json`（三类巡检），
拆单所需的设备都在 `deviceList` 上，机场台账随报文来，承载力由内置表查（不再传 capacityTable）：

    {
      "planSetInfo":       { ... },                      // 计划集，客户原有结构
      "planStrategyList":  [ { ..., deviceList: [...], planList: [...] } ],
      "airports":          [ {airportGuid, airportName, airportSn, airportModel} ]
    }

`deviceList[]` 在原报文基础上多带 5 个台账字段（Java 里这些要查 DeviceTmsTowerInfo）：
    coveringAirports / filter / filterTd / sort / towerBranchName

场景（刻意覆盖所有拆分分支）：
    策略 A  输电本体(workNature=1, t-rcxs, 可见光)  isLine=0 整线  22 基
        #001-012  ap-a (jc_03 三代, 20基/天) -> 每台 1440s -> 12 台一张
        #013-017  ap-b (jc_08 二代, 15基/天) -> 每台 1920s -> 5 台一张
        #018-019  ap-c (型号未配置)          -> 整组保底 1 小时，1 张
        #020      有覆盖但 filter=1          -> 过滤不通过，转人工
        #021-022  无覆盖                     -> 转人工
        人工 3 基                            -> 合 1 张
    策略 B  输电通道(workNature=2, t-ttdxs)  isLine=0  5 基
        策略时段两段，展开成 4 条单段子计划（2 次 × 2 段）
        #001-003  ap-a (jc_03 通道 15 公里/天) -> 按档距折算，3 台一张
        #004-005  无覆盖                      -> 通道直接剔除，不转人工

用法：
    python build_mock.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

MOCK_DIR = Path(__file__).resolve().parent / "mock_data"

PLAN_SET_GUID = "a1b2c3d4000041118222333444555aaa"
STRATEGY_A_GUID = "b1b2c3d4000041118222333444555bbb"
STRATEGY_B_GUID = "c1b2c3d4000041118222333444555ccc"
PROJECT_ID = "3e157f13e626429e93bc1efbf7d5bdc5"
PROJECT_NAME = "2026输检项目"
AREA_CODE = "100112102106"
LINE_A_GUID = "d1b2c3d4000041118222333444555ddd"
LINE_B_GUID = "e1b2c3d4000041118222333444555eee"
STRATEGY_C_GUID = "d1b2c3d4000041118222333444555ccc"   # 配电
STRATEGY_D_GUID = "f1b2c3d4000041118222333444555ddd"   # 变电
LINE_C_GUID = "a1b2c3d5000041118222333444555fff"       # 10kV 苍岭馈线

# 机场台账（Java 里查 EquipAirportInfo）。AP-C 故意用未配置型号，验证保底分支
AIRPORTS = [
    {"airportGuid": "ap-a", "airportName": "苍岭机场A", "airportSn": "SN-JC03-001", "airportModel": "jc_03"},
    {"airportGuid": "ap-b", "airportName": "苍岭机场B", "airportSn": "SN-JC08-002", "airportModel": "jc_08"},
    {"airportGuid": "ap-c", "airportName": "苍岭机场C", "airportSn": "SN-X-003", "airportModel": "X-UNKNOWN"},
]

# 承载力不再随报文传：拆单侧内置 airport_capacity()（单机场承载力.xlsx / Java AirportPlanScheduler.capacity）


def _common() -> dict:
    return {
        "id": -1, "createUser": -1, "createTime": "", "updateUser": -1, "updateTime": "",
        "createDept": -1, "deptCode": "", "isDeleted": -1, "fbProfessional": "", "fbYear": "",
    }


def build_device(tower: dict, strategy_guid: str, line_name: str, speciality: str = "tms") -> dict:
    """deviceList 元素 = 策略设备 + 内联台账字段。"""
    device = _common()
    device.update({
        "planStrategyGuid": strategy_guid,
        "jobObjectGuid": tower["line_guid"],
        "jobObjectDeviceGuid": tower["tower_guid"],
        "jobObjectName": line_name,
        "jobObjectDeviceName": tower["tower_name"],
        "speciality": speciality,
        "span": str(tower["span"]),
        # ---- 以下是 Java 要查台账才能拿到的字段，这里内联 ----
        "coveringAirports": tower["covering_airports"],
        "filter": tower["filter"],
        "filterTd": tower["filter_td"],
        "sort": tower["sort"],
        "towerBranchName": tower["branch_name"],
    })
    device.pop("isDeleted", None)   # 输入参数没有 isDeleted，也不做删除判断
    return device


def build_plan(base: dict, *, seq: int, plan_guid: str, plan_no: str,
               pre_start: str, pre_end: str, cycle_start: str, cycle_end: str,
               insp_start: str, insp_end: str, one_day: int, time_period: str,
               strategy_guid: str, job_object_num: int, frequency: str,
               speciality: str = "tms", work_nature: Optional[str] = None,
               work_nature_sub: Optional[str] = None, voltage_level: str = "AC110kV") -> dict:
    plan = _common()
    plan.update({
        "planGuid": plan_guid, "demandGuid": "", "demandNo": "", "demandDeptCode": "",
        "planNo": plan_no,
        "speciality": speciality,
        "voltageLevel": voltage_level,
        "jobObject": base["job_object"],
        "jobObjectGuid": base["line_guid"],
        "inspectionFrequency": frequency,
        "workNature": work_nature or base["work_nature"],
        "workNatureSub": work_nature_sub or base["work_nature_sub"],
        "jobObjectNum": job_object_num,
        "cancelNum": -1, "workOrderNum": -1,
        "planType": "1", "expectedCompletionTime": "", "planSource": "",
        "parentPlanGuid": "", "parentPlanNo": "",
        "planStatus": "8", "rateOfProgress": 0,
        "projectStage": "", "projectStageSub": "", "auditUser": "", "auditTime": "",
        "remark": "", "jobResults": "", "electricityQuantity": "", "flightAltitude": "",
        "opticalZoomFocal": "", "tmsTowerTdDistance": -1, "deliverInFlight": -1,
        "oneDay": one_day, "cycleCnt": seq,
        "preStartDate": pre_start, "preEndDate": pre_end,
        "cycleStartDate": cycle_start, "cycleEndDate": cycle_end,
        "inspectionCycleStartDate": insp_start, "inspectionCycleEndDate": insp_end,
        "originalPlanStartDate": "", "originalPlanEndDate": "",
        "workObjDetail": "",
        "projectName": PROJECT_NAME, "projectId": PROJECT_ID, "areaCode": AREA_CODE,
        "inspectionMethod": "1",
        "planStrategyGuid": strategy_guid, "planSetGuid": PLAN_SET_GUID,
        "timePeriod": time_period,
        "pictureType": "1", "trackType": "", "isValidate": "1",
        "completeNum": 0, "completeDeviceNum": 0, "completeKilometers": 0,
        "projectPlanType": "", "sortie": -1, "flyTime": -1,
    })
    return plan


def build_strategy(guid: str, base: dict, *, frequency: str, one_day: int,
                   time_period: str, device_list: list[dict], plan_list: list[dict],
                   speciality: str = "tms", work_nature: Optional[str] = None,
                   work_nature_sub: Optional[str] = None) -> dict:
    strategy = _common()
    strategy.update({
        "planStrategyGuid": guid, "planSetGuid": PLAN_SET_GUID,
        "speciality": speciality, "voltageLevel": "AC110kV",
        "jobObject": base["job_object"], "jobObjectGuid": base["line_guid"],
        "workNature": work_nature or base["work_nature"],
        "workNatureSub": work_nature_sub or base["work_nature_sub"],
        "frequency": frequency,
        "projectName": PROJECT_NAME, "projectId": PROJECT_ID, "hookProjectNum": 1,
        "projectType": "", "towerLabels": "", "areaCode": AREA_CODE, "workObjDetail": "",
        "inspectionMethod": "1", "planStrategyStatus": "",
        "preStartDate": base["range_start"], "preEndDate": base["range_end"],
        "oneDay": one_day,
        "jobObjectNum": len(device_list), "planCount": len(plan_list),
        "isLine": "0",
        "timePeriod": time_period,
        "pictureType": "1", "trackType": "", "isValidate": "1",
        "completeNum": 0, "completeKilometers": 0, "dailyInspectionFrequency": "1",
        "deviceList": device_list,
        "planList": plan_list,
    })
    return strategy


def build_towers(line_guid: str, count: int, covering_rule, span_rule) -> list[dict]:
    towers = []
    for i in range(1, count + 1):
        towers.append({
            "tower_guid": f"{line_guid[:6]}-t{i:03d}",
            "tower_name": f"苍岭线{i:03d}号塔",
            "line_guid": line_guid,
            "branch_name": str((i - 1) // 8 + 1),   # towerBranchName，和 sort 一起定顺序
            "sort": i,
            "span": span_rule(i),
            **covering_rule(i),
        })
    return towers


def main() -> None:
    MOCK_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------- 策略 A：本体 22 基 ----------------
    def covering_a(i: int) -> dict:
        if i <= 12:
            return {"covering_airports": "ap-a", "filter": 0, "filter_td": 0}
        if i <= 17:
            return {"covering_airports": "ap-b", "filter": 0, "filter_td": 0}
        if i <= 19:
            return {"covering_airports": "ap-c", "filter": 0, "filter_td": 0}
        if i == 20:
            return {"covering_airports": "ap-a", "filter": 1, "filter_td": 0}
        return {"covering_airports": "", "filter": 0, "filter_td": 0}

    towers_a = build_towers(LINE_A_GUID, 22, covering_a, lambda i: 300 + i * 10)
    devices_a = [build_device(t, STRATEGY_A_GUID, "110kV 苍岭线") for t in towers_a]

    base_a = {"job_object": "110kV 苍岭线", "line_guid": LINE_A_GUID,
              "work_nature": "1", "work_nature_sub": "t-rcxs",
              "range_start": "2026-11-01 00:00:00", "range_end": "2026-11-06 23:59:59"}
    no_a = "JH-20261116090000001"
    plans_a = [
        build_plan(base_a, seq=1, plan_guid="p-a-0001", plan_no=f"{no_a}-1",
                   pre_start="2026-11-01 00:00:00", pre_end="2026-11-02 23:59:59",
                   cycle_start="2026-11-01 00:00:00", cycle_end="2026-11-02 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-06 23:59:59",
                   one_day=2, time_period="08:30:00-17:30:00",
                   strategy_guid=STRATEGY_A_GUID, job_object_num=22, frequency="周期内3次"),
        build_plan(base_a, seq=2, plan_guid="p-a-0002", plan_no=f"{no_a}-2",
                   pre_start="2026-11-03 00:00:00", pre_end="2026-11-05 23:59:59",
                   cycle_start="2026-11-03 00:00:00", cycle_end="2026-11-05 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-06 23:59:59",
                   one_day=3, time_period="08:30:00-17:30:00",
                   strategy_guid=STRATEGY_A_GUID, job_object_num=22, frequency="周期内3次"),
        build_plan(base_a, seq=3, plan_guid="p-a-0003", plan_no=f"{no_a}-3",
                   pre_start="2026-11-06 00:00:00", pre_end="2026-11-06 23:59:59",
                   cycle_start="2026-11-06 00:00:00", cycle_end="2026-11-06 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-06 23:59:59",
                   one_day=1, time_period="08:30:00-17:30:00",
                   strategy_guid=STRATEGY_A_GUID, job_object_num=22, frequency="周期内3次"),
    ]
    strategy_a = build_strategy(STRATEGY_A_GUID, base_a, frequency="周期内3次", one_day=2,
                                time_period="08:30:00-17:30:00",
                                device_list=devices_a, plan_list=plans_a)

    # ---------------- 策略 C：配电（dms）本体 8 基 ----------------
    def covering_c(i: int) -> dict:
        if i <= 5:
            return {"covering_airports": "ap-a", "filter": 0, "filter_td": 0}
        if i == 6:
            return {"covering_airports": "ap-b", "filter": 1, "filter_td": 0}   # 过滤不通过 -> 人工
        return {"covering_airports": "", "filter": 0, "filter_td": 0}          # 无覆盖 -> 人工

    towers_c = build_towers(LINE_C_GUID, 8, covering_c, lambda i: 200 + i * 10)
    devices_c = [build_device(t, STRATEGY_C_GUID, "10kV 苍岭馈线", speciality="dms")
                 for t in towers_c]
    base_c = {"job_object": "10kV 苍岭馈线", "line_guid": LINE_C_GUID,
              "work_nature": "1", "work_nature_sub": "d-rcxs", "speciality": "dms",
              "range_start": "2026-11-01 00:00:00", "range_end": "2026-11-03 23:59:59"}
    no_c = "JH-20261116090000003"
    plans_c = [
        build_plan(base_c, seq=1, plan_guid="p-c-0001", plan_no=f"{no_c}-1",
                   pre_start="2026-11-01 00:00:00", pre_end="2026-11-03 23:59:59",
                   cycle_start="2026-11-01 00:00:00", cycle_end="2026-11-03 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-03 23:59:59",
                   one_day=3, time_period="08:30:00-17:30:00",
                   strategy_guid=STRATEGY_C_GUID, job_object_num=8, frequency="周期内1次",
                   speciality="dms", work_nature="1", work_nature_sub="d-rcxs",
                   voltage_level="AC10kV"),
    ]
    strategy_c = build_strategy(STRATEGY_C_GUID, base_c, frequency="周期内1次", one_day=3,
                                time_period="08:30:00-17:30:00",
                                device_list=devices_c, plan_list=plans_c,
                                speciality="dms", work_nature="1", work_nature_sub="d-rcxs")

    # ---------------- 策略 D：变电（sms）整站 2 台设备 ----------------
    station_guid = "st-0001"
    devices_d = []
    for idx in (1, 2):
        dev = _common()
        dev.update({
            "planStrategyGuid": STRATEGY_D_GUID,
            "jobObjectGuid": station_guid,
            "jobObjectDeviceGuid": f"st-0001-dev{idx}",
            "jobObjectName": "110kV 苍岭变电站",
            "jobObjectDeviceName": f"苍岭变电站#设备{idx}",
            "speciality": "sms",
            "span": "",
            "coveringAirports": "ap-a",
            "filter": 0, "filterTd": 0,
            "sort": idx, "towerBranchName": "1",
        })
        dev.pop("isDeleted", None)   # 输入参数没有 isDeleted
        devices_d.append(dev)
    base_d = {"job_object": "110kV 苍岭变电站", "line_guid": station_guid,
              "work_nature": "1", "work_nature_sub": "s-rcxs", "speciality": "sms",
              "range_start": "2026-11-01 00:00:00", "range_end": "2026-11-05 23:59:59"}
    no_d = "JH-20261116090000004"
    plans_d = [
        build_plan(base_d, seq=1, plan_guid="p-d-0001", plan_no=f"{no_d}-1",
                   pre_start="2026-11-01 00:00:00", pre_end="2026-11-05 23:59:59",
                   cycle_start="2026-11-01 00:00:00", cycle_end="2026-11-05 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-05 23:59:59",
                   one_day=5, time_period="08:30:00-17:30:00",
                   strategy_guid=STRATEGY_D_GUID, job_object_num=2, frequency="周期内1次",
                   speciality="sms", work_nature="1", work_nature_sub="s-rcxs",
                   voltage_level="AC500kV"),
    ]
    strategy_d = build_strategy(STRATEGY_D_GUID, base_d, frequency="周期内1次", one_day=5,
                                time_period="08:30:00-17:30:00",
                                device_list=devices_d, plan_list=plans_d,
                                speciality="sms", work_nature="1", work_nature_sub="s-rcxs")

    device_routes = [
        {"deviceGuid": station_guid, "deviceType": "sms", "trackType": "11",
         "taskPoint": 40, "validating": "1", "isDeleted": 0},
        {"deviceGuid": station_guid, "deviceType": "sms", "trackType": "11",
         "taskPoint": 32, "validating": "1", "isDeleted": 0},
        {"deviceGuid": station_guid, "deviceType": "sms", "trackType": "12",
         "taskPoint": 999, "validating": "1", "isDeleted": 0},   # 不同航迹，不参与
    ]

    # ---------------- 策略 B：通道 5 基，两段时段 ----------------
    def covering_b(i: int) -> dict:
        return {"covering_airports": "ap-a" if i <= 3 else "", "filter": 0, "filter_td": 0}

    spans = {1: 880, 2: 920, 3: 1040, 4: 760, 5: 810}
    towers_b = build_towers(LINE_B_GUID, 5, covering_b, lambda i: spans[i])
    devices_b = [build_device(t, STRATEGY_B_GUID, "110kV 苍岭线(通道)") for t in towers_b]

    base_b = {"job_object": "110kV 苍岭线(通道)", "line_guid": LINE_B_GUID,
              "work_nature": "2", "work_nature_sub": "t-ttdxs",
              "range_start": "2026-11-01 00:00:00", "range_end": "2026-11-14 23:59:59"}
    no_b = "JH-20261116090000002"
    plans_b = [
        build_plan(base_b, seq=1, plan_guid="p-b-0001", plan_no=f"{no_b}-1",
                   pre_start="2026-11-01 08:30:00", pre_end="2026-11-07 12:30:00",
                   cycle_start="2026-11-01 00:00:00", cycle_end="2026-11-07 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-14 23:59:59",
                   one_day=7, time_period="08:30:00-12:30:00",
                   strategy_guid=STRATEGY_B_GUID, job_object_num=5, frequency="每月2次"),
        build_plan(base_b, seq=2, plan_guid="p-b-0002", plan_no=f"{no_b}-2",
                   pre_start="2026-11-01 12:45:00", pre_end="2026-11-07 18:30:00",
                   cycle_start="2026-11-01 00:00:00", cycle_end="2026-11-07 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-14 23:59:59",
                   one_day=7, time_period="12:45:00-18:30:00",
                   strategy_guid=STRATEGY_B_GUID, job_object_num=5, frequency="每月2次"),
        build_plan(base_b, seq=3, plan_guid="p-b-0003", plan_no=f"{no_b}-3",
                   pre_start="2026-11-08 08:30:00", pre_end="2026-11-14 12:30:00",
                   cycle_start="2026-11-08 00:00:00", cycle_end="2026-11-14 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-14 23:59:59",
                   one_day=7, time_period="08:30:00-12:30:00",
                   strategy_guid=STRATEGY_B_GUID, job_object_num=5, frequency="每月2次"),
        build_plan(base_b, seq=4, plan_guid="p-b-0004", plan_no=f"{no_b}-4",
                   pre_start="2026-11-08 12:45:00", pre_end="2026-11-14 18:30:00",
                   cycle_start="2026-11-08 00:00:00", cycle_end="2026-11-14 23:59:59",
                   insp_start="2026-11-01 00:00:00", insp_end="2026-11-14 23:59:59",
                   one_day=7, time_period="12:45:00-18:30:00",
                   strategy_guid=STRATEGY_B_GUID, job_object_num=5, frequency="每月2次"),
    ]
    strategy_b = build_strategy(STRATEGY_B_GUID, base_b, frequency="每月2次", one_day=7,
                                time_period="08:30:00-12:30:00;12:45:00-18:30:00",
                                device_list=devices_b, plan_list=plans_b)

    plan_set = _common()
    plan_set.update({
        "planSetGuid": PLAN_SET_GUID,
        "planSetName": "2026年11月苍岭线巡视计划",
        "areaCode": AREA_CODE, "projectId": PROJECT_ID, "projectName": PROJECT_NAME,
        "hookProjectNum": 1, "speciality": "tms", "jobObjectNum": "2",
        "workNature": "1", "workNatureSub": "t-rcxs",
        "preStartDate": "2026-11-01 00:00:00", "preEndDate": "2026-11-14 23:59:59",
        "rateOfProgress": "0.00", "planSetStatus": "2",
        "totalWorkNum": -1, "completeWorkNum": -1, "statsUpdateTime": "",
    })

    # 基础场景：输电（tms）本体 + 通道
    payload = {
        "planSetInfo": plan_set,
        "planStrategyList": [strategy_a, strategy_b],
        "airports": AIRPORTS,
    }
    out = MOCK_DIR / "split_input.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {out} ({out.stat().st_size} bytes)")
    print(f"  策略A 输电本体：{len(devices_a)} 基杆塔 / {len(plans_a)} 条子计划")
    print(f"  策略B 输电通道：{len(devices_b)} 基杆塔 / {len(plans_b)} 条子计划")

    # 三类巡检齐全：输电 + 配电 + 变电
    multi = {
        "planSetInfo": {**plan_set, "planSetName": "2026年11月三类巡检计划"},
        "planStrategyList": [strategy_a, strategy_b, strategy_c, strategy_d],
        "airports": AIRPORTS,
        "deviceRoutes": device_routes,
    }
    out_multi = MOCK_DIR / "split_input_multi.json"
    out_multi.write_text(json.dumps(multi, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {out_multi} ({out_multi.stat().st_size} bytes)")
    print(f"  策略C 配电本体：{len(devices_c)} 基杆塔 / {len(plans_c)} 条子计划")
    print(f"  策略D 变电整站：{len(devices_d)} 台设备 / {len(plans_d)} 条子计划 / 航点 {len(device_routes)} 条")
    print(f"  机场 {len(AIRPORTS)} 个 / 承载力内置 airport_capacity()")


if __name__ == "__main__":
    main()
