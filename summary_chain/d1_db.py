# -*- coding: utf-8 -*-
"""D1 数据通道（云端最小实现）
=========================================================================
与 `D:/13445/Documents/outputs/wb_db.py` 同契约、同形状。云端（GitHub Actions）
跑的是独立仓库，无法 import 本地模块，故此处保留一份最小实现。

契约（与页面/形状兼容层一致）：
    POST {host}/api/create  {"base","table","records":[{扁平字段}]}
    POST {host}/api/update  {"base","table","record_id","fields":{...}}
    POST {host}/api/delete  {"base","table","record_ids":[...]}
    POST {host}/api/search  {"base","table","searchBody":{"page_size","page_token"}}
鉴权：请求头 X-Proxy-Token（值从环境变量 WB_PROXY_TOKEN 读，**不要写进代码库**）。

🔴 必须带浏览器 UA：python-urllib 默认 UA 会被 Cloudflare 以 `error code: 1010`
   拒掉（403）。实测这不是 TLS 指纹问题，补一个 UA 头即可（2026-09-20）。

未配置 WB_PROXY_TOKEN 时 `enabled()` 返回 False：调用方应回退旧飞书通道，
这样「代码先合、密钥后加」不会造成任何行为变化。
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = "Qd4ubeDLVazKBrspvMXcSY1Xn5c"
HOSTS = {
    "d1": "https://kf-workbench-d1.1344533650.workers.dev",
    "feishu": "https://kf-workbench-proxy.app.workbuddy.host",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 25

TOKEN = (os.environ.get("WB_PROXY_TOKEN") or os.environ.get("PROXY_TOKEN") or "").strip()
_TARGETS = [t.strip() for t in (os.environ.get("DB_TARGETS") or "d1").split(",") if t.strip()]
TARGETS = [t for t in _TARGETS if t in HOSTS] or ["feishu"]


def enabled() -> bool:
    """是否具备走 D1 的条件（有 token 且首选目标不是飞书）。"""
    return bool(TOKEN) and TARGETS[0] != "feishu"


def _post(host_key, path, body, attempts=3):
    url = HOSTS[host_key] + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json",
                "X-Proxy-Token": TOKEN,
                "User-Agent": UA,          # 🔴 缺这行会被 CF 以 1010 拒
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            last = RuntimeError(f"[{host_key}] HTTP {e.code} {e.reason} {detail}")
        except Exception as e:  # noqa: BLE001
            last = RuntimeError(f"[{host_key}] {type(e).__name__}: {e}")
        if i < attempts - 1:
            time.sleep(0.8 * (i + 1))
    raise last if last else RuntimeError(f"[{host_key}] unknown error")


def _call(path, body, what):
    ok = False
    for i, host_key in enumerate(TARGETS):
        try:
            res = _post(host_key, path, body)
            if res.get("code") not in (0, "0", None):
                raise RuntimeError(f"code={res.get('code')} msg={res.get('msg')}")
            if i == 0:
                ok = True
        except Exception as e:  # noqa: BLE001
            if i == 0:
                print(f"[d1_db] {what} 主后端[{host_key}] 失败：{e}")
                return False
            print(f"[d1_db] {what} 镜像[{host_key}] 失败（不影响主流程）：{e}")
    return ok


def search(table, page_size=1000, base=BASE):
    """读全表（主目标通道），返回 [{record_id, fields}]。"""
    host_key = TARGETS[0]
    out, token = [], ""
    while True:
        res = _post(host_key, "/api/search", {"base": base, "table": table,
                                              "searchBody": {"page_size": page_size,
                                                             "page_token": token}})
        if res.get("code") not in (0, "0", None):
            raise RuntimeError(f"search({table}) code={res.get('code')} msg={res.get('msg')}")
        data = res.get("data") or {}
        out.extend(data.get("items") or [])
        if not data.get("has_more"):
            return out
        token = str(data.get("page_token") or "")
        if not token:
            return out


def update(table, record_id, fields, base=BASE):
    if not record_id or not fields:
        return True
    return _call("/api/update", {"base": base, "table": table,
                                 "record_id": record_id, "fields": fields},
                 f"update({table}#{record_id})")


# ---------------------------------------------------------------------------
# feishu_api.base_search 的形状兼容读（云端的单点切换入口）
# ---------------------------------------------------------------------------
def _eq(actual, want) -> bool:
    """等值比较：容忍 None、字符串两端空白、数字与字符串互比。"""
    if actual is None:
        return False
    a = actual.strip() if isinstance(actual, str) else actual
    w = want.strip() if isinstance(want, str) else want
    return a == w or str(a) == str(w)


def search_records(table_id, conditions=None, field_names=None, base=BASE):
    """兼容读：全量拉取 + 本地等值过滤 + 字段投影，返回 [{record_id, fields}]。

    形状与 `feishu_api.base_search` 一致，可直接顶替它。

    🔴 为什么本地过滤、不让服务端过滤：
      1) D1 侧被迁的表都小（59~296 行），全量拉取代价可忽略；
      2) 平台资料库侧单次查询会被服务端截到 200 行（历史上曾把当日「休息」记录
         截断 → 误判在岗），本地过滤不存在这个上限；
      3) 现有调用方只用到 operator="is"（等值），本地模拟无歧义；一旦出现
         别的算子就返回 None 交回旧通道，避免语义偏差。
    D1 返回的已是扁平标量（日期即 "YYYY-MM-DD" 字符串），无需再拍平。
    任一环节异常都返回 None，由调用方回退飞书通道。
    """
    if base != BASE:
        return None                     # 跨 Base（如日报多维表格）不走 D1
    try:
        recs = search(table_id, base=base)
    except Exception as e:  # noqa: BLE001
        print(f"[d1_db] search_records({table_id}) 失败，交回飞书通道：{e}")
        return None

    for c in conditions or []:
        if c.get("operator") not in (None, "is"):
            print(f"[d1_db] 遇到未支持算子 {c.get('operator')!r}，交回飞书通道")
            return None

    out = []
    for r in recs:
        f = dict(r.get("fields") or {})
        hit = True
        for c in conditions or []:
            vals = c.get("value") or []
            if not any(_eq(f.get(c.get("field_name")), v) for v in vals):
                hit = False
                break
        if not hit:
            continue
        if field_names:
            f = {k: f.get(k) for k in field_names}
        out.append({"record_id": r.get("record_id"), "fields": f})
    return out


def create(table, records, base=BASE):
    if not records:
        return True
    return _call("/api/create", {"base": base, "table": table, "records": records},
                 f"create({table})")
