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

import datetime as _dt  # noqa: E402

CST = alerting.CST


def at(y, mo, d, h, mi):
    """构造 CST 时间的 aware datetime（测试里冻结"现在"用）。"""
    return _dt.datetime(y, mo, d, h, mi, tzinfo=CST)

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
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 19, 18, 15))
t("W 正常时不告警", rc == 0 and not alerts, "rc=%s alerts=%s" % (rc, alerts))

log("")
log("=== 3) watchdog：某时段未触发 ===")
alerts.clear()
map3 = {WF_PRAISE: runs_ok(None), WF_CHECK: runs_ok(None), WF_FINAL: []}
with mock.patch.object(alerting, "workflow_runs", side_effect=routes(map3)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 19, 18, 15))
t("W 未触发 → 告警", rc == 0 and len(alerts) == 1, "rc=%s" % rc)
t("W 告警点名缺失时段", alerts and "17:30" in alerts[0], alerts[:1])

log("")
log("=== 4) watchdog：某时段执行失败 ===")
alerts.clear()
map4 = {WF_PRAISE: runs_ok(None),
        WF_CHECK: [{"status": "completed", "conclusion": "failure"}],
        WF_FINAL: runs_ok(None)}
with mock.patch.object(alerting, "workflow_runs", side_effect=routes(map4)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 19, 18, 15))
t("W 执行失败 → 告警", rc == 0 and len(alerts) == 1, "rc=%s" % rc)
t("W 告警点名失败时段", alerts and "15:30" in alerts[0], alerts[:1])

log("")
log("=== 5) watchdog：查询接口异常 ===")
alerts.clear()


def boom(*_a, **_kw):
    raise RuntimeError("403 rate limited")


with mock.patch.object(alerting, "workflow_runs", side_effect=boom), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 19, 18, 15))
t("W 查询失败 → 告警（不静默）", rc == 0 and len(alerts) == 1, "rc=%s" % rc)

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
log("")
log("=== 0) 配置一致性（新项漏登记会让「未到点豁免」静默失效） ===")
_missing_at = sorted({wf for wf, _ in alerting.EXPECTED_WORKFLOWS} - set(alerting.EXPECTED_AT))
t("C 期望时刻覆盖全部提醒项", not _missing_at, _missing_at)

log("")
log("=== 9) 跨午夜迟到归位（09-29 02:09 实况回归） ===")
# 现场：GitHub 原生 schedule 迟到到次日 02:08 CST 才触发，运行时刻取「今天」=09-29，
# 而 09-29 的三个提醒（10:00/15:30/17:30）都还没到点 ⇒ 修复前必然误报。
# 修复后应改为核对「前一日」。⚠️ 本组在修复前会红（rc=2 + 1 条告警）。


def day_runs(by_date):
    """按「CST 日期 → {workflow: runs}」造桩；since 是 UTC 串，换算回 CST 日。"""
    def _f(wf, since, **_kw):
        d = (_dt.datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=_dt.timezone.utc).astimezone(CST))
        return (by_date.get(d.strftime("%Y-%m-%d")) or {}).get(wf, [])
    return _f


OKRUN = {"id": 1, "status": "completed", "conclusion": "success",
         "created_at": "2026-09-28T02:00:00Z", "html_url": "u"}
PREV_ALL_OK = {"2026-09-28": {WF_PRAISE: [OKRUN], WF_CHECK: [OKRUN], WF_FINAL: [OKRUN]}}

alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs(PREV_ALL_OK)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 2, 9))
t("T1 跨午夜迟到 → 回退核对前一日且不告警", rc == 0 and not alerts,
  "rc=%s alerts=%s" % (rc, alerts))

# 前一日真缺 17:30 → 仍然必须告警（不能因为加了豁免就永远闭嘴）
PREV_MISS = {"2026-09-28": {WF_PRAISE: [OKRUN], WF_CHECK: [OKRUN], WF_FINAL: []}}
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs(PREV_MISS)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 2, 9))
t("T2 前一日真缺失 → 仍告警且点名 17:30、日期为前一日",
  rc == 0 and len(alerts) == 1 and "17:30" in alerts[0] and "2026-09-28" in alerts[0],
  "rc=%s alerts=%s" % (rc, alerts[:1]))

# T3 显式指定**历史日期**（--date）回溯核对：全额判定，不受「未到点豁免」影响。
#    这是与 T2 的关键分界：T2 的回退是自动推断，T3 是人工指定 ⇒ 必须按指定日判。
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs(PREV_MISS)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-28", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 2, 9), allow_fallback=False)
t("T3 回溯核对历史日期 → 全额判定（点名 17:30，日期为 09-28）",
  rc == 0 and len(alerts) == 1 and "2026-09-28" in alerts[0] and "17:30" in alerts[0],
  "rc=%s alerts=%s" % (rc, alerts[:1]))

# T3b 显式指定「今天」且三项都未到点：不回退 ⇒ 全 pending ⇒ 不告警（凌晨无从判定今天）
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs(PREV_MISS)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 2, 9), allow_fallback=False)
t("T3b 指定今天且未到点 → 不回退、全 pending、不告警",
  rc == 0 and not alerts, "rc=%s alerts=%s" % (rc, alerts))

log("")
log("=== 10) 未到点豁免（当日部分到点） ===")
# CST 09-29 11:00：10:00 项已到点（+20 分宽容后），15:30/17:30 尚未到点。
# 10:00 项真缺 ⇒ 只应点名 10:00，不得把未到点的两项也算成异常。
ONLY_1000_MISS = {"2026-09-29": {WF_CHECK: [OKRUN], WF_FINAL: [OKRUN]}}
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs(ONLY_1000_MISS)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 11, 0))
t("T4 已到点项缺失 → 告警且只点名 10:00",
  rc == 0 and len(alerts) == 1 and "10:00" in alerts[0]
  and "15:30" not in alerts[0] and "17:30" not in alerts[0],
  "rc=%s alerts=%s" % (rc, alerts[:1]))

# 09:50（10:00 还没到 +20 分宽容）⇒ 三项全 pending ⇒ 不告警
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs({})), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 9, 50), allow_fallback=False)
t("T5 三项全未到点（关回退以隔离）→ pending，不告警", rc == 0 and not alerts,
  "rc=%s alerts=%s" % (rc, alerts))

# T5b 同样 09:50，但**开启**回退 ⇒ 去核对前一日；前一日全 ok ⇒ 不告警。
#     这就是修复的正面效果：凌晨/清早运行不再对着"今天"误报。
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs(PREV_ALL_OK)), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 9, 50))
t("T5b 09:50 开启回退 → 核对前一日(全 ok) ⇒ 不告警",
  rc == 0 and not alerts, "rc=%s alerts=%s" % (rc, alerts))

# 宽容期边界：10:19 仍在宽容内（未到点），10:21 才判异常
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs({})), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 10, 19), allow_fallback=False)
t("T6 宽容期内（10:19，关回退以隔离）不判异常", rc == 0 and not alerts, "rc=%s alerts=%s" % (rc, alerts))
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=day_runs({})), \
     mock.patch.object(alerting, "send_alert", side_effect=fake_alert):
    rc = alerting.watchdog("2026-09-29", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 29, 10, 21))
t("T7 过宽容期（10:21）→ 判异常并告警", rc == 0 and len(alerts) == 1,
  "rc=%s alerts=%s" % (rc, alerts[:1]))

log("")
log("=== 11) 告警未送达 → 交回 Actions 兜底 ===")
alerts.clear()
with mock.patch.object(alerting, "workflow_runs", side_effect=routes(map3)), \
     mock.patch.object(alerting, "send_alert", side_effect=lambda *a, **k: False):
    rc = alerting.watchdog("2026-09-19", repo="o/r", token="t", recheck_delay=0,
                           now=at(2026, 9, 19, 18, 15))
t("T8 告警未送达 → rc=2（触发 if: failure() 再兜一次）", rc == 2, "rc=%s" % rc)

log("---- RESULT: %d pass / %d fail ----" % (PASS, FAIL))
with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")
print("\n".join(LINES))
sys.exit(1 if FAIL else 0)
