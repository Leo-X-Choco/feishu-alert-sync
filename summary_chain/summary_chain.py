# -*- coding: utf-8 -*-
"""每日小结检查与同步（云端版）。

  --mode check   （15:30）在岗成员小结全部填写 → 直接同步；有未填 → @ 提醒一轮即结束
  --mode final   （17:30）最后一轮 @ 提醒未提交者 → 无论是否填齐都同步（空小结跳过）

数据源：飞书多维表格（小结/排班）；写入目标：飞书日报文档 + 小结表「同步状态」回写。
在岗判定：排班表当日状态 ∈ {休息,请假,节假日} → 不在岗（不提醒/不同步/不回写）；
         无记录或其他状态 → 在岗。结构性跳过：许磊（日报表格无对应行）。
退出码：0=完成  1=存在写入失败或其他错误
"""

import argparse
import sys

from . import config, daily_doc, feishu_api
from .feishu_api import base_search

REMIND_1530 = "今日工作小结还没填写，请在 17:20 前完成，当日会自动同步到飞书日报。"
REMIND_1730 = "日报将先行同步，你今日的小结还未填写；请补填后联系组长安排补同步。"


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
    """逐人 @ 提醒，返回发送失败名单。"""
    failed = []
    for m in missing:
        try:
            feishu_api.send_post_message(config.CHAT_ID, "每日小结提醒",
                                         config.OPEN_IDS.get(m), [text])
            print(f"   ✉️ 已提醒 {m}")
        except Exception as e:  # noqa: BLE001
            failed.append(m)
            print(f"   ❌ 提醒 {m} 失败: {e}")
    return failed


def sync_to_doc(iso_date: str, summaries: list[dict]) -> tuple[list[str], list[str]]:
    """将当日小结写入日报文档并回写「同步状态」，返回 (成功, 失败) 名单。"""
    table = daily_doc.find_work_table(config.DAILY_DOC_ID, iso_date)
    if not table:
        raise RuntimeError(f"日报中未找到「{config.iso_to_daily_title(iso_date)}」工作表格")
    row_by_name = {r["name"]: r for r in table["rows"] if r["name"]}
    success, failed = [], []
    for s in summaries:
        member = (s["fields"].get("成员") or "").strip()
        text = s["fields"].get("小结") or ""
        if not member or not text.strip():
            continue
        row = row_by_name.get(member)
        if not row:
            print(f"   [跳过] {member}: 日报表格无对应行")
            continue
        try:
            daily_doc.write_cell_items(config.DAILY_DOC_ID, row["cell_id"],
                                       row["children"], daily_doc.split_items(text))
            success.append(member)
            print(f"   ✅ 写入日报: {member}")
        except Exception as e:  # noqa: BLE001
            failed.append(member)
            print(f"   ❌ 写入日报失败 {member}: {e}")
        # 回写同步状态（失败不中断其他成员）
        if s.get("record_id"):
            try:
                feishu_api.base_update(config.SUMMARY_TABLE, s["record_id"],
                                       {"同步状态": "已同步" if member in success
                                        else "同步失败"})
            except Exception as e:  # noqa: BLE001
                print(f"   [回写失败] {member}: {e}")
    return success, failed


def run(mode: str, iso_date: str) -> int:
    summaries = fetch_summaries(iso_date)
    off = off_duty_members(iso_date)
    filled = {(s["fields"].get("成员") or "").strip() for s in summaries
              if (s["fields"].get("小结") or "").strip()}
    on_duty = [m for m in config.MEMBERS if m not in off
               and m not in config.STRUCTURAL_SKIP]
    missing = [m for m in on_duty if m not in filled]
    print(f"[状态] 在岗={on_duty} 已填={sorted(filled & set(on_duty))} "
          f"未填={missing} 不在岗跳过={sorted(off)}")

    remind_failed = []
    if mode == "check":
        if missing:
            print("[结果] 存在未填成员，仅提醒一轮（不同步）")
            remind_failed = remind(missing, REMIND_1530)
            return 0 if not remind_failed else 0  # 提醒失败仍视为本轮完成
        print("[结果] 在岗全员已填齐，直接同步")
    else:  # final
        if missing:
            remind_failed = remind(missing, REMIND_1730)
        print("[结果] 执行兜底同步（空小结自动跳过）")

    success, failed = sync_to_doc(iso_date, summaries)
    print(f"[同步] 成功={success} 失败={failed}")
    if failed:
        config.send_alert(
            f"🚨【每日小结同步部分失败】\n日期：{iso_date}\n失败成员：{('、'.join(failed))}\n"
            f"已回写「同步失败」状态，工作台看板可见。")
        return 1
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
        config.send_alert(f"🚨【小结同步脚本异常】模式 {args.mode} 日期 {args.date}\n{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
