# -*- coding: utf-8 -*-
"""云端提醒链路整体校验（推送前必跑）。

覆盖：
  ① 5 个 workflow 的 YAML 结构可解析，关键元素齐全
     （失败告警步骤、dry-run 输入、看门狗权限/cron、总开关）
  ② 5 个 python 模块语法可编译
  ③ 回归：在岗判定单测 + 整链路 dry-run（含 --dry-run 不发送断言）

运行：
    python tests\\check_all.py
"""

from __future__ import annotations

import os
import py_compile
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WF_DIR = os.path.join(ROOT, ".github", "workflows")
REPORT = os.path.join(ROOT, "tests", "_check_all_report.txt")
PY = sys.executable

LINES: list[str] = []
PASS = 0
FAIL = 0


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


def load_wf(fn: str):
    with open(os.path.join(WF_DIR, fn), encoding="utf-8") as f:
        return yaml.safe_load(f)


def triggers(d: dict):
    """YAML 1.1 会把 key `on` 解析成布尔 True，这里兼容取回。"""
    return d.get("on") if "on" in d else d.get(True)


def steps_of(d: dict) -> list[dict]:
    out = []
    for job in (d.get("jobs") or {}).values():
        out.extend(job.get("steps") or [])
    return out


def has_failure_alert(d: dict) -> bool:
    return any(s.get("name") == "Alert on failure" and s.get("if") == "failure()"
               for s in steps_of(d))


# ---------------------------------------------------------------------------
log("=== ① workflow YAML 结构 ===")
ALL_WF = ["sync-alerts.yml", "exam-overdue.yml", "praise-reminder.yml",
          "summary-check.yml", "summary-final-sync.yml", "notify-watchdog.yml"]
parsed: dict[str, dict] = {}
for fn in ALL_WF:
    p = os.path.join(WF_DIR, fn)
    if not os.path.isfile(p):
        t("WF 存在 %s" % fn, False, p)
        continue
    try:
        d = load_wf(fn)
        parsed[fn] = d
        t("WF 可解析 %s" % fn, isinstance(d, dict) and "jobs" in d)
    except Exception as e:  # noqa: BLE001
        t("WF 可解析 %s" % fn, False, repr(e))

REMIND_WF = ["praise-reminder.yml", "summary-check.yml", "summary-final-sync.yml"]
for fn in REMIND_WF:
    d = parsed.get(fn)
    if not d:
        continue
    t("WF 失败告警步骤 %s" % fn, has_failure_alert(d))
    trig = triggers(d) or {}
    inputs = ((trig.get("workflow_dispatch") or {}).get("inputs") or {})
    t("WF 含 dry_run 输入 %s" % fn, "dry_run" in inputs,
      list(inputs.keys()))
    t("WF 保留总开关 %s" % fn,
      any("CLOUD_CHAIN_ENABLED" in str((j.get("if") or ""))
          for j in (d.get("jobs") or {}).values()))

wd = parsed.get("notify-watchdog.yml")
if wd:
    trig = triggers(wd) or {}
    sched = trig.get("schedule") or []
    crons = [s.get("cron") for s in sched]
    t("看门狗 cron = 18:15 CST", "15 10 * * *" in crons, crons)
    t("看门狗声明 actions: read 权限",
      ((wd.get("permissions") or {}).get("actions")) == "read",
      wd.get("permissions"))
    job = list((wd.get("jobs") or {}).values())[0]
    env = job.get("env") or {}
    t("看门狗注入 GITHUB_TOKEN", "GITHUB_TOKEN" in env)
    t("看门狗调用 alerting --watchdog",
      any("alerting --watchdog" in str(s.get("run") or "") for s in steps_of(wd)))
    t("看门狗含失败告警步骤", has_failure_alert(wd))

log("")
log("=== ② python 语法编译 ===")
MODS = ["duty.py", "alerting.py", "config.py", "summary_chain.py",
        "praise_reminder.py", "feishu_api.py", "queue_consumer.py"]
for m in MODS:
    src = os.path.join(ROOT, "summary_chain", m)
    try:
        py_compile.compile(src, doraise=True)
        t("编译 %s" % m, True)
    except Exception as e:  # noqa: BLE001
        t("编译 %s" % m, False, repr(e))

log("")
log("=== ③ 回归测试（子进程） ===")
for script, label in (("test_duty_cloud.py", "在岗判定单测"),
                      ("test_chain_dryrun.py", "整链路 dry-run"),
                      ("test_alerting.py", "监控报警单测")):
    p = os.path.join(ROOT, "tests", script)
    r = subprocess.run([PY, p], capture_output=True, text=True, encoding="utf-8")
    tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
    t("回归 %s" % label, r.returncode == 0, f"rc={r.returncode} {tail}")

log("")
log("---- RESULT: %d pass / %d fail ----" % (PASS, FAIL))
with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")
print("\n".join(LINES))
sys.exit(1 if FAIL else 0)
