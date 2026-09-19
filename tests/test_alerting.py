# -*- coding: utf-8 -*-
"""云端监控报警模块单测（summary_chain.alerting）——不联网、不发送。

覆盖：
  · _verdict 判定：有成功记录 / 全失败 / 未触发 / 仍在执行
  · watchdog：全部正常 → 不告警；缺失或失败 → 告警且内容点名
  · watchdog：缺 GitHub 凭据 → 明确告警（而不是静默通过）
  · job_failure：workflow 失败入口能发出告警
  · send_alert：Webhook 可用时走 Webhook；不可用时降级不抛异常

运行：
    python tests\\test_alerting.py
"""

from __future__ import annotations

import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import summary_chain.alerting as alerting  # noqa: E402
from summary_chain import config  # noqa: E402

REPORT = os.path.join(ROOT, "tests", "_test_alerting_report.txt")
LINES: list[str] = []
PASS = 0
FAIL = 0
alerts: list[str] = []


def log(s: str = "") -> None:
    LINES.append(str(s))


def t(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        log("PASS " + name)
    else:
        FAIL += 1
        log("FAIL " + name + ("  | " + str(extra)[:300] if extra else ""))


def fake_alert(text, *a, **kw):
    alerts.append(text)
    return True


WF_PRAISE = "praise-reminder.yml"
WF_CHECK = "summary-check.yml"
WF_FINAL = "summary-final-sync.yml"


def runs_ok(_wf, *_a, **_kw):
    return [{"id": 1, "status": "completed", "conclusion": "success",
             "created_at": "2026-09-19T02:00:00Z", "html_url": "u"}]


def routes(mapping):
    def _f(wf, *_a, **_kw):
        return mapping.get(wf, [])
    return _f


# ---------------------------------------------------------------------------
log("=== 1) _verdict 判定 ===")
t("V 有成功记录 → ok",
  alerting._verdict([{"status": "completed", "conclusion": "success"}])[0] == "ok")
t("V 无记录 → bad（未触发）",
  alerting._verdict([])[0] == "bad")
t("V 全失败 → bad",
  alerting._verdict([{"status": "completed", "conclusion": "failure"}])[0] == "bad")
t("V 全取消 → bad",
  alerting._verdict([{"status": "completed", "conclusion": "cancelled"}])[0] == "bad")
t("V 仍在执行 → running",
  alerting._verdict([{"status": "in_progress", "conclusion": None}])[0] == "running")
t("V 失败+成功混合 → ok",
  alerting._verdict([{"status": "completed", "conclusion": "failure"},
                     {"status": "completed", "conclusion": "success"}])[0] == "ok")

log("")
log("=== 2) watchdog：全部正常 ===")
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=runs_ok), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0)
t("W 正常时不告警", rc == 0 and not alerts, "rc=%s alerts=%s" % (rc, alerts))

log("")
log("=== 3) watchdog：某时段未触发 ===")
alerts.clear()
map3 = {WF_PRAISE: runs_ok(None), WF_CHECK: runs_ok(None), WF_FINAL: []}
with mock.patch.object(alerting, "workflow_runs", side_effect=routes(map3)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0)
t("W 未触发 → 告警", rc == 2 and len(alerts) == 1, "rc=%s" % rc)
t("W 告警点名缺失时段", alerts and "17:30" in alerts[0], alerts[:1])

log("")
log("=== 4) watchdog：某时段执行失败 ===")
alerts.clear()
map4 = {WF_PRAISE: runs_ok(None),
        WF_CHECK: [{"status": "completed", "conclusion": "failure"}],
        WF_FINAL: runs_ok(None)}
with mock.patch.object(alerting, "workflow_runs", side_effect=routes(map4)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0)
t("W 执行失败 → 告警", rc == 2 and len(alerts) == 1, "rc=%s" % rc)
t("W 告警点名失败时段", alerts and "15:30" in alerts[0], alerts[:1])

log("")
log("=== 5) watchdog：查询接口异常 ===")
alerts.clear()


def boom(*_a, **_kw):
    raise RuntimeError("403 rate limited")


with mock.patch.object(alerting, "workflow_runs", side_effect=boom), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0)
t("W 查询失败 → 告警（不静默）", rc == 2 and len(alerts) == 1, "rc=%s" % rc)

log("")
log("=== 6) watchdog：缺凭据 ===")
alerts.clear()
with mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="", token="", recheck_delay=0)
t("W 缺凭据 → 返回 1 且告警", rc == 1 and len(alerts) == 1, "rc=%s" % rc)

log("")
log("=== 7) job_failure 入口 ===")
alerts.clear()
with mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.job_failure("Daily Summary Check (15:30 CST)", "summary-check",
                              repo="o/r")
t("J 失败告警已发出", rc == 0 and len(alerts) == 1, "rc=%s" % rc)
t("J 告警含任务名", alerts and "Daily Summary Check" in alerts[0], alerts[:1])

log("")
log("=== 8) send_alert 通道行为 ===")
with mock.patch.object(config, "ALERT_WEBHOOK", ""), \
     mock.patch.object(alerting.feishu_api, "send_post_message",
                       side_effect=RuntimeError("bot 未入群")):
    ok = alerting.send_alert("通道全挂测试", strict=False)
t("A 全通道失败 → 返回 False 且不抛异常（兜底打日志）", ok is False)

with mock.patch.object(config, "ALERT_WEBHOOK", ""), \
     mock.patch.object(alerting.feishu_api, "send_post_message",
                       side_effect=RuntimeError("bot 未入群")):
    raised = False
    try:
        alerting.send_alert("strict 模式", strict=True)
    except RuntimeError:
        raised = True
t("A strict 模式全通道失败 → 抛异常", raised)

log("")
log("---- RESULT: %d pass / %d fail ----" % (PASS, FAIL))
with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")
print("\n".join(LINES))
sys.exit(1 if FAIL else 0)
