# -*- coding: utf-8 -*-
"""d1_quota_alert.py —— Cloudflare 免费额度巡检·双指标（**云端版**，跑在 GitHub Actions）

2026-10-22 扩展：补上 Workers 请求指标（免费 100,000/天）。10-21 事故复盘发现 10-15 的
    双指标扩展只落在本机版，云端版仍单指标 ⇒ 80% 越线发生在北京 23 点（本机已关机）时
    云端兜底看不见，Workers 103.1% 触顶、Error 1027 全停约 2 小时才由次日补报发现。

为什么要有云端版（2026-09-25 实测的盲区）：
    本机版（工作区 outputs/d1_quota_alert.py）依赖本机常驻巡检。而本机夜间会关机
    （09-24 19:01 → 09-25 09:01 零巡检记录），若超限发生在关机时段，实时告警**永远不会发**：
    09-24 那次 109.8% 就是这么漏掉的。⇒ 把巡检挪到云端，才能做到零盲区。

与本地版的差异：
    · 状态存 `QUOTA_STATE_DIR/state.json`（workflow 用 actions/cache 跨运行持久化，
      因为 GitHub Actions 每次运行都是新容器）；
    · 只做「当日判定 + 同级别/升级」这一件事（云端每小时都会跑，不需要「开机补报」）；
    · 文案用纯文本（走飞书单聊 API，msg_type=text），不依赖交互卡片。
    · 🔴 2026-10-26 起**只发「许磊的智能助手」单聊**，不再发「海外客服三组」群
      （原 `FEISHU_ALERT_WEBHOOK` 群通道降级为可选兜底，默认关闭）。

环境变量（全部来自 GitHub Secrets/Variables）：
    CF_ACCOUNT_ID          必需  Cloudflare 账号 ID
    CF_API_TOKEN           必需  **只需 Zone/Account Analytics 读**权限的 token（最小权限）
    FEISHU_APP_ID          必需  「许磊的智能助手」自建应用 App ID
    FEISHU_APP_SECRET      必需  同上 App Secret
    WB_DM_OPEN_ID          可选  收件人 open_id，默认许磊（ou_716ef6…）
    FEISHU_ALERT_WEBHOOK   可选  群 webhook（仅在 FEISHU_GROUP_FALLBACK=1 时才用）
    FEISHU_GROUP_FALLBACK  可选  1 = 个人通道失败时回落群；默认 0（不回落）
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
WORKERS_LIMIT = 100_000
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

QUERY_W = """
query($acc: String!, $start: Time!, $end: Time!) {
  viewer {
    accounts(filter: {accountTag: $acc}) {
      workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $start, datetime_leq: $end}) {
        dimensions { datetimeHour scriptName }
        sum { requests }
      }
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


def fetch_workers_usage(acc: str, token: str) -> dict:
    """当日（UTC）Workers 请求累计 + 最近一个完整小时的速率 + 按脚本拆分（元数据查询，不耗额度）。"""
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    body = json.dumps({"query": QUERY_W, "variables": {
        "acc": acc,
        "start": today + "T00:00:00Z",
        "end": now.strftime("%Y-%m-%dT%H:%M:%SZ")}}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    if j.get("errors"):
        raise RuntimeError("GraphQL(W) errors: " + json.dumps(j["errors"], ensure_ascii=False)[:400])
    groups = (j.get("data", {}).get("viewer", {}).get("accounts") or [{}])[0].get(
        "workersInvocationsAdaptive") or []
    by_script: dict = {}
    by_hour: dict = {}
    for g in groups:
        dim = g.get("dimensions") or {}
        h = str(dim.get("datetimeHour") or "")
        sc = str(dim.get("scriptName") or "?")
        n = int((g.get("sum") or {}).get("requests") or 0)
        if not h:
            continue
        by_script[sc] = by_script.get(sc, 0) + n
        by_hour[h] = by_hour.get(h, 0) + n
    day_reqs = sum(n for h, n in by_hour.items() if h[:10] == today)
    done = sorted((h, n) for h, n in by_hour.items() if h != now.strftime("%Y-%m-%dT%H:00:00Z"))
    rate, rate_hour = (done[-1][1], done[-1][0]) if done else (0, "")
    top = sorted(by_script.items(), key=lambda kv: -kv[1])[:3]
    return {"day": today, "day_reqs": day_reqs, "rate": rate, "rate_hour": rate_hour,
            "limit": WORKERS_LIMIT, "top": top}


def build_text_workers(info: dict, level: str) -> str:
    reqs, limit = info["day_reqs"], info["limit"]
    ratio = reqs / limit * 100
    eta = ""
    if info.get("rate") and reqs < limit:
        eta = "\n最近一个完整小时 %s 次 ⇒ 按此速率约 %.1f 小时后触顶。" % (
            format(info["rate"], ","), (limit - reqs) / info["rate"])
    elif reqs >= limit:
        eta = "\n已超出 %s 次，所有 Worker 现已停摆。" % format(reqs - limit, ",")
    tops = "、".join("%s=%s" % (k, format(v, ",")) for k, v in info.get("top") or [])
    head = ("🔴【事故】Workers 请求额度已超限" if level == "crit"
            else "🟠 Workers 请求额度预警（客服工作台数据口）")
    if level == "crit":
        tail = ("\n**影响：所有 Worker 停摆（Error 1027）**——数据口 d1.ciacobd0223.dpdns.org 全挂，"
                "工作台读写不可用（离线模式）、图片不可看不可传、巡检触发器停摆。"
                "\n**恢复：北京时间次日 08:00**（UTC 午夜自动重置，免费版无法手动重置）。")
    else:
        tail = ("\n触顶后所有 Worker 停摆（Error 1027）：工作台数据读写、图片、巡检触发全部不可用。"
                "\n建议：现在关闭多余的工作台/手册标签页（每个打开的页面都会持续产生请求）。")
    return ("%s\n\nUTC 日 %s 已用 %s / %s 请求 ＝ %.1f%%%s\n%s"
            "\n（按脚本：%s）\n数据来源：Cloudflare GraphQL Analytics（云端每小时巡检，与本机是否开机无关）") % (
        head, info["day"], format(reqs, ","), format(limit, ","), ratio, eta, tail, tops or "?")


def run_workers_check(acc: str, token: str, dry: bool, force: bool, st: dict) -> tuple[int, dict]:
    """【Workers 请求】单指标检查（与 D1 检查同构；状态键 w_day/w_level）。返回 (退出码, 新状态)。"""
    try:
        info = fetch_workers_usage(acc, token)
    except Exception as e:  # noqa: BLE001
        print("! Workers 取数失败（不影响 D1 检查）：%s" % str(e)[:200])
        return 0, st
    level = decide(info["day_reqs"], info["limit"])
    print("Workers ｜ UTC 日 %s 已用 %s / %s 次 = %.1f%% ｜ 速率 %s 次/时（%s）｜ Top：%s" % (
        info["day"], format(info["day_reqs"], ","), format(info["limit"], ","),
        info["day_reqs"] / info["limit"] * 100, format(info["rate"], ","),
        info["rate_hour"] or "?", "、".join("%s=%s" % (k, format(v, ",")) for k, v in info["top"]) or "?"))
    if level is None:
        print("→ Workers 未跨阈值（<80%），不告警")
        return 0, st
    prev = st.get("w_level") if st.get("w_day") == info["day"] else None
    if not force and _RANK[level] <= _RANK.get(prev, 0):
        print("→ Workers 今日已按 %s 级告警过（幂等跳过）" % prev)
        return 0, st
    text = build_text_workers(info, level)
    if dry:
        print("--- Workers 告警内容（dry-run，未发送）---")
        print(text)
        return 0, st
    ok = send(text)
    print("→ Workers %s 级告警发送%s" % (level, "成功" if ok else "失败"))
    if ok:
        st.update({"w_day": info["day"], "w_level": level, "reqs": info["day_reqs"],
                   "w_sent_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")})
    return 0, st


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


FEISHU_HOST = "https://open.feishu.cn"
# 许磊本人 open_id（2026-10-02 取自 lark-cli auth status 实测值）
# 🔴 必须用 `or` 兜底：GitHub Secrets 里若没配 WB_DM_OPEN_ID，env 会存在但为**空串**
#    ⇒ 直接 os.environ.get(k, default) 拿到的是 ""，消息会发给空收件人而失败。
DM_OPEN_ID = (os.environ.get("WB_DM_OPEN_ID") or "").strip() \
    or "ou_716ef6bab4627dd72d16a05debf42ca9"


def _feishu_token(app_id: str, app_secret: str) -> str:
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        FEISHU_HOST + "/open-apis/auth/v3/tenant_access_token/internal",
        data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8") or "{}")
    if d.get("code") != 0:
        raise RuntimeError("取 tenant_access_token 失败: "
                           + json.dumps(d, ensure_ascii=False)[:220])
    return d["tenant_access_token"]


def send_dm(text: str) -> tuple[bool, str]:
    """给「许磊的智能助手」单聊发一条纯文本。返回 (是否成功, 说明)。"""
    app_id = (os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = (os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        return False, "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET"
    try:
        tok = _feishu_token(app_id, app_secret)
        body = json.dumps({
            "receive_id": DM_OPEN_ID,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            FEISHU_HOST + "/open-apis/im/v1/messages?receive_id_type=open_id",
            data=body, method="POST",
            headers={"Authorization": "Bearer " + tok,
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8") or "{}")
        if d.get("code") != 0:
            return False, "单聊返回异常: " + json.dumps(d, ensure_ascii=False)[:220]
        mid = ((d.get("data") or {}).get("message_id")) or "sent"
        return True, str(mid)
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:200])


def send(text: str) -> bool:
    """发送告警：**只发「许磊的智能助手」单聊**（2026-10-26 老大要求）。

    实现：企业自建应用 `FEISHU_APP_ID/SECRET` 取 tenant_access_token
          → `im/v1/messages` 以 open_id 发给许磊本人。
    飞书里这条消息就出现在「许磊的智能助手」这个机器人的对话窗口里。
    🔴 「海外客服三组」群已**不再发送**（含 webhook 兜底）；
       要临时恢复，设 `FEISHU_GROUP_FALLBACK=1`。
    """
    ok, note = send_dm(text)
    print("  → 个人单聊（许磊的智能助手）：%s" % ("成功 " + note if ok else "失败 " + note))
    if ok:
        return True

    if os.environ.get("FEISHU_GROUP_FALLBACK", "0") not in ("1", "true", "yes"):
        print("  ! 群兜底已关闭（默认）⇒ 本条只留在 Actions 日志里，不发任何群")
        return False

    hook = os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
    if not hook:
        print("! 未配置 FEISHU_ALERT_WEBHOOK，跳过发送")
        return False
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = urllib.request.Request(hook, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8") or "{}")
    ok2 = d.get("code", d.get("StatusCode", 0)) == 0
    if not ok2:
        print("! Webhook 返回异常：", json.dumps(d, ensure_ascii=False)[:200])
    return ok2


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

    st = load_state()
    code_w, st = run_workers_check(acc, token, dry, a.force, st)
    save_state(st)

    info = fetch_usage(acc, token)
    level = decide(info["day_rows"], info["limit"])
    print("UTC 日 %s 已用 %s / %s 行 = %.1f%% ｜ 速率 %s 行/时（%s）" % (
        info["day"], format(info["day_rows"], ","), format(info["limit"], ","),
        info["day_rows"] / info["limit"] * 100, format(info["rate"], ","), info["rate_hour"] or "?"))
    if level is None:
        print("→ D1 未跨阈值（<80%%），不告警")
        return code_w


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
