# -*- coding: utf-8 -*-
"""每日小结检查与提醒（云端版，纯提醒）。

  --mode check   （15:30）在岗成员有未填小结 → 群内 @ 提醒一轮；全员已填 → 静默
  --mode final   （17:30）最后一轮 @ 提醒未提交者；全员已填 → 静默

2026-09-08 起：日报同步永久下线（日报表封存为历史快照），两个模式均只检查+提醒，
不写任何表。数据源：飞书多维表格（小结/排班，只读）。
在岗判定（2026-09-19 起）：统一走 duty.pushable_members()——白名单
         {正常上班/加班/节假日加班} 才提醒；「团建外出」计入出勤但不提醒；
         工作日无记录＝默认正常上班，周末/法定节假日无记录＝不在岗；
         法定节假日由 config.HOLIDAYS 兜底。
         🔴 旧实现用 (休息/请假/节假日) 黑名单，漏判「事假」等 v39 新状态 →
            2026-09-19 15:38 事假成员被误 @（已修）。
         结构性跳过：「姓名」下拉中无选项的成员（如许磊）。
退出码：0=全员已填(静默)或提醒全部发送成功  2=提醒发送失败(已群告警)  1=脚本异常
"""

import argparse
import sys

from . import alerting, config, duty, feishu_api
from .feishu_api import base_search


def fetch_summaries(iso_date: str) -> list[dict]:
    return base_search(config.SUMMARY_TABLE,
                       conditions=[{"field_name": "日期", "operator": "is",
                                    "value": [iso_date]}],
                       field_names=["成员", "日期", "小结", "同步状态"])


def off_duty_members(iso_date: str) -> set[str]:
    """【已弃用】当日「明确不在岗」成员集合（= 名单 − 可推送）。

    保留仅为兼容旧调用；判定请改用 duty.pushable_members()，
    否则会重现 2026-09-19 的漏判（旧黑名单不含「事假」等 v39 新状态）。
    """
    pushable, _alerts = duty.pushable_members(iso_date, members=config.MEMBERS)
    return {m for m in config.MEMBERS if m not in pushable}


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


def run(mode: str, iso_date: str, dry_run: bool = False) -> int:
    """纯提醒版（2026-09-08 起）：日报同步已永久下线，两个模式都只做检查+提醒。

    dry_run=True 时只打印本应提醒谁，不实际发送（供手动演练 / 排障，零打扰）。
    退出码：0=全员已填(静默)或提醒全部发送成功  2=提醒发送失败(已群告警)  1=脚本异常
    """
    summaries = fetch_summaries(iso_date)
    pushable, alerts = duty.pushable_members(iso_date, members=config.MEMBERS)
    for a in alerts:
        print(f"   [排班告警] {a}")
    # 真·数据异常（重复行 / 名单外成员）主动告警：这类静默会掩盖问题
    hard = [a for a in alerts if ("重复排班记录" in a or "名单外成员" in a)]
    if hard:
        alerting.send_alert(f"🚨【排班数据异常】{iso_date}\n"
                            + "\n".join("· " + a for a in hard))
    filled = {(s["fields"].get("成员") or "").strip() for s in summaries
              if (s["fields"].get("小结") or "").strip()}
    on_duty = [m for m in config.MEMBERS if m in pushable
               and m not in config.STRUCTURAL_SKIP]
    missing = [m for m in on_duty if m not in filled]
    skipped = sorted(set(config.MEMBERS) - pushable)
    print(f"[状态] 可推送={on_duty} 已填={sorted(filled & set(on_duty))} "
          f"未填={missing} 不推送跳过={skipped}")

    if not missing:
        print("[结果] 在岗全员已填齐，静默结束（日报同步已于 2026-09-08 下线，不再写入）")
        return 0

    if mode == "check":
        text = (f"今日（{iso_date}）工作小结尚未提交，请尽快到[客服培训小组工作台]"
                f"(https://workbuddy.link/p/RaP6fdiaAOnouYexyMBHcK)填写，今日 17:20 前完成，谢谢！")
    else:
        text = (f"今日（{iso_date}）工作小结尚未提交，请尽快到[客服培训小组工作台]"
                f"(https://workbuddy.link/p/RaP6fdiaAOnouYexyMBHcK)填写，今天下班前完成，谢谢！（最后一轮提醒）")
    if dry_run:
        print(f"[dry-run] 本轮本应提醒 {len(missing)} 人：{missing}")
        for m in missing:
            print(f"   [dry-run] @{m}（{config.OPEN_IDS.get(m, '缺 open_id 映射')}）")
        return 0
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
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本应提醒谁，不实际发送（排障/演练用）")
    args = ap.parse_args()
    try:
        return run(args.mode, args.date, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}", file=sys.stderr)
        config.send_alert(f"🚨【小结提醒脚本异常】模式 {args.mode} 日期 {args.date}\n{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
