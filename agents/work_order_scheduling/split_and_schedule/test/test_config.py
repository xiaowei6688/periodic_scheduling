"""排班业务配置测试。"""

import os
import unittest
from unittest.mock import patch

from agents.work_order_scheduling.shared.config import ScheduleSettings


class TestScheduleSettings(unittest.TestCase):
    def test_defaults_match_existing_algorithm_rules(self):
        with patch.dict(os.environ, {}, clear=True):
            configured = ScheduleSettings.from_env()
        self.assertEqual(configured.day_seconds_visible, 28800)
        self.assertEqual(configured.day_seconds_infrared, 21600)
        self.assertEqual(configured.quota_ratio, 0.80)
        self.assertEqual(configured.force_window_visible, (59400, 63000))
        self.assertEqual(configured.force_window_infrared, (75600, 79200))
        self.assertEqual(
            configured.existing_orders_api_path,
            "/api/main-cycle-server/workOrderInfo/getCycleOrderProjectPlanPage",
        )

    def test_environment_overrides_schedule_rules(self):
        with patch.dict(os.environ, {
            "SCHEDULE_QUOTA_RATIO": "0.75",
            "SCHEDULE_FORCE_WINDOW_VISIBLE": "16:00:00-17:00:00",
            "SCHEDULE_SMS_VISIBLE_START": "09:00:00",
        }, clear=False):
            configured = ScheduleSettings.from_env()
        self.assertEqual(configured.quota_ratio, 0.75)
        self.assertEqual(configured.force_window_visible, (57600, 61200))
        self.assertEqual(configured.sms_visible_start, 32400)

    def test_invalid_ratio_is_rejected(self):
        with patch.dict(os.environ, {"SCHEDULE_QUOTA_RATIO": "1.2"}):
            with self.assertRaisesRegex(ValueError, "SCHEDULE_QUOTA_RATIO"):
                ScheduleSettings.from_env()


if __name__ == "__main__":
    unittest.main()
