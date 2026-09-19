# -*- coding: utf-8 -*-
"""在岗推送判定（云端链路）——口径与本地 v44 完全同源。

背景（2026-09-19 事故）：
  云端 summary_chain / praise_reminder 原先各自用一套**旧黑名单**判定
  （config.OFF_DUTY_STATUSES = 休息/请假/节假日）。而排班表状态体系自
  v39 起已重构为「正常上班/团建外出/加班/休息/法定休息/事假/病假/年假/
  调休/节假日加班」——旧黑名单既不含「事假/病假/年假/调休/法定休息」，
  也不含新语义 → 事假成员被判为**在岗**，导致误推（2026-09-19 15:38
  方佳莹「事假」仍被 @ 提醒）。

本模块是云端链路**唯一的**在岗判定实现，规则与本地
`sync_daily_summary.pushable_names()`、页面 `notifyDuty()` 保持一致：

  A. 当日有排班记录 → 取**最后一条**为准（重复行告警）：
       · 状态 ∈ PUSH_DUTY_STATUSES（正常上班/加班/节假日加班）→ 可推送；
       · 「团建外出」→ 计入出勤但**不推送**；
       · 其余（休息/法定休息/各类假期）→ 不推送。
  B. 当日无记录 → 按日历性质：
       · 工作日（周一–周五）或**法定调休上班日** → 默认正常上班，可推送
         （排班表既有约定是「有偏离才记录」，工作日不记录属正常）；
       · 周末 / 法定节假日 → 不在岗，不推送，并告警提示补录加班。
  C. 法定节假日由 HOLIDAYS_2026 兜底，不依赖人工提前标注。

用法：
    from . import duty
    pushable, alerts = duty.pushable_members("2026-09-19")
"""

from __future__ import annotations

import re
from datetime import datetime

from . import config


def norm_date(raw) -> str:
    """归一化日期为 YYYY-MM-DD（容忍 2026/9/19、2026.9.19、带时间、毫秒时间戳）。

    无法识别时返回去除首尾空白后的原串，交由调用方按不等处理。
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return str(raw)
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw) / 1000).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return str(raw)
    s = str(raw).strip()
    if not s:
        return ""
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return s


def holiday_info(date_str: str) -> dict:
    """当日节假日信息：{is_holiday, is_makeup, weekend, name}。

    调休上班日（makeups）标记为 is_holiday=True & is_makeup=True
    （与页面 holidayInfo 行为一致，便于日历上按「假期」高亮）。
    """
    res = {"is_holiday": False, "is_makeup": False, "weekend": False, "name": ""}
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(date_str or ""))
    if not m:
        return res
    for h in config.HOLIDAYS:
        if date_str in (h.get("makeups") or []):
            res.update(is_holiday=True, is_makeup=True, name=h.get("name", ""))
            return res
        if h.get("start") and h.get("end") and h["start"] <= date_str <= h["end"]:
            res.update(is_holiday=True, name=h.get("name", ""))
            return res
    dow = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).weekday()
    res["weekend"] = dow >= 5
    return res


def is_rest_day(date_str: str) -> bool:
    """是否休息日（法定节假日，或周末且非法定调休上班日）。"""
    hi = holiday_info(date_str)
    if hi["is_makeup"]:
        return False
    return bool(hi["is_holiday"] or hi["weekend"])


def _legacy(status: str) -> str:
    """历史状态名归一（与页面 SCHED_LEGACY 一致）。"""
    return config.LEGACY_STATUS_MAP.get(status, status)


def _fetch_rows(iso_date: str) -> list[dict]:
    """读排班表当日记录 → [{member, status, remark, record_id}]。"""
    from .feishu_api import base_search

    target = norm_date(iso_date)
    recs = base_search(config.SCHED_TABLE,
                       conditions=[{"field_name": "日期", "operator": "is",
                                    "value": [target]}],
                       field_names=["日期", "成员", "状态", "备注"])
    rows: list[dict] = []
    for r in recs:
        f = r.get("fields") or {}
        if norm_date(f.get("日期")) != target:
            continue                      # 服务端过滤偶有宽松匹配，二次校验
        member = str(f.get("成员") or "").strip()
        if not member:
            continue
        rows.append({"record_id": r.get("record_id", ""),
                     "member": member,
                     "status": str(f.get("状态") or "").strip(),
                     "remark": str(f.get("备注") or "").strip(),
                     "order": r.get("record_id", "")})
    return rows


def pushable_members(iso_date: str, members: list[str] | None = None,
                     rows: list[dict] | None = None) -> tuple[set[str], list[str]]:
    """返回 (当日可推送提醒的成员集合, 告警列表)。

    rows 可显式注入（离线单测用）；省略时从排班表实时读取。
    members 省略时用 config.MEMBERS。
    """
    names = list(members if members is not None else config.MEMBERS)
    target = norm_date(iso_date)
    alerts: list[str] = []

    hi = holiday_info(target)
    rest_day = is_rest_day(target)

    if rows is None:
        rows = _fetch_rows(target)

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(str(r.get("member") or "").strip(), []).append(r)

    pushable: set[str] = set()
    unrecorded: list[str] = []
    for m in names:
        recs = grouped.get(m) or []
        if not recs:
            unrecorded.append(m)
            if not rest_day:
                pushable.add(m)           # 工作日无记录 = 默认正常上班
            continue
        if len(recs) > 1:
            alerts.append(f"{m} 当日存在 {len(recs)} 条重复排班记录（已取最后一条）")
        status = _legacy(str(recs[-1].get("status") or "").strip())
        if status in config.PUSH_DUTY_STATUSES:
            pushable.add(m)
            if hi["is_holiday"] and not hi["is_makeup"] and status == "正常上班":
                alerts.append(f"{m} 在法定假日（{hi['name']}）标注为「正常上班」，"
                              f"建议改为「节假日加班」")
        elif status in config.ATTENDANCE_ONLY_STATUSES:
            pass                          # 团建外出：计入出勤、不推送
        # 其余（休息/法定休息/各类假期）→ 不推送

    if unrecorded and rest_day:
        kind = f"法定节假日（{hi['name']}）" if hi["is_holiday"] else "周末"
        alerts.append(f"{target} 为{kind}且未登记排班（已按不推送处理）："
                      + "、".join(unrecorded)
                      + "；若当日需上班/加班，请提前录入「加班 / 节假日加班」")

    extra = sorted(set(grouped.keys()) - set(names))
    if extra:
        alerts.append("排班表出现名单外成员：" + "、".join(extra))

    return pushable, alerts
