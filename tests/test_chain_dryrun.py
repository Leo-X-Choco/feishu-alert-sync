# -*- coding: utf-8 -*-
"""云端小结提醒链路 dry-run（mock 网络层，零发送）——验证修复后的完整判定链路。

场景 1（真实发送模拟）：用 2026-09-19 真实排班跑 summary_chain.run("check", ...)
    期望：仅向李玉婷（加班）发送 1 条；方佳莹（事假）不再被提醒。
场景 2（--dry-run）：同一日期带 dry_run=True
    期望：发送 0 条，退出码 0（演练不打扰任何人）。

运行：
    cd D:\\13445\\Documents\\feishu-alert-sync
    python tests\\test_chain_dryrun.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from summary_chain import config, summary_chain  # noqa: E402
import summary_chain.duty as duty_mod  # noqa: E402

REPORT = os.path.join(ROOT, "tests", "_test_chain_dryrun_report.txt")
LINES: list[str] = []


def log(s: str = "") -> None:
    LINES.append(str(s))


# 2026-09-19 真实排班（与线上表一致）
SCHED = [
    {"record_id": "recvuv4iKB5H4H", "fields": {"日期": "2026-09-19", "成员": "方佳莹", "状态": "事假", "备注": "病假"}},
    {"record_id": "recvuv4iKC5jAh", "fields": {"日期": "2026-09-19", "成员": "许磊", "状态": "休息", "备注": ""}},
    {"record_id": "recvuv4iKCzhTZ", "fields": {"日期": "2026-09-19", "成员": "李燕芳", "状态": "休息", "备注": ""}},
    {"record_id": "recvuv4iKCw2rf", "fields": {"日期": "2026-09-19", "成员": "胡镭", "状态": "休息", "备注": ""}},
    {"record_id": "recvuv4iKCTjBW", "fields": {"日期": "2026-09-19", "成员": "李玉婷", "状态": "加班", "备注": ""}},
]
SUMMARY: list[dict] = []          # 当天无人提交小结（复现 15:38 现场）

sent: list[tuple] = []


def fake_base_search(table_id, conditions=None, field_names=None, app_token=None):
    if table_id == config.SCHED_TABLE:
        return SCHED
    if table_id == config.SUMMARY_TABLE:
        return SUMMARY
    return []


def fake_fetch_rows(iso):
    return [{"record_id": r["record_id"], "member": r["fields"]["成员"],
             "status": r["fields"]["状态"], "remark": r["fields"].get("备注", ""),
             "order": r["record_id"]}
            for r in SCHED if (r["fields"].get("日期") or "") == iso]


def fake_send_group_message(title, at_open_id, text_lines, retries=3):
    sent.append((title, at_open_id, tuple(text_lines)))
    return "webhook"


def run_case(dry: bool) -> tuple[int, str]:
    """跑一次链路，返回 (退出码, 捕获的输出)。"""
    buf = io.StringIO()
    with mock.patch.object(summary_chain, "base_search", side_effect=fake_base_search), \
         mock.patch.object(summary_chain.feishu_api, "send_group_message",
                           side_effect=fake_send_group_message), \
         mock.patch.object(summary_chain.config, "send_alert",
                           side_effect=lambda t: log("[ALERT] " + str(t))), \
         mock.patch.object(duty_mod, "_fetch_rows", side_effect=fake_fetch_rows), \
         contextlib.redirect_stdout(buf):
        code = summary_chain.run("check", "2026-09-19", dry_run=dry)
    return code, buf.getvalue()


def main() -> int:
    ok = True
    fang = config.OPEN_IDS["方佳莹"]
    li = config.OPEN_IDS["李玉婷"]

    # ---- 场景 1：正常发送 ----
    sent.clear()
    code, out = run_case(dry=False)
    log("=== 场景 1：summary_chain --mode check 2026-09-19（模拟真实发送）===")
    log(out.rstrip())
    log("退出码 = %s，发送条数 = %d" % (code, len(sent)))
    for s in sent:
        log("   -> 标题=%s @=%s" % (s[0], s[1]))
    names = [s[1] for s in sent]
    log("")
    for cond, msg in (
        (fang not in names, "未向方佳莹（事假）发送提醒"),
        (li in names, "向李玉婷（加班）发送提醒"),
        (len(sent) == 1, "仅 1 条提醒（无重复/无漏发）"),
        (code == 0, "退出码 0"),
    ):
        log(("PASS " if cond else "FAIL ") + msg)
        ok = ok and cond

    # ---- 场景 2：dry-run ----
    sent.clear()
    code2, out2 = run_case(dry=True)
    log("")
    log("=== 场景 2：同一日期 + dry_run=True（演练，不发送）===")
    log(out2.rstrip())
    log("退出码 = %s，发送条数 = %d" % (code2, len(sent)))
    log("")
    for cond, msg in (
        (len(sent) == 0, "dry-run 未发送任何消息"),
        (code2 == 0, "dry-run 退出码 0"),
        ("[dry-run] 本轮本应提醒 1 人" in out2, "dry-run 正确预告将提醒 1 人"),
    ):
        log(("PASS " if cond else "FAIL ") + msg)
        ok = ok and cond

    log("")
    log("---- RESULT: %s ----" % ("PASS" if ok else "FAIL"))
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(LINES) + "\n")
    print("\n".join(LINES))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
