# -*- coding: utf-8 -*-
"""每日小结检查与提醒（云端版，纯提醒）。

  --mode check   （15:30）在岗成员有未填小结 → 群内 @ 提醒一轮；全员已填 → 静默
  --mode final   （17:30）最后一轮 @ 提醒未提交者；全员已填 → 静默

2026-09-08 起：日报同步永久下线（日报表封存为历史快照），两个模式均只检查+提醒，
不写任何表。数据源：飞书多维表格（小结/排班，只读）。
在岗判定：排班表当日状态 ∈ {休息,请假,节假日} → 不在岗（不提醒）；
         无记录或其他状态 → 在岗。结构性跳过：「姓名」下拉中无选项的成员（如许磊）。
退出码：0=全员已填(静默)或提醒全部发送成功  2=提醒发送失败(已群告警)  1=脚本异常
"""

import argparse
import sys

from . import config, feishu_api
from .feishu_api import base_search


def fetch_summaries(iso_date: str) -> list[dict]:
    return base_search(config.SUMMARY_TABLE,
                       conditions=[{"field_name": "日期", "operator": "is",
                                    "value": [iso_date]}],
                       field_names=["成员", "日期", "小结", "同步状态"])


def off_duty_members(iso_date: str) -> set[str]:
    recs = base_search(config.SCHED_TABLE,
                       conditions=[{"field_name": "日期", "operator": "is",
                                    "value": [iso_date]}],
                       field_names=["成员", "状态", "日期"])
    off = set()
    for r in recs:
        f = r["fields"]
        member = (f.get("成员") or "").strip()
        if member and (f.get("状态") or "").strip() in config.OFF_DUTY_STATUSES:
            off.add(member)
    return off


def remind(missing: list[str], text: str) -> list[str]:
    """逐人 @ 提醒（群自定义机器人 Webhook），返回发送失败名单。"""
    failed = []
    for m in missing:
        try:
            feishu_api.send_group_message("每日小结提醒",
                                          config.OPEN_IDS.get(m), [text])
            print(f"   ✉️ 已提醒 {m}")
        except Exception as e:  # noqa: BLE001
            failed.append(m)
            print(f"   ❌ 提醒 {m} 失败: {e}")
    return failed


def run(mode: str, iso_date: str) -> int:
    """纯提醒版（2026-09-08 起）：日报同步已永久下线，两个模式都只做检查+提醒。

    退出码：0=全员已填(静默)或提醒全部发送成功  2=提醒发送失败(已群告警)  1=脚本异常
    """
    summaries = fetch_summaries(iso_date)
    off = off_duty_members(iso_date)
    filled = {(s["fields"].get("成员") or "").strip() for s in summaries
              if (s["fields"].get("小结") or "").strip()}
    on_duty = [m for m in config.MEMBERS if m not in off
               and m not in config.STRUCTURAL_SKIP]
    missing = [m for m in on_duty if m not in filled]
    print(f"[状态] 在岗={on_duty} 已填={sorted(filled & set(on_duty))} "
          f"未填={missing} 不在岗跳过={sorted(off)}")

    if not missing:
        print("[结果] 在岗全员已填齐，静默结束（日报同步已于 2026-09-08 下线，不再写入）")
        return 0

    if mode == "check":
        text = (f"今日（{iso_date}）工作小结尚未提交，请尽快到[客服培训小组工作台]"
                f"(https://workbuddy.link/p/RaP6fdiaAOnouYexyMBHcK)填写，今日 17:20 前完成，谢谢！")
    else:
        text = (f"今日（{iso_date}）工作小结尚未提交，请尽快到[客服培训小组工作台]"
                f"(https://workbuddy.link/p/RaP6fdiaAOnouYexyMBHcK)填写，今天下班前完成，谢谢！（最后一轮提醒）")
    failed = remind(missing, text)
    if failed:
        config.send_alert(
            f"🚨【每日小结提醒发送失败】\n日期：{iso_date}\n成员：{'、'.join(failed)}\n请人工 @ 跟进。")
        return 2
    print(f"[结果] 已提醒 {len(missing)} 人（mode={mode}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["check", "final"], required=True)
    ap.add_argument("--date", default=config.iso_today_cst())
    args = ap.parse_args()
    try:
        return run(args.mode, args.date)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}", file=sys.stderr)
        config.send_alert(f"🚨【小结提醒脚本异常】模式 {args.mode} 日期 {args.date}\n{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
