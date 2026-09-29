# -*- coding: utf-8 -*-
"""d1_quota_alert.py —— D1 行读额度巡检（**云端版**，跑在 GitHub Actions）

为什么要有云端版（2026-09-25 实测的盲区）：
    本机版（工作区 outputs/d1_quota_alert.py）依赖本机常驻巡检。而本机夜间会关机
    （09-24 19:01 → 09-25 09:01 零巡检记录），若超限发生在关机时段，实时告警**永远不会发**：
    09-24 那次 109.8% 就是这么漏掉的。⇒ 把巡检挪到云端，才能做到零盲区。

与本地版的差异：
    · 状态存 `QUOTA_STATE_DIR/state.json`（workflow 用 actions/cache 跨运行持久化，
      因为 GitHub Actions 每次运行都是新容器）；
    · 只做「当日判定 + 同级别/升级」这一件事（云端每小时都会跑，不需要「开机补报」）；
    · 文案用纯文本（走 FEISHU_ALERT_WEBHOOK，msg_type=text），不依赖交互卡片。

环境变量（全部来自 GitHub Secrets/Variables）：
    CF_ACCOUNT_ID          必需  Cloudflare 账号 ID
    CF_API_TOKEN           必需  **只需 Zone/Account Analytics 读**权限的 token（最小权限）
    FEISHU_ALERT_WEBHOOK   必需  告警群 Webhook
    QUOTA_STATE_DIR        可选  状态目录，默认 .quota_state
    QUOTA_DRY_RUN          可选  1 = 只打印不发送

用法：python d1_quota_alert.py [--dry-run] [--force]   （退出码 0 表示流程健康）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

DAILY_LIMIT = 5_000_000
D1_DATABASE_ID = "8f9488d4-867f-406c-8841-0af84cee311e"
GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
WARN_RATIO, CRIT_RATIO = 0.80, 1.00
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")
_RANK = {None: 0, "warn": 1, "crit": 2}

QUERY = """
query($acc: String!, $start: Date!, $end: Date!, $db: String!) {
  viewer {
    accounts(filter: {accountTag: $acc}) {
      d1AnalyticsAdaptiveGroups(
        limit: 1000
        filter: {date_geq: $start, date_leq: $end, databaseId: $db}
        orderBy: [datetimeHour_ASC]
      ) { dimensions { datetimeHour } sum { rowsRead } count }
    }
  }
}
"""


def _clean_proxy() -> None:
    for k in PROXY_KEYS:
        os.environ.pop(k, None)


def fetch_usage(acc: str, token: str) -> dict:
    """当日累计 + 最近一个完整小时的速率（GraphQL 元数据查询，不消耗 D1 行读额度）。"""
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    body = json.dumps({"query": QUERY, "variables": {
        "acc": acc, "start": (now - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        "end": today, "db": D1_DATABASE_ID}}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    if j.get("errors"):
        raise RuntimeError("GraphQL errors: " + json.dumps(j["errors"], ensure_ascii=False)[:400])
    groups = (j.get("data", {}).get("viewer", {}).get("accounts") or [{}])[0].get(
        "d1AnalyticsAdaptiveGroups") or []
    hours = sorted((str((g.get("dimensions") or {}).get("datetimeHour") or ""),
                    int((g.get("sum") or {}).get("rowsRead") or 0)) for g in groups)
    hours = [(h, r) for h, r in hours if h]
    cur = now.strftime("%Y-%m-%dT%H:00:00Z")
    day_rows = sum(r for h, r in hours if h[:10] == today)
    done = [(h, r) for h, r in hours if h != cur]
    rate, rate_hour = (done[-1][1], done[-1][0]) if done else ((hours[-1][1], hours[-1][0]) if hours else (0, ""))
    return {"day": today, "day_rows": day_rows, "rate": rate, "rate_hour": rate_hour, "limit": DAILY_LIMIT}


def decide(day_rows: int, limit: int = DAILY_LIMIT):
    if limit <= 0:
        return None
    r = day_rows / limit
    return "crit" if r >= CRIT_RATIO else ("warn" if r >= WARN_RATIO else None)


def build_text(info: dict, level: str) -> str:
    rows, limit = info["day_rows"], info["limit"]
    ratio = rows / limit * 100
    eta = ""
    if info.get("rate") and rows < limit:
        eta = "\n最近一个完整小时 %s 行 ⇒ 按此速率约 %.1f 小时后触顶。" % (
            format(info["rate"], ","), (limit - rows) / info["rate"])
    elif rows >= limit:
        eta = "\n已超出 %s 行，D1 现已拒绝全部数据查询。" % format(rows - limit, ",")
    if level == "crit":
        return ("🔴【事故】D1 行读额度已超限（客服小组工作台）\n\n"
                "UTC 日 %s 已用 %s / %s 行 = %.1f%%%s\n"
                "工作台全部数据读写不可用（顶栏显示「离线模式」）。\n"
                "恢复：北京时间次日 08:00（UTC 午夜自动重置，免费版无法手动重置）。\n"
                "在此之前无需反复刷新。\n"
                "数据来源：Cloudflare GraphQL Analytics（云端巡检，与本机是否开机无关）") % (
            info["day"], format(rows, ","), format(limit, ","), ratio, eta)
    return ("🟠 D1 行读额度预警（客服小组工作台）\n\n"
            "UTC 日 %s 已用 %s / %s 行 = %.1f%%%s\n"
            "触顶后 D1 会拒绝一切查询，工作台全部数据读写不可用。\n"
            "建议现在关掉多余的工作台标签页（每多开一个满速标签页约 +4.5 万行/小时）。\n"
            "数据来源：Cloudflare GraphQL Analytics（云端巡检）") % (
        info["day"], format(rows, ","), format(limit, ","), ratio, eta)


def _state_path() -> str:
    d = os.environ.get("QUOTA_STATE_DIR") or ".quota_state"
    return os.path.join(d, "state.json")


def load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    p = _state_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=2)


def send(text: str) -> bool:
    hook = os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
    if not hook:
        print("! 未配置 FEISHU_ALERT_WEBHOOK，跳过发送")
        return False
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = urllib.request.Request(hook, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8") or "{}")
    ok = d.get("code", d.get("StatusCode", 0)) == 0
    if not ok:
        print("! Webhook 返回异常：", json.dumps(d, ensure_ascii=False)[:200])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="D1 行读额度巡检（云端版）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    dry = a.dry_run or os.environ.get("QUOTA_DRY_RUN") == "1"

    _clean_proxy()
    acc = (os.environ.get("CF_ACCOUNT_ID") or "").strip()
    token = (os.environ.get("CF_API_TOKEN") or "").strip()
    if not acc or not token:
        print("! 缺少 CF_ACCOUNT_ID / CF_API_TOKEN —— 无法巡检（不视为失败）")
        return 0

    info = fetch_usage(acc, token)
    level = decide(info["day_rows"], info["limit"])
    print("UTC 日 %s 已用 %s / %s 行 = %.1f%% ｜ 速率 %s 行/时（%s）" % (
        info["day"], format(info["day_rows"], ","), format(info["limit"], ","),
        info["day_rows"] / info["limit"] * 100, format(info["rate"], ","), info["rate_hour"] or "?"))
    if level is None:
        print("→ 未跨阈值（<80%%），不告警")
        return 0

    st = load_state()
    prev = st.get("level") if st.get("day") == info["day"] else None
    if not a.force and _RANK[level] <= _RANK.get(prev, 0):
        print("→ 今日已按 %s 级告警过（幂等跳过）" % prev)
        return 0

    text = build_text(info, level)
    if dry:
        print("--- 告警内容（dry-run，未发送）---")
        print(text)
        return 0
    ok = send(text)
    print("→ %s 级告警发送%s" % (level, "成功" if ok else "失败"))
    if ok:
        st.update({"day": info["day"], "level": level, "rows": info["day_rows"],
                   "sent_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")})
        save_state(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
