# -*- coding: utf-8 -*-
"""tab_reminder 单测：时间解析 / 窗口命中 / 跨日 / 负责人解析（纯逻辑，不联网）。"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from summary_chain import tab_reminder as tr  # noqa: E402

CST = timezone(timedelta(hours=8))


def at(h, m, day=16):
    return datetime(2026, 10, day, h, m, tzinfo=CST)


class ParseHHMM(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(tr.parse_hhmm("10:00"), (10, 0))
        self.assertEqual(tr.parse_hhmm("9:30"), (9, 30))
        self.assertEqual(tr.parse_hhmm("23:59"), (23, 59))

    def test_tolerant(self):
        self.assertEqual(tr.parse_hhmm(" 10：00 "), (10, 0))       # 全角冒号+空白
        self.assertEqual(tr.parse_hhmm("10:05"), (10, 5))

    def test_invalid(self):
        for bad in (None, "", "   ", "10点", "25:00", "10:60", "abc", "10"):
            self.assertIsNone(tr.parse_hhmm(bad), bad)


class LoadDueTasks(unittest.TestCase):
    TABS = [
        {"fields": {"名称": "任务A", "负责人": "张三、李四", "提醒时间": "10:03"}},
        {"fields": {"名称": "任务B", "负责人": "王五", "提醒时间": "10:05"}},
        {"fields": {"名称": "任务C", "负责人": "赵六", "提醒时间": "09:00"}},
        {"fields": {"名称": "任务D", "负责人": "钱七", "提醒时间": ""}},
        {"fields": {"名称": "任务E", "负责人": "孙八", "提醒时间": "10:03：坏值"}},
    ]

    def _load(self, start, end):
        with mock.patch.object(tr, "base_search", return_value=self.TABS):
            return tr.load_due_tasks(start, end)

    def test_window_hit(self):
        due = self._load(at(10, 0), at(10, 5))
        self.assertEqual([d["name"] for d in due], ["任务A", "任务B"])

    def test_window_start_exclusive(self):
        """上次运行恰在 10:03 ⇒ 任务A(10:03) 已被上一轮覆盖，本轮不得重复。"""
        due = self._load(at(10, 3), at(10, 5))
        self.assertEqual([d["name"] for d in due], ["任务B"])

    def test_before_window(self):
        due = self._load(at(10, 4), at(10, 5))
        self.assertEqual([d["name"] for d in due], ["任务B"])

    def test_cross_midnight(self):
        """窗口跨日：(23:58 → 00:02] 应命中 00:01 与 23:59 的任务。"""
        tabs = [
            {"fields": {"名称": "夜任务", "负责人": "张三", "提醒时间": "23:59"}},
            {"fields": {"名称": "晨任务", "负责人": "李四", "提醒时间": "00:01"}},
        ]
        with mock.patch.object(tr, "base_search", return_value=tabs):
            due = tr.load_due_tasks(at(23, 58, day=15), at(0, 2, day=16))
        self.assertEqual(sorted(d["name"] for d in due), ["夜任务", "晨任务"])

    def test_owners_dedup(self):
        due = self._load(at(10, 2), at(10, 4))
        self.assertEqual(due[0]["owners"], ["张三", "李四"])
        self.assertEqual(due[0]["hhmm"], "10:03")


class WindowStart(unittest.TestCase):
    def test_cap_when_no_prev(self):
        """查不到上次运行 ⇒ 30 分钟封顶。"""
        now = at(14, 0)
        with mock.patch.object(tr, "_gh_get", side_effect=RuntimeError("boom")):
            start = tr.window_start(now, self_run_id="999")
        self.assertEqual(start, now - timedelta(minutes=tr.WINDOW_CAP_MIN))

    def test_prev_run_wins(self):
        """上次运行 3 分钟前 ⇒ 窗口起点=上次运行（而非 30 分钟前）。"""
        now = at(14, 5)
        prev_iso = (now - timedelta(minutes=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = {"workflow_runs": [
            {"id": 555, "created_at": prev_iso},                     # 非自身、最近
            {"id": 444, "created_at": "2026-10-16T05:00:00Z"},
        ]}
        with mock.patch.object(tr, "_gh_get", return_value=data):
            start = tr.window_start(now, self_run_id="999")
        self.assertEqual(start, now - timedelta(minutes=3))

    def test_self_excluded(self):
        """列表里第一条是自己（GITHUB_RUN_ID）⇒ 跳过自己取更早的。"""
        now = at(14, 5)
        prev_iso = (now - timedelta(minutes=2)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self_iso = (now - timedelta(seconds=5)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = {"workflow_runs": [
            {"id": "999", "created_at": self_iso},                   # 自己
            {"id": 555, "created_at": prev_iso},
        ]}
        with mock.patch.object(tr, "_gh_get", return_value=data):
            start = tr.window_start(now, self_run_id="999")
        self.assertEqual(start, now - timedelta(minutes=2))


if __name__ == "__main__":
    unittest.main()
