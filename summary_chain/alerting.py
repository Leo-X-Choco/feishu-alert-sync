# -*- coding: utf-8 -*-
"""云端提醒链路 —— 异常监控与报警（2026-09-19 新增）。

背景：提醒一旦「该跑没跑」比「推错人」更隐蔽——群里安安静静，谁也不会发现。
本模块把三类异常都变成**主动告警**：

  ① 执行失败 —— 脚本抛异常 / 依赖安装失败 / job 超时。
     由各 workflow 的 `if: failure()` 步骤调用 `--job-failure`。
  ② 该跑没跑 —— 定时未触发、被并发策略取消、启动失败。
     由 `--watchdog` 每天扫 GitHub Actions 运行记录，缺少当日成功记录即告警；
     为区分「GitHub 定时延迟」与「真没跑」，发现异常后会先等一段时间复查一次。
  ③ 告警通道失效 —— 群 Webhook 挂了会导致「连报警都发不出」。
     故告警走双通道：群自定义机器人 Webhook 优先，失败降级应用机器人消息，
     再失败则把告警原文打到 Actions 日志（至少可在运行记录里看到）。

用法：
    python -m summary_chain.alerting --watchdog [--date 2026-09-19]
    python -m summary_chain.alerting --job-failure "Daily Summary Check (15:30 CST)" "summary-check"

退出码：0=一切正常（或告警已成功发出）  2=发现异常（已告警）  1=模块自身出错
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from . import config, feishu_api

CST = timezone(timedelta(hours=8))

# 群名（告警文案用）
GROUP_NAME = "海外客服三组"

# 每日必须成功执行的「提醒类」workflow（file → 可读名称）
EXPECTED_WORKFLOWS = [
    ("praise-reminder.yml", "10:00 好评提醒"),
    ("summary-check.yml", "15:30 小结提醒（首轮）"),
    ("summary-final-sync.yml", "17:30 小结提醒（末轮）"),
]

# 发现异常后的复查等待（秒），用于排除 GitHub 定时任务的常见延迟
RECHECK_DELAY = 600


# ---------------------------------------------------------------------------
# 告警发送（双通道 + 日志兜底）
# ---------------------------------------------------------------------------
def send_alert(text: str, *, strict: bool = False) -> bool:
    """发送群告警。Webhook 优先 → 应用机器人降级 → 打印到日志。

    strict=True 时全部通道失败会抛异常（供 watchdog 自身失败时上抛给 Actions）。
    """
    ok = False
    if config.ALERT_WEBHOOK:
        try:
            payload = {"msg_type": "text", "content": {"text": text}}
            req = urllib.request.Request(
                config.ALERT_WEBHOOK,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            ok = True
            print("[alert] 已通过群 Webhook 发送告警")
        except Exception as e:  # noqa: BLE001
            print(f"[alert] 群 Webhook 告警失败：{e}")
    else:
        print("[alert] 未配置 FEISHU_ALERT_WEBHOOK，跳过 Webhook 通道")

    if not ok:
        # 降级：应用机器人富文本（需机器人入群；失败也不阻塞）
        try:
            feishu_api.send_post_message(config.CHAT_ID, "提醒链路告警", None, [text])
            ok = True
            print("[alert] 已通过应用机器人发送告警（降级通道）")
        except Exception as e:  # noqa: BLE001
            print(f"[alert] 应用机器人告警失败：{e}")

    if not ok:
        # 最后兜底：打进 Actions 日志（运行记录里可见）
        print("=" * 72)
        print("[告警未送达 · 原文] " + text)
        print("=" * 72)
        if strict:
            raise RuntimeError("告警通道全部失败")
    return ok


# ---------------------------------------------------------------------------
# GitHub Actions 运行记录
# ---------------------------------------------------------------------------
def _gh_get(url: str, token: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "wb-notify-watchdog",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def workflow_runs(workflow_file: str, since_iso: str, *, repo: str,
                  token: str) -> list[dict]:
    """返回自 since_iso（UTC ISO8601）以来该 workflow 的运行列表（精简字段）。"""
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow_file}/runs?" + urlencode({"created": f">={since_iso}",
                                                 "per_page": "50"}))
    data = _gh_get(url, token)
    out = []
    for r in data.get("workflow_runs") or []:
        out.append({"id": r.get("id"),
                    "status": r.get("status"),
                    "conclusion": r.get("conclusion"),
                    "created_at": r.get("created_at"),
                    "html_url": r.get("html_url")})
    return out


def _verdict(runs: list[dict]) -> tuple[str, str]:
    """判定单个 workflow 当日状态 → (状态, 说明)。

    状态：ok=已有成功记录 / running=仍在跑（视为待观察）/ bad=失败或未触发
    """
    if not runs:
        return "bad", "当日无任何运行记录（疑似未触发）"
    if any(r.get("conclusion") == "success" for r in runs):
        return "ok", "已有成功记录"
    if any(r.get("status") != "completed" for r in runs):
        return "running", "仍在执行中"
    concluded = "、".join(sorted({str(r.get("conclusion")) for r in runs}))
    return "bad", f"运行均未成功（{concluded}）"


def _day_start_utc(iso_date: str) -> str:
    """CST 当天 00:00 对应的 UTC ISO8601（GitHub API 用 UTC 过滤）。"""
    d = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=CST)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_status(iso_date: str, *, repo: str, token: str) -> list[dict]:
    """采集当日各提醒 workflow 的执行状态。"""
    since = _day_start_utc(iso_date)
    rows = []
    for wf, label in EXPECTED_WORKFLOWS:
        try:
            runs = workflow_runs(wf, since, repo=repo, token=token)
            state, why = _verdict(runs)
            rows.append({"workflow": wf, "label": label, "state": state,
                         "why": why, "runs": runs})
        except Exception as e:  # noqa: BLE001
            rows.append({"workflow": wf, "label": label, "state": "unknown",
                         "why": f"查询失败：{e}", "runs": []})
    return rows


# ---------------------------------------------------------------------------
# 看门狗
# ---------------------------------------------------------------------------
def watchdog(iso_date: str, *, repo: str, token: str,
             recheck_delay: int = RECHECK_DELAY) -> int:
    """检查当日提醒是否都成功执行；异常则告警。返回 0=正常 2=异常。"""
    if not repo or not token:
        print("[watchdog] 缺少 GITHUB_REPOSITORY / GITHUB_TOKEN，无法检查运行记录")
        send_alert(f"🚨【提醒看门狗无法运行】{iso_date}\n"
                   f"缺少 GitHub 读取凭据（GITHUB_REPOSITORY / GITHUB_TOKEN 未注入），"
                   f"无法核对当日提醒是否执行，请检查 workflow 配置。")
        return 1

    print(f"[watchdog] 检查 {iso_date} 的提醒执行情况（repo={repo}）")
    rows = collect_status(iso_date, repo=repo, token=token)
    for r in rows:
        print(f"   - {r['label']} [{r['workflow']}] → {r['state']}（{r['why']}）")

    # 除「已成功」外的所有状态都算异常：未触发 / 失败 / 仍在跑 / 查询失败
    problematic = [r for r in rows if r["state"] != "ok"]

    if problematic and recheck_delay > 0:
        print(f"[watchdog] 发现 {len(problematic)} 项异常，"
              f"{recheck_delay}s 后复查（排除 GitHub 定时延迟）")
        time.sleep(recheck_delay)
        rows = collect_status(iso_date, repo=repo, token=token)
        for r in rows:
            print(f"   [复查] {r['label']} → {r['state']}（{r['why']}）")
        problematic = [r for r in rows if r["state"] != "ok"]

    if not problematic:
        print("[watchdog] 全部提醒已成功执行，无需告警")
        return 0

    state_label = {"bad": "未执行 / 执行失败",
                   "running": "仍在执行中（超时未完成）",
                   "unknown": "状态无法确认（查询失败）"}
    lines = [f"🚨【每日提醒异常告警】{iso_date}", ""]
    for r in problematic:
        lines.append(f"· {r['label']}：{state_label.get(r['state'], r['state'])} —— {r['why']}")
    lines.append("")
    lines.append(f"请核对 GitHub Actions 记录：https://github.com/{repo}/actions")
    lines.append("（若为 GitHub 定时延迟可忽略；连续多日异常请检查定时配置与权限。）")
    text = "\n".join(lines)
    print("[watchdog] " + text.replace("\n", " | "))
    send_alert(text)
    return 2


# ---------------------------------------------------------------------------
# workflow 失败告警入口
# ---------------------------------------------------------------------------
def job_failure(workflow_name: str, job_name: str, *, repo: str = "") -> int:
    """workflow 步骤失败时调用（`if: failure()`）。"""
    lines = [f"🚨【云端提醒任务执行失败】",
             f"任务：{workflow_name}",
             f"步骤：{job_name}"]
    if repo:
        lines.append(f"请查看运行日志：https://github.com/{repo}/actions")
    text = "\n".join(lines)
    print("[job-failure] " + text.replace("\n", " | "))
    send_alert(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="云端提醒链路异常监控与报警")
    ap.add_argument("--watchdog", action="store_true", help="检查当日提醒执行情况并告警")
    ap.add_argument("--job-failure", nargs=2, metavar=("WORKFLOW", "JOB"),
                    help="workflow 失败告警（workflow 名 + 步骤名）")
    ap.add_argument("--date", default=config.iso_today_cst(), help="目标日期 YYYY-MM-DD")
    ap.add_argument("--recheck-delay", type=int, default=RECHECK_DELAY,
                    help="异常时的复查等待秒数（0=不复查）")
    args = ap.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")

    try:
        if args.job_failure:
            return job_failure(args.job_failure[0], args.job_failure[1], repo=repo)
        if args.watchdog:
            return watchdog(args.date, repo=repo, token=token,
                            recheck_delay=args.recheck_delay)
        ap.print_help()
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[错误] 监控模块自身异常：{e}", file=sys.stderr)
        try:
            send_alert(f"🚨【提醒监控模块自身异常】日期 {args.date}\n{e}")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
