# -*- coding: utf-8 -*-
"""飞书开放平台 API 封装：自建应用 tenant_access_token + 多维表格 + 消息。"""

import json
import os
import time
import urllib.request
import urllib.error

from . import config

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

_token_cache = {"token": "", "expire_at": 0.0}


class ApiError(Exception):
    def __init__(self, code, msg, path=""):
        self.code = code
        self.msg = msg
        super().__init__(f"[{path}] code={code} msg={msg}")


def tenant_token() -> str:
    """获取/缓存 tenant_access_token（有效期约 2h，提前 5 分钟刷新）。"""
    if not APP_ID or not APP_SECRET:
        raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量")
    if _token_cache["token"] and time.time() < _token_cache["expire_at"]:
        return _token_cache["token"]
    body = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode("utf-8")
    req = urllib.request.Request(
        config.FEISHU_HOST + "/open-apis/auth/v3/tenant_access_token/internal",
        data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    if d.get("code") != 0:
        raise ApiError(d.get("code"), d.get("msg"), "tenant_access_token")
    _token_cache["token"] = d["tenant_access_token"]
    _token_cache["expire_at"] = time.time() + int(d.get("expire", 7200)) - 300
    return _token_cache["token"]


def api(method: str, path: str, body: dict | None = None,
        params: dict | None = None) -> dict:
    """调用开放平台 API，返回 data 部分；业务码非 0 抛 ApiError。"""
    url = config.FEISHU_HOST + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tenant_token())
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8", "ignore")[:300], path)
    if d.get("code") != 0:
        raise ApiError(d.get("code"), d.get("msg"), path)
    return d.get("data") or {}


# ---------------------------------------------------------------------------
# 多维表格
# ---------------------------------------------------------------------------
def _norm(value):
    """规范化单元格值：text 可能是 string 或 [{text:..}] 段列表。"""
    if isinstance(value, list):
        return "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg)
                       for seg in value)
    return value


def base_search(table_id: str, conditions: list[dict] | None = None,
                field_names: list[str] | None = None) -> list[dict]:
    """检索记录，返回 [{record_id, fields(已规范化)}]。空 conditions = 全表。"""
    out, page_token = [], ""
    while True:
        body = {"page_size": 500}
        if field_names:
            body["field_names"] = field_names
        if conditions:
            body["filter"] = {"conjunction": "and", "conditions": conditions}
        if page_token:
            body["page_token"] = page_token
        d = api("POST", f"/open-apis/bitable/v1/apps/{config.BASE_TOKEN}"
                        f"/tables/{table_id}/records/search", body=body)
        for item in d.get("items") or []:
            fields = {k: _norm(v) for k, v in (item.get("fields") or {}).items()}
            out.append({"record_id": item.get("record_id"), "fields": fields})
        page_token = d.get("page_token") or ""
        if not d.get("has_more") or not page_token:
            return out


def base_create(table_id: str, records: list[dict]) -> int:
    """批量新建记录，fields 为 {字段名: 值}；返回写入条数。"""
    n = 0
    for i in range(0, len(records), 100):
        d = api("POST", f"/open-apis/bitable/v1/apps/{config.BASE_TOKEN}"
                        f"/tables/{table_id}/records/batch_create",
                body={"records": [{"fields": r} for r in records[i:i + 100]]})
        n += len(d.get("records") or [])
    return n


def base_update(table_id: str, record_id: str, fields: dict) -> None:
    api("PUT", f"/open-apis/bitable/v1/apps/{config.BASE_TOKEN}"
               f"/tables/{table_id}/records/{record_id}", body={"fields": fields})


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------
def send_post_message(chat_id: str, title: str, at_open_id: str | None,
                      text_lines: list[str], retries: int = 3) -> str:
    """发富文本消息（可带 @），返回 message_id。"""
    para = []
    if at_open_id:
        para.append({"tag": "at", "user_id": at_open_id})
    for i, line in enumerate(text_lines):
        if i or at_open_id:
            para.append({"tag": "text", "text": (" " if (i or at_open_id) else "") + line})
        else:
            para.append({"tag": "text", "text": line})
    content = {"post": {"zh_cn": {"title": title, "content": [para]}}}
    last_err = None
    for attempt in range(retries):
        try:
            d = api("POST", "/open-apis/im/v1/messages?receive_id_type=chat_id",
                    body={"receive_id": chat_id, "msg_type": "post",
                          "content": json.dumps(content, ensure_ascii=False)})
            return d["message_id"]
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1) * 3)  # 15s/30s/45s
    raise RuntimeError(f"消息发送失败（已重试 {retries} 次）: {last_err}")


def read_users(message_id: str) -> set[str]:
    """查询消息已读的 open_id 集合。"""
    out, page_token = set(), ""
    while True:
        params = {"user_id_type": "open_id", "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        d = api("GET", f"/open-apis/im/v1/messages/{message_id}/read_users",
                params=params)
        for it in d.get("items") or []:
            uid = it.get("user_id") or (it.get("user") or {}).get("user_id")
            if uid:
                out.add(uid)
        page_token = d.get("page_token") or ""
        if not d.get("has_more") or not page_token:
            return out
