# -*- coding: utf-8 -*-
"""新人考核超期提醒（云端版，每日 09:00 北京时间）。

规则：
  判定通过：验收结论 = '通过' 或 进组结果 = '通过'
  培训周期：从「培训周期」文本解析天数（如 "7天（如情况不佳会延长）" → 7），
           未填或缺数字时默认 7 天
  截止日  ：入职日期 + 周期天数 - 1（第 N 天为最后一天）
  超期    ：状态 = '在职' 且 未通过 且 今天 > 截止日（离职/模板人员跳过）

输出：超期者合并为一条群卡片消息（webhook，逐人 @，注明入职日期/周期/截止日/
超期天数/当前验收结论）；无超期静默结束。
退出码：0=无超期或提醒发送成功  2=提醒发送失败(已群告警)  1=脚本异常
"""

import argparse
import re
import sys
from datetime import datetime, timezone, timedelta

from . import config, feishu_api
from .feishu_api import base_search

CST = timezone(timedelta(hours=8))
DEFAULT_DAYS = 7


def _iso_today(override: str | None) -> str:
    return override or config.iso_today_cst()


def _parse_employ_date(value) -> str:
    """入职日期归一化为 YYYY-MM-DD（兼容毫秒时间戳与字符串）。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=CST).strftime("%Y-%m-%d")
    return str(value)[:10]


def _period_days(text: str) -> int:
    m = re.search(r"(\d+)\s*天", text or "")
    return int(m.group(1)) if m else DEFAULT_DAYS


def fetch_exam_records() -> list[dict]:
    return base_search(config.EXAM_TABLE,
                       conditions=[],
                       field_names=["姓名", "状态", "入职日期", "培训周期",
                                    "验收结论", "进组结果"])


def _text(value) -> str:
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return (value[0].get("text") or "").strip()
        return (value[0] if value else "").strip() if value else ""
    return (str(value) if value is not None else "").strip()


def _passed(f: dict) -> bool:
    return _text(f.get("验收结论")) == "通过" or _text(f.get("进组结果")) == "通过"


def find_overdue(records: list[dict], today_iso: str) -> list[dict]:
    """返回超期人员列表（含展示用字段）。"""
    today = datetime.strptime(today_iso, "%Y-%m-%d").date()
    out = []
    for r in records:
        f = r["fields"]
        name = _text(f.get("姓名"))
        status = _text(f.get("状态")) or "在职"
        if not name or status != "在职" or _passed(f):
            continue
        hire_iso = _parse_employ_date(f.get("入职日期"))
        if not hire_iso:
            continue
        hire = datetime.strptime(hire_iso, "%Y-%m-%d").date()
        days = _period_days(_text(f.get("培训周期")))
        deadline = hire + timedelta(days=days - 1)
        if today > deadline:
            out.append({
                "name": name,
                "hire": hire_iso,
                "days": days,
                "deadline": deadline.strftime("%Y-%m-%d"),
                "overdue_days": (today - deadline).days,
                "conclusion": _text(f.get("验收结论")) or "待验收",
            })
    out.sort(key=lambda x: x["overdue_days"], reverse=True)
    return out


def run(today_iso: str) -> int:
    records = fetch_exam_records()
    overdue = find_overdue(records, today_iso)
    print(f"[状态] 考核记录 {len(records)} 条，超期 {len(overdue)} 人")
    if not overdue:
        print("[结果] 无超期人员，静默结束")
        return 0
    lines = [f"以下 {len(overdue)} 名在职新人已超过培训周期仍未通过考核："]
    for p in overdue:
        ou = config.OPEN_IDS.get(p["name"])
        at = f"<at id={ou}></at> " if ou else ""
        lines.append(
            f"{at}**{p['name']}**：入职 {p['hire']}，培训周期 {p['days']} 天"
            f"（截止 {p['deadline']}），已超期 {p['overdue_days']} 天，"
            f"当前验收结论：{p['conclusion']}")
    lines.append("请带教人/组长尽快跟进验收，或在 [客服培训小组工作台]"
                 "(https://workbuddy.link/p/RaP6fdiaAOnouYexyMBHcK)"
                 "更新「培训周期/验收结论」。")
    try:
        feishu_api.send_group_card("新人考核超期提醒", lines)
    except Exception as e:  # noqa: BLE001
        print(f"   ❌ 发送失败: {e}")
        config.send_alert(
            f"🚨【超期提醒发送失败】\n超期人员：{'、'.join(p['name'] for p in overdue)}\n错误：{e}")
        return 2
    print(f"[结果] 已发送超期提醒（{len(overdue)} 人）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default="", help="覆盖今天日期 YYYY-MM-DD（测试用）")
    args = ap.parse_args()
    try:
        return run(_iso_today(args.today))
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}", file=sys.stderr)
        config.send_alert(f"🚨【超期提醒脚本异常】\n{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
