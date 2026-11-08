# -*- coding: utf-8 -*-
"""probe_dm.py —— 手册探针告警/恢复 → 「许磊的智能助手」单聊
用法：python probe_dm.py <alert|recover|test> [streak] [dur_min] [detail]
open_id 说明：接收者 ID（非机密），硬编码避免依赖仓库 secrets（WB_DM_OPEN_ID 未配置）。
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

APP_ID = "cli_aaad0f934aa21bed"
# 🔑 这两个从仓库 secrets 读（d1-quota-alert 同款，已验证可用）
import os

APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
OPEN_ID = os.environ.get("WB_DM_OPEN_ID", "") or "ou_716ef6bab4627dd72d16a05debf42ca9"

import urllib.request


def http_json(url, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    rq = urllib.request.Request(url, data=json.dumps(body or {}).encode(), headers=headers)
    return json.load(urllib.request.urlopen(rq, timeout=20))


def main():
    alert_type = sys.argv[1] if len(sys.argv) > 1 else "test"
    streak = sys.argv[2] if len(sys.argv) > 2 else "0"
    dur = sys.argv[3] if len(sys.argv) > 3 else "0"
    detail = sys.argv[4] if len(sys.argv) > 4 else ""

    tk = http_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                   {"app_id": APP_ID, "app_secret": APP_SECRET})
    token = tk.get("tenant_access_token")
    if not token:
        print("no tenant_access_token:", json.dumps(tk)[:200])
        sys.exit(1)

    if alert_type == "alert":
        text = ("🚨【手册探针】手册网站持续打不开：已连续 " + streak + " 次探活失败（约 " + dur + " 分钟）。"
                + (("失败形态：" + detail + "。") if detail else "")
                + "话术数据不受影响（独立存储）。如果影响同事使用请等几分钟再试，恢复后会再发一条通知。")
    elif alert_type == "recover":
        text = ("✅【手册探针】已恢复：此前连续失败 " + streak + " 次（约 " + dur + " 分钟），"
                "手册网站现在能正常打开了。")
    else:
        text = "🧪【手册探针】通道测试：这是一条测试消息。看到它说明「许磊的智能助手」单聊告警通道正常（2026-11-08 整改上线）。"

    resp = http_json("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                     {"receive_id": OPEN_ID, "msg_type": "text",
                      "content": json.dumps({"text": text}, ensure_ascii=False)}, token=token)
    code = resp.get("code")
    print("send result code=", code, "msg=", resp.get("msg"))
    if code != 0:
        print("send failed:", json.dumps(resp, ensure_ascii=False)[:300])
        sys.exit(1)
    print("DM sent OK")


if __name__ == "__main__":
    main()
