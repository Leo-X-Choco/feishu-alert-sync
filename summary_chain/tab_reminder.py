# -*- coding: utf-8 -*-
"""任务提醒引擎（通用版）—— 工作安排·各任务按「提醒时间」到点提醒。

🔴 2026-10-16 改造：原「好评提醒」（praise_reminder.py，每日 10:00 一跑、只认
   提醒时间=10:00、文案硬编码好评）按需求下线，本引擎替代：
     · 由 Cloudflare Worker `patrol-trigger` 每 5 分钟 dispatch 一次
       （仓库为公开仓库，Actions 分钟免费；worker 侧另有 4 分钟去重）
     · 窗口语义：本次运行覆盖「上一次运行时刻 → 现在」内到点的提醒时间
       （分钟粒度；窗口链天然去重——重复 dispatch 不会重复提醒；
       上次运行距今超 30 分钟则按 30 分钟封顶，过旧的提醒不补发）
     · 命中的任务 → 当天在岗的负责人逐人发送，文案通用（不含业务专称）
     · 在岗判定：复用 duty.pushable_members()（与原引擎一致）
     · 无 sleep / verify 步骤（原「20 分钟未读补发」随改造暂停；
       如需已读跟踪须 im 通道 + message_id 落库，另行设计）
退出码：0=成功/静默  2=存在发送失败(已群告警)  1=出错
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from . import alerting, config, duty, feishu_api
from .feishu_api import base_search

CST = timezone(timedelta(hours=8))
WINDOW_CAP_MIN = 30          # 窗口封顶：上次运行距今超过该值，只提醒最近 30 分钟内到点的
REPO = os.environ.get("GITHUB_REPO", "Leo-X-Choco/feishu-alert-sync")
SELF_WF = "tab-reminder.yml"

_HHMM_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")


def parse_hhmm(raw):
    """把提醒时间字段归一化为 (时, 分)；空/非法返回 None（不提醒）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _HHMM_RE.match(s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return (h, mi)
    return None


def _gh_get(path: str, token: str):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "tab-reminder",
                 **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def window_start(now_cst: datetime, self_run_id: str) -> datetime:
    """窗口起点 = 上一次运行时刻（CST，排除自身）；取不到则按 30 分钟封顶。

    🔴 为什么用「上一次运行」而不是固定 5 分钟槽：
       dispatch 偶发失败/延迟时，下一次运行会自动把缺口补上（自愈），
       且窗口链互不重叠 ⇒ 重复 dispatch 也绝不会重复提醒。
    """
    start = now_cst - timedelta(minutes=WINDOW_CAP_MIN)
    try:
        data = _gh_get("/repos/%s/actions/workflows/%s/runs?per_page=8" % (REPO, SELF_WF),
                       os.environ.get("GITHUB_TOKEN", ""))
        for r in data.get("workflow_runs", []):
            if self_run_id and str(r.get("id")) == str(self_run_id):
                continue
            created = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).astimezone(CST)
            if created > start:
                start = created
            break   # 列表按创建时间倒序，第一条非自身即最近一次
    except Exception as e:  # noqa: BLE001
        print("[窗口] 查询上次运行失败（按 %d 分钟封顶处理）：%s" % (WINDOW_CAP_MIN, e))
    return start


def load_due_tasks(win_start: datetime, now_cst: datetime) -> list:
    """读选项卡表，返回提醒时间落在 (win_start, now_cst] 的任务。"""
    tabs = base_search(config.TAB_TABLE, field_names=["名称", "负责人", "提醒时间"])
    s_min = win_start.hour * 60 + win_start.minute
    e_min = now_cst.hour * 60 + now_cst.minute
    same_day = win_start.date() == now_cst.date()
    due = []
    for t in tabs:
        f = t.get("fields") or {}
        hm = parse_hhmm(f.get("提醒时间"))
        if not hm:
            continue
        r_min = hm[0] * 60 + hm[1]
        if same_day:
            hit = s_min < r_min <= e_min
        else:   # 跨日（罕见）：窗口 = (起点→午夜] ∪ [0点→现在]
            hit = (r_min > s_min) or (r_min <= e_min)
        if not hit:
            continue
        owners = []
        for name in str(f.get("负责人") or "").replace("，", "、").split("、"):
            name = name.strip()
            if name and name not in owners:
                owners.append(name)
        if owners:
            due.append({"name": str(f.get("名称") or "未命名任务"),
                        "hhmm": "%02d:%02d" % hm, "owners": owners})
    return due


def mode_send(now_cst: datetime, duty_date: str, dry_run: bool = False,
              self_run_id: str = "") -> int:
    win_start = window_start(now_cst, self_run_id)
    print("[窗口] (%s, %s]（北京）" % (win_start.strftime("%H:%M"), now_cst.strftime("%H:%M")))
    due = load_due_tasks(win_start, now_cst)
    if not due:
        print("[结果] 本窗口无到点任务，静默结束")
        return 0
    pushable, alerts = duty.pushable_members(duty_date, members=config.MEMBERS)
    for a in alerts:
        print("   [排班告警] " + a)
    hard = [a for a in alerts if ("重复排班记录" in a or "名单外成员" in a)]
    if hard:
        alerting.send_alert("🚨【排班数据异常】%s\n%s" % (duty_date, "\n".join("· " + a for a in hard)))

    sends, failures = [], []
    for task in due:
        targets = [m for m in task["owners"] if m in pushable]
        skipped = [m for m in task["owners"] if m not in pushable]
        print("[任务] %s @ %s → 提醒 %s（休班跳过 %s）" % (
            task["name"], task["hhmm"], targets or "无", skipped or "无"))
        for m in targets:
            if dry_run:
                print("   [dry-run] 本应提醒 %s（不实际发送）" % m)
                continue
            open_id = config.OPEN_IDS.get(m, "")
            if not open_id:
                failures.append("%s（%s）：缺少 open_id 映射" % (m, task["name"]))
                continue
            try:
                channel = feishu_api.send_group_message(
                    "工作安排提醒", open_id,
                    ["⏰ 任务提醒（%s）：请及时处理今日「%s」相关工作。" % (task["hhmm"], task["name"])])
                print("   ✅ %s -> %s" % (m, channel))
                sends.append((task["name"], m, channel))
            except Exception as e:  # noqa: BLE001
                failures.append("%s（%s）：%s" % (m, task["name"], e))
                print("   ❌ %s: %s" % (m, e))
    if dry_run:
        print("[dry-run] 未实际发送")
        return 0
    if failures:
        config.send_alert("🚨【任务提醒发送失败】\n" + "\n".join("· " + x for x in failures)
                          + "\n请人工跟进。")
        return 2
    print("[结果] 已发送 %d 条提醒" % len(sends))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=config.iso_today_cst(),
                    help="在岗判定用的日期（默认今天，北京时间）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本应提醒谁，不实际发送（排障/演练用）")
    args = ap.parse_args()
    now_cst = datetime.now(CST)
    try:
        return mode_send(now_cst, duty_date=args.date, dry_run=args.dry_run,
                         self_run_id=os.environ.get("GITHUB_RUN_ID", ""))
    except Exception as e:  # noqa: BLE001
        print("[错误] %s" % e, file=sys.stderr)
        config.send_alert("🚨【任务提醒脚本异常】%s\n%s" % (now_cst.strftime("%Y-%m-%d %H:%M"), e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
