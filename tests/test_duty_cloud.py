# -*- coding: utf-8 -*-
"""云端在岗判定离线单测（summary_chain.duty）——不联网、不发送任何消息。

重点回归：2026-09-19 15:38 事故——「事假」成员被旧黑名单判为在岗而误 @。
本测试用当天**真实排班数据**断言修复后只保留可推送者。

运行：
    cd D:\\13445\\Documents\\feishu-alert-sync
    python tests\\test_duty_cloud.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from summary_chain import config, duty  # noqa: E402

REPORT = os.path.join(ROOT, "tests", "_test_duty_cloud_report.txt")
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


def row(member: str, status: str, remark: str = "", rid: str = "") -> dict:
    return {"member": member, "status": status, "remark": remark, "record_id": rid}


MEMBERS = list(config.MEMBERS)

# ---------------------------------------------------------------------------
# 1) 2026-09-19 真实数据（周六）——事故回归
#    排班表实际：方佳莹=事假(备注病假) / 许磊,李燕芳,胡镭=休息 / 李玉婷=加班
#    旧口径 ("休息","请假","节假日") 下「事假」不在黑名单 → 方佳莹被判在岗 → 误 @
# ---------------------------------------------------------------------------
REAL_0919 = [
    row("方佳莹", "事假", "病假", "recvuv4iKB5H4H"),
    row("许磊", "休息", "", "recvuv4iKC5jAh"),
    row("李燕芳", "休息", "", "recvuv4iKCzhTZ"),
    row("胡镭", "休息", "", "recvuv4iKCw2rf"),
    row("李玉婷", "加班", "", "recvuv4iKCTjBW"),
]
push, alerts = duty.pushable_members("2026-09-19", MEMBERS, rows=REAL_0919)
t("R1 09-19 真实数据：仅李玉婷可推送", push == {"李玉婷"}, "pushable=%s" % sorted(push))
t("R2 09-19 事假成员（方佳莹）不再可推送", "方佳莹" not in push)
t("R3 09-19 休息成员均不可推送", not ({"许磊", "李燕芳", "胡镭"} & push))
t("R4 09-19 无重复行/名单外告警", alerts == [], alerts)

# 旧口径对照（证明修复前的行为）
old_off = {r["member"] for r in REAL_0919 if r["status"] in ("休息", "请假", "节假日")}
old_on_duty = [m for m in MEMBERS if m not in old_off]
t("R5 旧黑名单口径确实会把方佳莹判为在岗（复现事故根因）",
  "方佳莹" in old_on_duty, "old_on_duty=%s" % old_on_duty)

# ---------------------------------------------------------------------------
# 2) 各类状态枚举
# ---------------------------------------------------------------------------
for status in ("事假", "病假", "年假", "调休", "休息", "法定休息", "法定节假日", "请假"):
    p, _a = duty.pushable_members("2026-09-22", MEMBERS, rows=[row("方佳莹", status)])
    t("S 状态「%s」不可推送" % status, "方佳莹" not in p, "pushable=%s" % sorted(p))

for status in ("正常上班", "加班", "节假日加班"):
    p, _a = duty.pushable_members("2026-09-22", MEMBERS, rows=[row("方佳莹", status)])
    t("S 状态「%s」可推送" % status, "方佳莹" in p, "pushable=%s" % sorted(p))

p, _a = duty.pushable_members("2026-09-22", MEMBERS, rows=[row("方佳莹", "团建外出")])
t("S 状态「团建外出」计入出勤但不推送", "方佳莹" not in p, "pushable=%s" % sorted(p))

# 历史状态名归一
p, _a = duty.pushable_members("2026-09-22", MEMBERS, rows=[row("方佳莹", "在岗")])
t("S 遗留状态「在岗」→ 正常上班（可推送）", "方佳莹" in p)
p, _a = duty.pushable_members("2026-09-22", MEMBERS, rows=[row("方佳莹", "法定节假日")])
t("S 遗留状态「法定节假日」→ 法定休息（不推送）", "方佳莹" not in p)

# ---------------------------------------------------------------------------
# 3) 无记录时按日历区分
# ---------------------------------------------------------------------------
p, a = duty.pushable_members("2026-09-22", MEMBERS, rows=[])     # 周二
t("C 工作日（09-22 周二）无记录 → 全员默认可推送", p == set(MEMBERS), sorted(p))
t("C 工作日无记录不告警", a == [], a)

p, a = duty.pushable_members("2026-09-19", MEMBERS, rows=[])     # 周六
t("C 周末（09-19 周六）无记录 → 全不可推送", p == set(), sorted(p))
t("C 周末无记录产出告警", any("未登记排班" in x for x in a), a)

p, a = duty.pushable_members("2026-10-01", MEMBERS, rows=[])     # 国庆
t("C 法定节假日（10-01 国庆）无记录 → 全不可推送", p == set(), sorted(p))
t("C 法定节假日无记录告警含节日名", any("法定节假日" in x and "国庆" in x for x in a), a)

p, _a = duty.pushable_members("2026-09-20", MEMBERS, rows=[])    # 周日，但国庆调休上班日
t("C 调休上班日（09-20 周日）无记录 → 全员可推送", p == set(MEMBERS), sorted(p))

# 法定假日标注为「正常上班」→ 照常推送并提示核对
p, a = duty.pushable_members("2026-10-01", MEMBERS, rows=[row("方佳莹", "正常上班")])
t("C 法定假日标「正常上班」仍推送", "方佳莹" in p)
t("C 法定假日标「正常上班」给出核对提醒", any("节假日加班" in x for x in a), a)

# ---------------------------------------------------------------------------
# 4) 重复行 / 名单外成员
# ---------------------------------------------------------------------------
p, a = duty.pushable_members("2026-09-22", MEMBERS,
                             rows=[row("方佳莹", "事假"), row("方佳莹", "加班")])
t("D 同人同日多条取最后一条（事假→加班 = 可推送）", "方佳莹" in p, sorted(p))
t("D 重复行产出告警", any("重复排班记录" in x for x in a), a)

p, a = duty.pushable_members("2026-09-22", MEMBERS, rows=[row("张三", "正常上班")])
t("D 名单外成员不进可推送集合", "张三" not in p)
t("D 名单外成员告警", any("名单外成员" in x for x in a), a)

# ---------------------------------------------------------------------------
# 5) 日期/日历工具
# ---------------------------------------------------------------------------
t("U norm_date 2026/9/19", duty.norm_date("2026/9/19") == "2026-09-19")
t("U norm_date 2026.9.19", duty.norm_date("2026.9.19") == "2026-09-19")
t("U norm_date 带时间", duty.norm_date("2026-09-19 08:00") == "2026-09-19")
t("U norm_date 毫秒时间戳",
  duty.norm_date(1789747200000) == "2026-09-19",
  duty.norm_date(1789747200000))
t("U norm_date 空值", duty.norm_date(None) == "" and duty.norm_date("") == "")
t("U 09-19 是周末且非调休", duty.is_rest_day("2026-09-19") is True)
t("U 09-20 是调休上班日（不算休息日）", duty.is_rest_day("2026-09-20") is False)
t("U 10-01 是法定节假日", duty.is_rest_day("2026-10-01") is True)
t("U 09-22 是工作日", duty.is_rest_day("2026-09-22") is False)
t("U 09-25 中秋假期", duty.is_rest_day("2026-09-25") is True)
t("U 10-10 国庆调休上班", duty.is_rest_day("2026-10-10") is False)
t("U holiday_info 节日名", duty.holiday_info("2026-10-01")["name"] == "国庆节")

# ---------------------------------------------------------------------------
# 6) 配置一致性（三处同源）
# ---------------------------------------------------------------------------
t("G 推送白名单含 3 项", set(config.PUSH_DUTY_STATUSES) == {"正常上班", "加班", "节假日加班"})
t("G 团建外出 = 计入出勤不推送", config.ATTENDANCE_ONLY_STATUSES == ("团建外出",))
t("G OFF_DUTY_STATUSES 已补齐 8 类不推送状态",
  set(config.NOT_PUSH_STATUSES) == {"休息", "法定休息", "法定节假日", "事假",
                                    "病假", "年假", "调休", "请假"},
  config.NOT_PUSH_STATUSES)
t("G 旧黑名单不再包含「事假」漏判（事假已在 NOT_PUSH）", "事假" in config.OFF_DUTY_STATUSES)
t("G 节假日表含 7 个节日", len(config.HOLIDAYS) == 7)
t("G 节假日表起始年 2026", all(h["start"].startswith("2026") for h in config.HOLIDAYS))

log("")
log("---- RESULT: %d pass / %d fail ----" % (PASS, FAIL))

with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")
print("\n".join(LINES))
sys.exit(1 if FAIL else 0)
