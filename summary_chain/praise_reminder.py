# -*- coding: utf-8 -*-
"""每日 10:00 好评提醒（云端版）。

流程：
  send 模式：读选项卡表(提醒时间=10:00) → 排班表在岗过滤 → 逐人 @ 发送
             （发送结果落 state 文件供 verify 使用；通道=群自定义机器人 Webhook）
  verify 模式：读 state 校验。Webhook 通道无 message_id，无已读回执 →
             跳过已读校验与未读补发（与本地生产版一致）；仅报告历史发送失败项。
             含 message_id 的历史 state（im 通道）仍走已读校验+补发。

在岗判定（2026-09-19 起）：统一走 duty.pushable_members()——推送白名单
         {正常上班/加班/节假日加班}；「团建外出」计入出勤但不提醒；
         工作日无记录＝默认正常上班；周末/法定节假日无记录＝不在岗；
         法定节假日由 config.HOLIDAYS 兜底。全员不可推送 → 静默结束(0)。
         🔴 旧实现用 (休息/请假/节假日) 黑名单，漏判「事假」等 v39 新状态。
退出码：0=成功/静默结束  2=存在发送失败(已群告警)  1=出错
"""

import argparse
import json
import os
import sys

from . import alerting, config, duty, feishu_api
from .feishu_api import base_search

STATE_FILE = os.environ.get("PRAISE_STATE_FILE", "praise_state.json")


def load_targets() -> list[str]:
    """读选项卡表确定提醒对象（提醒时间=10:00 的负责人并集）。"""
    tabs = base_search(config.TAB_TABLE,
                       conditions=[{"field_name": "提醒时间", "operator": "is",
                                    "value": [config.PRAISE_REMIND_TIME]}],
                       field_names=["名称", "负责人", "提醒时间"])
    owners: list[str] = []
    for t in tabs:
        f = t["fields"]
        for name in (f.get("负责人") or "").replace("，", "、").split("、"):
            name = name.strip()
            if name and name not in owners:
                owners.append(name)
    return owners


def off_duty_members(iso_date: str) -> set[str]:
    """【已弃用】当日不可推送成员（= 名单 − 可推送）；保留仅为兼容旧调用。

    判定已统一到 duty.pushable_members()，勿再使用旧黑名单 config.OFF_DUTY_STATUSES。
    """
    pushable, _alerts = duty.pushable_members(iso_date, members=config.MEMBERS)
    return {m for m in config.MEMBERS if m not in pushable}


def mode_send(iso_date: str, dry_run: bool = False) -> int:
    owners = load_targets()
    pushable, alerts = duty.pushable_members(iso_date, members=config.MEMBERS)
    for a in alerts:
        print(f"   [排班告警] {a}")
    hard = [a for a in alerts if ("重复排班记录" in a or "名单外成员" in a)]
    if hard:
        alerting.send_alert(f"🚨【排班数据异常】{iso_date}\n"
                            + "\n".join("· " + a for a in hard))
    # 选项卡表读不到 10:00 目标 → 配置缺失，主动告警（否则该轮静默空转、无人察觉）
    if not owners:
        alerting.send_alert(
            f"🚨【好评提醒无目标】{iso_date}\n"
            f"选项卡表「{config.PRAISE_TAB_NAME}」未配置「提醒时间={config.PRAISE_REMIND_TIME}」"
            f"的负责人，本轮 10:00 好评提醒将空转，请检查选项卡配置。")
    targets = [m for m in owners if m in pushable]
    skipped = [m for m in owners if m not in pushable]
    print(f"[目标] 负责人={owners} 不推送跳过={skipped} 待提醒={targets}")
    if not targets:
        print("[结果] 无可推送目标，静默结束")
        return 0
    if dry_run:
        print(f"[dry-run] 本应提醒 {len(targets)} 人：{targets}（不实际发送）")
        return 0

    state, failures = {"date": iso_date, "channel": "webhook", "sends": []}, []
    for m in targets:
        entry = {"name": m, "open_id": config.OPEN_IDS.get(m, "")}
        if not entry["open_id"]:
            failures.append(m)
            entry["error"] = "缺少 open_id 映射"
        else:
            try:
                channel = feishu_api.send_group_message(
                    "每日好评提醒", entry["open_id"],
                    [f"你好，请及时处理今日「{config.PRAISE_TAB_NAME}」相关工作（好评邮件邀约与发放，记得截图登记）。"])
                entry["channel"] = channel
                print(f"   ✅ {m} -> {channel}")
            except Exception as e:  # noqa: BLE001
                failures.append(m)
                entry["error"] = str(e)
                print(f"   ❌ {m}: {e}")
        state["sends"].append(entry)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    if failures:
        config.send_alert(f"🚨【好评提醒发送失败】\n日期：{iso_date}\n成员：{('、'.join(failures))}\n已自动重试仍失败，请人工跟进。")
        return 2
    return 0


def mode_verify(iso_date: str) -> int:
    if not os.path.exists(STATE_FILE):
        print("[verify] 无 state 文件，跳过")
        return 0
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    unread, failures, webhook_skipped = [], [], []
    for entry in state.get("sends", []):
        mid = entry.get("message_id")
        if not mid:
            # webhook 通道：无已读回执，跳过校验（不误报）
            if not entry.get("error"):
                webhook_skipped.append(entry["name"])
            else:
                failures.append(entry["name"])
            continue
        try:
            read = feishu_api.read_users(mid)
            if entry.get("open_id") in read:
                entry["read"] = True
                print(f"   👍 {entry['name']} 已读")
            else:
                entry["read"] = False
                unread.append(entry["name"])
                print(f"   👀 {entry['name']} 未读，补发")
                try:
                    feishu_api.send_post_message(
                        config.CHAT_ID, "好评提醒（补发）", entry["open_id"],
                        ["上一条提醒可能未读：请及时处理今日好评相关工作，谢谢！"])
                except Exception as e:  # noqa: BLE001
                    failures.append(entry["name"])
                    entry["resent_error"] = str(e)
        except Exception as e:  # noqa: BLE001
            print(f"   [已读校验不可用] {entry['name']}: {e}")
    if webhook_skipped:
        print(f"[verify] webhook 通道无已读回执，跳过校验: {webhook_skipped or '无'}")
    if failures:
        config.send_alert(f"🚨【好评提醒未送达】\n日期：{iso_date}\n成员：{('、'.join(failures))}\n请人工 @ 跟进。")
        return 2
    print(f"[结果] 未读已补发: {unread or '无'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["send", "verify"], required=True)
    ap.add_argument("--date", default=config.iso_today_cst())
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本应提醒谁，不实际发送（排障/演练用）")
    args = ap.parse_args()
    try:
        return (mode_send(args.date, dry_run=args.dry_run) if args.mode == "send"
                else mode_verify(args.date))
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}", file=sys.stderr)
        config.send_alert(f"🚨【好评提醒脚本异常】日期 {args.date}\n{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
