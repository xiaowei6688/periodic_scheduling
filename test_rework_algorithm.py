"""重排算法单元测试（按 8 层规则逐层验证）。

测试构造的工单使用与真实集成一致的「原始形态」字段名，
再经 canonicalize() 规整，确保测试覆盖真实链路。

    python -m unittest test_rework_algorithm -v
"""

from __future__ import annotations

import unittest

from rework_algorithm import (
    Calendar,
    Scheduler,
    _occupy_existing,
    _priority,
    canonicalize,
    categorize_old,
    effective_limit,
    effective_time_period,
    iter_days,
    largest_remainder_split,
    parse_time_windows,
    preferred_airport,
    rework,
    schedule_manual,
)

DEFAULT_WINDOW = ("2026-12-01 00:00:00", "2026-12-10 23:59:59")
DAY_WINDOW = "08:30:00-17:30:00"     # 与旧工单默认窗口一致，9 小时


def new_order(guid: str, seconds: int, pre_start: str, pre_end: str,
              airport: str = "AP", tp: str = DAY_WINDOW, **kw) -> dict:
    """拆单产出形态：plan 挂详情 + 工单层字段。"""
    order = {
        "work_order_guid": guid, "work_order_no": f"ZQ-{guid}", "plan_guid": f"p-{guid}",
        "cycle_airport_guid": airport, "second_num": seconds, "capacity_limit": None,
        "inspection_method": "1", "time_period": tp,
        "pre_start_date": pre_start, "pre_end_date": pre_end,
        "start_date": None, "end_date": None, "source": "new",
        "plan": {"speciality": "tms", "workNature": "1", "workNatureSub": "t-rcxs", "oneDay": 1},
    }
    order.update(kw)
    return order


def old_order(guid: str, seconds: int, start: str, end: str, status: str,
              airport: str = "AP", pre_start: str | None = None,
              pre_end: str | None = None, speciality: str = "tms", **kw) -> dict:
    """归一化后旧工单形态（rework_orders.normalize_existing 的产出）。"""
    order = {
        "work_order_guid": guid, "work_order_no": f"ZQ-{guid}", "plan_guid": f"p-{guid}",
        "cycle_airport_guid": airport, "second_num": seconds, "capacity_limit": None,
        "inspection_method": "1",
        "work_order_status": status,
        "professional_category": speciality, "work_nature": "1",
        "work_nature_sub": "t-rcxs", "one_day": 1,
        "pre_start_date": pre_start, "pre_end_date": pre_end,
        "start_date": start, "end_date": end, "source": "existing",
    }
    order.update(kw)
    return order


def build_cbd_scenario():
    """文档2 的 C→B→D 链式重排示例，全部落在旧工单默认窗口 08:30-17:30 内。"""
    window = ("2026-12-01 00:00:00", "2026-12-10 23:59:59")
    old = [
        old_order("f1", 32400, "2026-12-01 08:30:00", "2026-12-01 17:30:00", "8"),
        old_order("f2", 25200, "2026-12-02 08:30:00", "2026-12-02 15:30:00", "8"),
        old_order("f3", 32400, "2026-12-03 08:30:00", "2026-12-03 17:30:00", "8"),
        old_order("f4", 25200, "2026-12-04 08:30:00", "2026-12-04 15:30:00", "8"),
        old_order("B", 7200, "2026-12-02 15:30:00", "2026-12-02 17:30:00", "9",
                  pre_start="2026-12-01 00:00:00", pre_end="2026-12-04 23:59:59"),
        old_order("D", 7200, "2026-12-04 15:30:00", "2026-12-04 17:30:00", "9",
                  pre_start="2026-12-04 00:00:00", pre_end="2026-12-08 23:59:59"),
    ]
    new = [new_order("C", 7200, "2026-12-02 00:00:00", "2026-12-02 23:59:59")]
    return new, old, window


class TestBasics(unittest.TestCase):
    def test_parse_windows(self):
        self.assertEqual(parse_time_windows(DAY_WINDOW), [(30600, 63000)])
        self.assertEqual(parse_time_windows("08:30:00-12:30:00;12:45:00-18:30:00"),
                         [(30600, 45000), (45900, 66600)])
        self.assertEqual(parse_time_windows(""), [])

    def test_effective_default_window(self):
        visible = canonicalize(old_order("a", 1, "2026-12-01 08:00:00", "2026-12-01 09:00:00", "9"))
        self.assertIn("08:30", effective_time_period(visible))
        infrared = canonicalize(old_order("b", 1, "2026-12-01 08:00:00", "2026-12-01 09:00:00", "9",
                                          work_nature_sub="t-hwxs"))
        self.assertIn("05:00", effective_time_period(infrared))

    def test_iter_days_inclusive(self):
        days = iter_days("2026-12-01 00:00:00", "2026-12-03 23:59:59")
        self.assertEqual(days, ["2026-12-01", "2026-12-02", "2026-12-03"])

    def test_effective_limit(self):
        o = canonicalize(new_order("x", 7200, "2026-12-01", "2026-12-01",
                                   tp="08:00:00-11:00:00"))
        # 时段 3h=10800 < 配额 23040 -> 10800
        self.assertEqual(effective_limit(o), 10800)

    def test_largest_remainder(self):
        self.assertEqual(sum(largest_remainder_split(5, [31, 30])), 5)
        self.assertEqual(largest_remainder_split(1, [31, 30]), [1, 0])
        self.assertEqual(largest_remainder_split(2, [1, 1]), [1, 1])


class TestLayer2(unittest.TestCase):
    def test_substation_fixed(self):
        orders = [canonicalize(old_order("s", 3600, "2026-12-01 08:00:00", "2026-12-01 09:00:00",
                                         "9", speciality="sms"))]
        cat = categorize_old(orders, *DEFAULT_WINDOW)
        self.assertEqual([o["guid"] for o in cat.fixed], ["s"])
        self.assertEqual(cat.reworkable, [])

    def test_status9_reworkable(self):
        orders = [canonicalize(old_order("b", 3600, "2026-12-01 08:00:00", "2026-12-01 09:00:00", "9"))]
        cat = categorize_old(orders, *DEFAULT_WINDOW)
        self.assertEqual([o["guid"] for o in cat.reworkable], ["b"])

    def test_status16_zero_seconds_not_occupied(self):
        orders = [canonicalize(old_order("z", 0, "2026-12-01 08:00:00", "2026-12-01 08:00:00", "16"))]
        cat = categorize_old(orders, *DEFAULT_WINDOW)
        self.assertEqual(cat.fixed, [])
        self.assertEqual(cat.reworkable, [])

    def test_status8_12_fixed(self):
        for status in ("8", "12"):
            with self.subTest(status=status):
                orders = [canonicalize(old_order("f", 3600, "2026-12-01 08:00:00",
                                                 "2026-12-01 09:00:00", status))]
                cat = categorize_old(orders, *DEFAULT_WINDOW)
                self.assertEqual([o["guid"] for o in cat.fixed], ["f"])

    def test_reworkable_window_must_intersect_range(self):
        """预执行窗口与请求范围不相交的状态9工单，不进入本次任务（接口 5.2）。"""
        inside = canonicalize(old_order("in", 3600, "2026-12-05 08:00:00", "2026-12-05 09:00:00",
                                        "9", pre_start="2026-12-05 00:00:00",
                                        pre_end="2026-12-05 23:59:59"))
        outside = canonicalize(old_order("out", 3600, "2026-11-01 08:00:00", "2026-11-01 09:00:00",
                                         "9", pre_start="2026-11-01 00:00:00",
                                         pre_end="2026-11-01 23:59:59"))
        cat = categorize_old([inside, outside], *DEFAULT_WINDOW)
        self.assertEqual([o["guid"] for o in cat.reworkable], ["in"])
        self.assertEqual(cat.fixed, [])

    def test_unplaced_reason_not_occupied(self):
        """已标记「未能排下」的排队工单，保留窗口不是真实占用（文档2 §六）。"""
        sched = Scheduler(*DEFAULT_WINDOW)
        order = canonicalize(old_order("x", 3600, "2026-12-01 08:00:00",
                                       "2026-12-01 09:00:00", "9"))
        order["schedule_reason"] = "超量均摊；未能排下"
        _occupy_existing(sched, order, [])
        self.assertEqual(sched.calendar("AP").occupied("2026-12-01"), [])

    def test_failed_reason_but_not_status9_still_occupied(self):
        """非 9 状态带「未能排下」字样仍是真实占用（对齐 Java 6572 的 status==9 限定）。"""
        sched = Scheduler(*DEFAULT_WINDOW)
        order = canonicalize(old_order("y", 3600, "2026-12-01 08:00:00",
                                       "2026-12-01 09:00:00", "8"))
        order["schedule_reason"] = "超量均摊；未能排下"
        _occupy_existing(sched, order, [])
        self.assertEqual(sched.calendar("AP").occupied("2026-12-01"), [(28800, 32400)])

    def test_priority_smaller_window_first(self):
        """同等条件下，原始时间窗口越小优先级越高（文档2 §四）。"""
        a = canonicalize(new_order("a", 7200, "2026-12-01 00:00:00", "2026-12-01 23:59:59"))
        b = canonicalize(new_order("b", 7200, "2026-12-01 00:00:00", "2026-12-10 23:59:59"))
        self.assertLess(_priority(a), _priority(b))


class TestCalendar(unittest.TestCase):
    def test_free_intervals(self):
        cal = Calendar()
        cal.place("a", "2026-12-01", [(28800, 43200)], 14400)   # 08:00-12:00
        free = cal.free_intervals("2026-12-01", [(28800, 50400)])
        self.assertEqual(free, [(43200, 50400)])

    def test_can_place_fragmented(self):
        cal = Calendar()
        cal.place("a", "2026-12-01", [(28800, 43200)], 14400)
        cal.place("b", "2026-12-01", [(46800, 50400)], 3600)
        order = canonicalize(new_order("c", 7200, "2026-12-01", "2026-12-01",
                                       tp="08:00:00-15:00:00"))
        frags = cal.can_place(order, "2026-12-01", enforce_quota=False)
        self.assertIsNotNone(frags)
        total = sum(e - s for s, e in frags)
        self.assertEqual(total, 7200)

    def test_can_place_quota_blocks(self):
        cal = Calendar()
        cal.place("a", "2026-12-01", [(28800, 43200)], 14400)   # 已 4h
        order = canonicalize(new_order("c", 18000, "2026-12-01", "2026-12-01",
                                       tp="08:00:00-17:30:00"))
        # 4h + 5h = 9h > 6.4h 配额
        self.assertIsNone(cal.can_place(order, "2026-12-01", enforce_quota=True))
        self.assertIsNotNone(cal.can_place(order, "2026-12-01", enforce_quota=False))


class TestLayer456(unittest.TestCase):
    """文档2 的 C→B→D 链式重排示例。"""

    def test_chain_rework_c_b_d(self):
        new, old, window = build_cbd_scenario()
        result = rework(new, old, *window, enforce_quota=False)
        self.assertEqual(result.entry, "insert_rework")
        moved = [s.moved_guid for s in result.rework_steps]
        self.assertEqual(moved, ["B", "D"])
        self.assertEqual([s.preempted_by for s in result.rework_steps], ["C", "B"])
        c = next(p for p in result.new_placements if p["work_order_guid"] == "C")
        self.assertEqual(c["start"], "2026-12-02 15:30:00")
        d = next(x for x in result.changed_old_orders if x["work_order_guid"] == "D")
        self.assertTrue(d["new_start"].startswith("2026-12-05"))

    def test_normal_entry_no_chain(self):
        old = [old_order("f", 14400, "2026-12-01 08:00:00", "2026-12-01 12:00:00", "8")]
        new = [new_order("C", 7200, "2026-12-02 00:00:00", "2026-12-02 23:59:59")]
        result = rework(new, old, *DEFAULT_WINDOW, enforce_quota=False)
        self.assertEqual(result.entry, "normal")
        self.assertEqual(result.rework_steps, [])
        self.assertEqual(len(result.new_placements), 1)

    def test_cycle_prevention(self):
        """A 抢 B、B 只能抢 A 时，必须失败而不是死循环。"""
        window = ("2026-12-01 00:00:00", "2026-12-01 23:59:59")
        old = [
            old_order("A", 16200, "2026-12-01 08:30:00", "2026-12-01 13:00:00", "9",
                      pre_start="2026-12-01 00:00:00", pre_end="2026-12-01 23:59:59"),
            old_order("B", 16200, "2026-12-01 13:00:00", "2026-12-01 17:30:00", "9",
                      pre_start="2026-12-01 00:00:00", pre_end="2026-12-01 23:59:59"),
        ]
        new = [new_order("C", 16200, "2026-12-01 00:00:00", "2026-12-01 23:59:59")]
        result = rework(new, old, *window, enforce_quota=False)
        self.assertEqual(result.entry, "force_insert")
        self.assertEqual(len(result.unplaced), 1)


class TestLayer67(unittest.TestCase):
    def test_force_proposal_protected_holders(self):
        window = ("2026-12-01 00:00:00", "2026-12-01 23:59:59")
        old = [old_order("f", 32400, "2026-12-01 08:30:00", "2026-12-01 17:30:00", "8")]
        new = [new_order("C", 7200, "2026-12-01 00:00:00", "2026-12-01 23:59:59")]
        result = rework(new, old, *window, enforce_quota=False)
        self.assertEqual(result.entry, "force_insert")
        self.assertEqual(result.force_proposals[0].order_guid, "C")
        self.assertIn("f", result.force_proposals[0].protected_holders)
        self.assertEqual(result.changed_old_orders, [])

    def test_change_set_fields(self):
        new, old, window = build_cbd_scenario()
        result = rework(new, old, *window, enforce_quota=False)
        d = next(x for x in result.changed_old_orders if x["work_order_guid"] == "D")
        self.assertEqual(set(d), {"work_order_guid", "preempted_by",
                                  "old_start", "old_end", "new_start", "new_end"})
        self.assertEqual(d["old_start"], "2026-12-04 15:30:00")
        self.assertTrue(d["new_start"].startswith("2026-12-05"))


class TestLayer8(unittest.TestCase):
    def _manual(self, guid: str, plan: str, ps: str, pe: str) -> dict:
        return {
            "work_order_guid": guid, "work_order_no": f"RX-{guid}", "plan_guid": plan,
            "cycle_airport_guid": None, "second_num": None, "capacity_limit": None,
            "inspection_method": "2", "professional_category": "tms", "work_nature": "1",
            "work_nature_sub": "t-rcxs", "one_day": 1, "work_order_status": "9",
            "pre_start_date": ps, "pre_end_date": pe,
            "start_date": None, "end_date": None, "source": "new",
        }

    def test_schedule_manual_weekly(self):
        orders = [self._manual(f"m{i}", "plan-x",
                               "2026-12-01 00:00:00", "2027-01-31 23:59:59")
                  for i in range(4)]
        result = schedule_manual([canonicalize(o) for o in orders],
                                 "2026-12-01 00:00:00", "2027-01-31 23:59:59")
        self.assertEqual(len(result), 4)
        months = {r["month"] for r in result}
        self.assertEqual(months, {"2026-12", "2027-01"})
        dec_weeks = [r["week_start"] for r in result if r["month"] == "2026-12"]
        self.assertEqual(len(dec_weeks), len(set(dec_weeks)))

    def test_manual_uses_own_window(self):
        orders = [self._manual("m0", "plan-y",
                               "2026-12-01 00:00:00", "2026-12-31 23:59:59")]
        result = schedule_manual([canonicalize(o) for o in orders],
                                 "2026-11-01 00:00:00", "2026-11-30 23:59:59")
        self.assertEqual(result[0]["month"], "2026-12")


class TestFinalOutput(unittest.TestCase):
    """排班后的最终输出：新增一组 + 按 guid 修改的旧单一组。"""

    def test_two_group_shape(self):
        new, old, window = build_cbd_scenario()
        result = rework(new, old, *window, enforce_quota=False)
        out = result.to_dict()
        self.assertEqual(set(out), {"entry", "window", "new", "update",
                                    "still_unplaced_old", "unplaced_new",
                                    "force_proposals", "rework_chain", "warnings"})

    def test_still_unplaced_old_listed_not_in_groups(self):
        """此前「未能排下」的旧单：不占位、不重排，显式列出，但不进 new/update 两组。"""
        window = ("2026-12-01 00:00:00", "2026-12-10 23:59:59")
        old = [old_order("u", 3600, "2026-12-01 08:00:00", "2026-12-01 09:00:00", "9",
                         schedule_reason="超量均摊；未能排下")]
        new = [new_order("C", 7200, "2026-12-02 00:00:00", "2026-12-02 23:59:59")]
        result = rework(new, old, *window, enforce_quota=False)
        self.assertEqual([o["work_order_guid"] for o in result.still_unplaced_old], ["u"])
        new_guids = {o.get("work_order_guid") for o in result.new_orders_out}
        update_guids = {o.get("work_order_guid") for o in result.update_orders}
        self.assertNotIn("u", new_guids)
        self.assertNotIn("u", update_guids)
        # 它不占日历，新单也不受影响
        self.assertEqual(result.entry, "normal")

    def test_new_group_carries_scheduled_times(self):
        new, old, window = build_cbd_scenario()
        result = rework(new, old, *window, enforce_quota=False)
        c = next(o for o in result.new_orders_out if o["work_order_guid"] == "C")
        self.assertEqual(c["start_date"], "2026-12-02 15:30:00")
        self.assertEqual(c["end_date"], "2026-12-02 17:30:00")

    def test_update_group_is_keyed_by_guid(self):
        new, old, window = build_cbd_scenario()
        result = rework(new, old, *window, enforce_quota=False)
        guids = sorted(u["work_order_guid"] for u in result.update_orders)
        self.assertEqual(guids, ["B", "D"])
        d = next(u for u in result.update_orders if u["work_order_guid"] == "D")
        self.assertEqual(set(d), {"work_order_guid", "work_order_no", "start_date", "end_date"})
        self.assertTrue(d["start_date"].startswith("2026-12-05"))

    def test_unplaced_new_keeps_null_times(self):
        window = ("2026-12-01 00:00:00", "2026-12-01 23:59:59")
        old = [old_order("f", 32400, "2026-12-01 08:30:00", "2026-12-01 17:30:00", "8")]
        new = [new_order("C", 7200, "2026-12-01 00:00:00", "2026-12-01 23:59:59")]
        result = rework(new, old, *window, enforce_quota=False)
        c = next(o for o in result.new_orders_out if o["work_order_guid"] == "C")
        self.assertIsNone(c["start_date"])
        self.assertIsNone(c["end_date"])
        self.assertEqual(len(result.unplaced), 1)

    def test_rework_default_enforces_quota(self):
        # 5h + 2.5h = 7.5h > 80% 配额 6.4h → 默认配额约束下排不进
        window = ("2026-12-01 00:00:00", "2026-12-01 23:59:59")
        old = [old_order("f", 18000, "2026-12-01 08:30:00", "2026-12-01 13:30:00", "8")]
        new = [new_order("C", 9000, "2026-12-01 00:00:00", "2026-12-01 23:59:59")]
        result = rework(new, old, *window)
        self.assertEqual(result.entry, "force_insert")
        self.assertEqual(len(result.unplaced), 1)


class TestDocRuleGaps(unittest.TestCase):
    """上一轮补齐的文档缺口：跳过日期、变电08:30、允许抢占标志、强插影响链、人工筛选、旧单机场。"""

    def test_skip_weekday_and_holiday(self):
        sched = Scheduler(*DEFAULT_WINDOW, skip_weekdays=[7], holidays=["2026-12-06"])
        self.assertTrue(sched._skip("2026-12-06"))   # 2026-12-06 周日
        self.assertTrue(sched._skip("2026-12-13"))   # 周日
        self.assertFalse(sched._skip("2026-12-07"))  # 周一

    def test_substation_visible_starts_at_0830(self):
        raw = {
            "work_order_guid": "s1", "work_order_no": "ZQ-S1", "plan_guid": "p1",
            "cycle_airport_guid": "AP", "second_num": 3600, "capacity_limit": None,
            "inspection_method": "1", "time_period": "08:00:00-12:00:00",
            "pre_start_date": "2026-12-01 00:00:00", "pre_end_date": "2026-12-01 23:59:59",
            "start_date": None, "end_date": None, "source": "new",
            "plan": {"speciality": "sms", "workNature": "1", "workNatureSub": "s-rcxs", "oneDay": 1},
        }
        result = rework([raw], [], *DEFAULT_WINDOW, enforce_quota=True)
        self.assertEqual(result.entry, "normal")
        self.assertEqual(result.new_placements[0]["start"], "2026-12-01 08:30:00")

    def test_force_proposal_lists_affected_orders(self):
        window = ("2026-12-01 00:00:00", "2026-12-01 23:59:59")
        old = [old_order("f", 32400, "2026-12-01 08:30:00", "2026-12-01 17:30:00", "8")]
        new = [new_order("C", 7200, "2026-12-01 00:00:00", "2026-12-01 23:59:59")]
        result = rework(new, old, *window, enforce_quota=False)
        proposal = result.force_proposals[0]
        self.assertEqual(proposal.order_guid, "C")
        self.assertIn("f", proposal.protected_holders)
        affected = {a["work_order_guid"]: a for a in proposal.affected_orders}
        self.assertIn("f", affected)
        self.assertEqual(affected["f"]["relation"], "direct")

    def test_manual_eligible_filters(self):
        def manual(guid, status="9", source="5", reason=None):
            raw = {
                "work_order_guid": guid, "work_order_no": f"RX-{guid}", "plan_guid": "p-m",
                "cycle_airport_guid": None, "second_num": None, "capacity_limit": None,
                "inspection_method": "2", "professional_category": "tms", "work_nature": "1",
                "work_nature_sub": "t-rcxs", "one_day": 1, "work_order_status": status,
                "order_source": source, "schedule_reason": reason,
                "pre_start_date": "2026-12-01 00:00:00", "pre_end_date": "2027-01-31 23:59:59",
                "start_date": None, "end_date": None, "source": "new",
            }
            return canonicalize(raw)

        ok = manual("m1")
        status8 = manual("m2", status="8")
        blocked = manual("m3", reason="补巡待确认排班")
        got = schedule_manual([ok, status8, blocked], *DEFAULT_WINDOW)
        self.assertEqual([m["work_order_guid"] for m in got], ["m1"])

    def test_preferred_airport_common_first(self):
        details = [{"coveringAirports": "ap-x,ap-y"}, {"coveringAirports": "ap-x"}]
        self.assertEqual(preferred_airport(details, "ap-old"), "ap-x")
        # 无共同首位机场 → 不能改选次优，沿用原机场
        self.assertEqual(preferred_airport(
            [{"coveringAirports": "ap-x"}, {"coveringAirports": "ap-z"}], "ap-old"), "ap-old")
        # 明细缺失 → 沿用原机场
        self.assertEqual(preferred_airport(None, "ap-old"), "ap-old")


if __name__ == "__main__":
    unittest.main(verbosity=2)