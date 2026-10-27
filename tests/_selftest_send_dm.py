# -*- coding: utf-8 -*-
"""_selftest_send_dm.py —— 校验云端 d1_quota_alert.send_dm/send 的通道选择逻辑（不联网）

辨别力：把 send() 里「个人通道优先」或「群兜底默认关」任一拆掉，下面必有一条转红。
"""
import io
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import d1_quota_alert as q  # noqa: E402

fails = 0


def chk(cond, msg):
    global fails
    if not cond:
        fails += 1
        print("  FAIL " + msg)
    else:
        print("  PASS " + msg)


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_fake(seq):
    """按顺序返回预置响应；记录每次调用的 URL 与 body。"""
    calls = []

    def urlopen(req, timeout=None):
        calls.append({"url": getattr(req, "full_url", ""), "body": req.data})
        return FakeResp(seq.pop(0))

    q.urllib.request.urlopen = urlopen
    return calls


print("[1] 无凭据 → 明确失败，不走群")
os.environ.pop("FEISHU_APP_ID", None)
os.environ.pop("FEISHU_APP_SECRET", None)
os.environ.pop("FEISHU_ALERT_WEBHOOK", None)
os.environ["FEISHU_GROUP_FALLBACK"] = "0"
ok, note = q.send_dm("x")
chk(ok is False and "未配置" in note, "无凭据 ⇒ False + 说明缺 FEISHU_APP_ID/SECRET（实际: %s）" % note)

print("[2] 有凭据 + 接口成功 → 发到 im/v1/messages，收件人=许磊")
os.environ["FEISHU_APP_ID"] = "cli_test"
os.environ["FEISHU_APP_SECRET"] = "sec_test"
os.environ["WB_DM_OPEN_ID"] = "ou_716ef6bab4627dd72d16a05debf42ca9"
calls = install_fake([
    {"code": 0, "tenant_access_token": "t-fake"},
    {"code": 0, "data": {"message_id": "om_fake_123"}},
])
ok, note = q.send_dm("hello")
chk(ok is True and note == "om_fake_123", "成功 ⇒ True + message_id（实际: %s）" % note)
chk(len(calls) == 2, "两次调用：取 token + 发消息（实际 %d 次）" % len(calls))
chk(calls[0]["url"].endswith("/open-apis/auth/v3/tenant_access_token/internal"),
    "第 1 次打 token 端点（实际: %s）" % calls[0]["url"])
chk("receive_id_type=open_id" in calls[1]["url"] and "/im/v1/messages" in calls[1]["url"],
    "第 2 次打 im/v1/messages + open_id")
sent = json.loads(calls[1]["body"].decode("utf-8"))
chk(sent.get("receive_id") == "ou_716ef6bab4627dd72d16a05debf42ca9", "收件人=许磊 open_id")
chk(sent.get("msg_type") == "text", "msg_type=text")
chk(json.loads(sent["content"]).get("text") == "hello", "正文正确解包")

print("[3] 单聊业务失败 + 群兜底关 → 不发 webhook")
os.environ["FEISHU_ALERT_WEBHOOK"] = "https://example.invalid/hook"
calls = install_fake([
    {"code": 0, "tenant_access_token": "t-fake"},
    {"code": 230001, "msg": "bot not in chat"},
])
ok = q.send("boom")
chk(ok is False, "单聊失败 + 兜底关 ⇒ send() 返回 False")
chk(len(calls) == 2, "只打了 token+单聊两个请求，**没有**打 webhook（实际 %d 次）" % len(calls))
chk(all("example.invalid" not in c["url"] for c in calls), "确认 webhook 完全未被调用")

print("[4] 单聊失败 + 显式开启兜底 → 才发 webhook")
os.environ["FEISHU_GROUP_FALLBACK"] = "1"
calls = install_fake([
    {"code": 0, "tenant_access_token": "t-fake"},
    {"code": 230001, "msg": "bot not in chat"},
    {"code": 0, "StatusCode": 0},
])
ok = q.send("boom")
chk(ok is True, "兜底开启且 webhook 成功 ⇒ True")
chk(len(calls) == 3 and "example.invalid" in calls[2]["url"], "第 3 个请求才是 webhook（实际 %d 次）" % len(calls))

print("\n%s  %d 项失败" % ("✅ 全部通过" if fails == 0 else "❌", fails))
sys.exit(1 if fails else 0)
