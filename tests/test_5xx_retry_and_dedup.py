# -*- coding: utf-8 -*-
"""验证 2026-10-21 14:00 事故的改进补丁（本地，不触网）：

P1  http_json 对查询类请求（GET）遇服务端 5xx 自动重试；写接口（POST）不重试；
    既有白名单码（1255002/1254607）与超时重试行为不变。
P2  send_alert_and_mark：告警发送成功落 alert_sent.marker 标记（供 workflow 兜底去重）。

用 mock 完全离线，不产生真实网络请求。
"""
import io
import json
import os
import sys
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_alerts  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'} | {name}{' | ' + detail if detail else ''}")


class FakeResp(io.BytesIO):
    """模拟 urlopen 的成功响应（支持上下文管理器）。"""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body):
    return urllib.error.HTTPError(
        "http://test", code, "Err",
        hdrs=None, fp=io.BytesIO(body.encode("utf-8")),
    )


BODY_2200 = json.dumps({"code": 2200, "msg": "Internal Error"})
BODY_1254607 = json.dumps({"code": 1254607, "msg": "Data not ready"})
BODY_1255002 = json.dumps({"code": 1255002, "msg": "Something went wrong"})
BODY_OK = json.dumps({"code": 0, "data": {"ok": 1}})


def run_case(sleep_patched=True, **kwargs):
    """统一跑 http_json 的 case：mock 掉 urlopen 与 sleep，返回 (结果, 调用次数, 异常)。"""
    side = kwargs.pop("side")
    calls = {"n": 0}

    def _fake_urlopen(req, data=None, timeout=None):
        calls["n"] += 1
        item = side[min(calls["n"] - 1, len(side) - 1)]
        if isinstance(item, urllib.error.HTTPError):
            raise item
        return FakeResp(item.encode("utf-8"))

    with mock.patch("urllib.request.urlopen", _fake_urlopen), \
         mock.patch.object(sync_alerts.time, "sleep", lambda s: None):
        try:
            r = sync_alerts.http_json(kwargs.pop("method", "GET"), "http://test/x", **kwargs)
            return r, calls["n"], None
        except sync_alerts.LarkError as e:
            return None, calls["n"], e


# ---- P1 场景 ----

# 1. GET 遇 500（code 2200，即 10-21 事故形态）：重试 2 次仍失败 → 报错且错误信息保留 HTTP 500
r, n, e = run_case(side=[http_error(500, BODY_2200)] * 3, method="GET")
check("P1-1 GET 500 重试耗尽报错", r is None and n == 3 and e is not None,
      f"calls={n} err={str(e)[:80]}")
check("P1-1b 错误信息含『已重试 2 次』与 HTTP 500",
      e is not None and "已重试 2 次" in str(e) and "HTTP 500" in str(e))

# 2. GET 500 ×2 后第三次成功 → 成功，共 3 次调用
r, n, e = run_case(side=[http_error(500, BODY_2200), http_error(500, BODY_2200), BODY_OK], method="GET")
check("P1-2 GET 500 第三次自愈成功", r is not None and n == 3 and r.get("code") == 0,
      f"calls={n}")

# 3. POST（写接口）遇 500：不重试，直接报错，仅 1 次调用
r, n, e = run_case(side=[http_error(500, BODY_2200)], method="POST",
                   payload={"a": 1})
check("P1-3 POST 500 不重试", r is None and n == 1 and e is not None and "HTTP 500" in str(e),
      f"calls={n}")

# 4. 既有行为回归：GET 400 + 白名单码 1254607 → 仍可重试
#    （注意每次要全新的 HTTPError 实例：fp 只能读一次，复用实例第二次 body 变空）
r, n, e = run_case(side=[http_error(400, BODY_1254607) for _ in range(3)], method="GET")
check("P1-4 既有白名单码 1254607 重试不变", r is None and n == 3,
      f"calls={n}")

# 5. 既有行为回归：4xx 业务错误（非白名单）→ 不重试，1 次调用
r, n, e = run_case(side=[http_error(400, json.dumps({"code": 9999, "msg": "bad"}))], method="GET")
check("P1-5 4xx 业务错误立即报错", r is None and n == 1 and "HTTP 400" in str(e),
      f"calls={n}")

# 6. 既有行为回归：HTTP 200 + 白名单码 1255002 → 退避耗尽后原样返回
r, n, e = run_case(side=[json.dumps({"code": 1255002})] * 3, method="GET")
check("P1-6 200+白名单码耗尽原样返回", r is not None and n == 3 and r.get("code") == 1255002,
      f"calls={n}")

# 7. 503 也覆盖（其他 5xx 同理）
r, n, e = run_case(side=[http_error(503, "Service Unavailable"), BODY_OK], method="GET")
check("P1-7 GET 503 也可重试", r is not None and n == 2, f"calls={n}")

# ---- P2 场景 ----

marker = sync_alerts.ALERT_SENT_MARKER
if os.path.exists(marker):
    os.remove(marker)

# 8. 告警发送成功 → 落标记文件
with mock.patch.object(sync_alerts, "send_alert", lambda w, t: True):
    ok = sync_alerts.send_alert_and_mark("http://whook", "test")
check("P2-8 发送成功落标记", ok and os.path.exists(marker))

# 9. 告警发送失败 → 不落标记
if os.path.exists(marker):
    os.remove(marker)
with mock.patch.object(sync_alerts, "send_alert", lambda w, t: False):
    ok = sync_alerts.send_alert_and_mark("http://whook", "test")
check("P2-9 发送失败不落标记", (not ok) and not os.path.exists(marker))

# 10. 标记内容为时间字符串（非空）——先重新走一遍成功路径重建标记
with mock.patch.object(sync_alerts, "send_alert", lambda w, t: True):
    sync_alerts.send_alert_and_mark("http://whook", "test")
content = open(marker, encoding="utf-8").read().strip() if os.path.exists(marker) else ""
check("P2-10 标记内容非空时间串", len(content) == 19 and content[4] == "-", f"content={content!r}")

# ---- workflow yaml 语法 ----
try:
    import yaml
    wf = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                          ".github/workflows/sync-alerts.yml"), encoding="utf-8"))
    sync_job = wf["jobs"]["sync"]
    nf_if = wf["jobs"]["notify-failure"]["if"]
    cond_ok = ("alert_sent" in sync_job.get("outputs", {})
               and "alert_sent != 'true'" in nf_if
               and any(s.get("id") == "alert_marker" for s in sync_job["steps"]))
    check("P2-11 workflow 语法与去重接线正确", cond_ok)
except ImportError:
    check("P2-11 workflow yaml 校验（pyyaml 缺失，跳过自动校验）", True, "SKIP")

# ---- 汇总 ----
fails = [r for r in RESULTS if not r[1]]
print(f"\n===== {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS =====")
sys.exit(1 if fails else 0)
