# -*- coding: utf-8 -*-
"""
云端同步脚本：飞书群【邮件资产-客服】法务/侵权邮件预警 → 多维表格【法务侵权邮件预警记录】

运行环境：GitHub Actions（或任意 Linux 服务器 + cron）
认证方式：飞书开放平台企业自建应用（tenant_access_token，自动获取，无需人工刷新）

所需环境变量（通过 GitHub Secrets 配置）：
  FEISHU_APP_ID        自建应用 App ID
  FEISHU_APP_SECRET    自建应用 App Secret
  FEISHU_BASE_TOKEN    多维表格 Base token（FjGXbNiKmabLJzs4bt9cQTTJnwY）
  FEISHU_TABLE_ID      表 ID（tbla4IqbNsmQCqvp）
  FEISHU_CHAT_ID       群 chat_id（oc_41b71ccde24705641e1e382fabe7b4d4）
  FEISHU_ALERT_WEBHOOK 可选，飞书群机器人 Webhook（用于失败告警，见 README）
  LOOKBACK_HOURS       可选，扫描最近 N 小时内的消息，默认 24

行为：
  1. 拉取群内最近消息（分页）
  2. 筛选"⚖️ 法务/侵权邮件预警"类消息（内容含"法务/侵权邮件预警"）
  3. 读取表中已有"消息ID"做去重
  4. 批量写入新记录（幂等，重复运行安全）
  5. 任一阶段失败时，若配置了 FEISHU_ALERT_WEBHOOK，自动向飞书群发送告警
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FEISHU_HOST = "https://open.feishu.cn"

# 法务类别 select 字段的合法选项（不在列表内的归入"其他"）
CATEGORY_OPTIONS = {
    "律师函", "版权投诉", "版权侵权", "DMCA通知", "法律催告", "其他", "非法务",
    "版权侵权/DMCA通知", "版权侵权/律师函", "版权投诉/DMCA通知",
    "商标侵权/知识产权投诉", "版权登记/版权行政通知", "版权相关",
    "DMCA通知/版权侵权",
}


class LarkError(Exception):
    pass


class SyncFailed(Exception):
    """同步失败，携带失败阶段。"""

    def __init__(self, stage, cause):
        self.stage = stage
        self.cause = cause
        super().__init__(f"阶段[{stage}]: {cause}")


def now_str():
    """当前时间字符串（Asia/Shanghai）。"""
    try:
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M:%S")


def send_alert(webhook, text):
    """
    通过飞书群机器人 webhook 发送文本告警。
    发送失败不抛出异常（避免掩盖主错误），仅打印警告并返回 False。
    """
    if not webhook:
        return False
    try:
        payload = {"msg_type": "text", "content": {"text": text}}
        req = urllib.request.Request(webhook, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with urllib.request.urlopen(req, data=data, timeout=15) as resp:
            body = resp.read().decode("utf-8", "ignore")
        try:
            result = json.loads(body)
            ok = result.get("code") == 0 or result.get("Status") == "success" or result.get("ok") is True
        except ValueError:
            ok = False
        if not ok:
            print(f"[WARN] 飞书告警发送未确认成功: {body[:200]}")
        return ok
    except Exception as e:
        print(f"[WARN] 发送飞书告警失败: {e}")
        return False


def build_alert_text(stage, cause, job_url=None):
    """构造告警文本。"""
    lines = [
        "🚨【预警同步失败提醒】",
        f"时间：{now_str()}",
        f"阶段：{stage}",
        f"原因：{str(cause)[:300]}",
    ]
    if job_url:
        lines.append(f"运行日志：{job_url}")
    lines.append("若已配置本机自动化，请检查飞书授权是否过期；否则等待下次自动重试。")
    return "\n".join(lines)


def http_json(method, url, headers=None, payload=None):
    """发起 HTTP 请求，返回解析后的 JSON。读超时/网络错误退避重试 1 次。"""
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_err = None
    for attempt in range(2):  # 首次 + 1 次重试
        try:
            with urllib.request.urlopen(req, data=data, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # HTTP 状态码错误是明确的业务错误，不重试，直接归类
            body = e.read().decode("utf-8", "ignore")
            raise LarkError(f"HTTP {e.code}: {body[:500]}") from e
        except (TimeoutError, urllib.error.URLError) as e:
            # 读超时/网络抖动属临时性错误，退避 2 秒后重试 1 次
            last_err = e
            if attempt == 0:
                time.sleep(2)
                continue
    # 重试耗尽仍失败，转为 LarkError 以便上层归类到具体阶段
    raise LarkError(f"请求超时/网络错误（已重试 1 次仍失败）: {last_err}") from last_err


def get_tenant_token(app_id, app_secret):
    """获取 tenant_access_token（每次调用自动续期）。"""
    url = f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal"
    resp = http_json("POST", url, payload={"app_id": app_id, "app_secret": app_secret})
    if resp.get("code") != 0:
        raise LarkError(f"获取 tenant_access_token 失败: {resp}")
    return resp["tenant_access_token"]


def list_chat_messages(token, chat_id, page_size=50, page_token=None):
    """拉取群消息一页。"""
    url = (
        f"{FEISHU_HOST}/open-apis/im/v1/messages"
        f"?container_id_type=chat&container_id={urllib.parse.quote(chat_id)}"
        f"&sort_type=ByCreateTimeDesc&page_size={page_size}"
        f"&card_msg_content_type=user_card_content"
    )
    if page_token:
        url += f"&page_token={urllib.parse.quote(page_token)}"
    resp = http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
    if resp.get("code") != 0:
        raise LarkError(f"拉取群消息失败: {resp}")
    data = resp.get("data", {})
    return data.get("items", []), data.get("has_more", False), data.get("page_token", "")


def extract_text(content):
    """
    将消息 content 转为纯文本。
    原生 API 的 post 消息 content 是 JSON 字符串（如 {"zh_cn": {"title":..., "content": [[{"tag":"text","text":"..."}]]}}），
    兼容纯文本形态。
    """
    if not content:
        return ""
    # 尝试解析 JSON
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return str(content)

    def walk(node):
        parts = []
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("tag", "name"):
                    continue  # 跳过 tag/name 等元数据（值如 "text"、"a"）
                if isinstance(v, str):
                    parts.append(v)
                elif isinstance(v, (dict, list)):
                    parts.extend(walk(v))
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    parts.extend(walk(item))
                    parts.append("\n")  # 每个段落结束后换行，便于按行解析
                else:
                    parts.extend(walk(item))
        elif isinstance(node, str):
            parts.append(node)
        return parts

    # json.loads 已自动处理 \n 等转义，直接拼接即可
    return "".join(walk(obj))


def clean_md_link(text):
    """清理 markdown 链接 [label](url) -> label；普通 url 保留。"""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("`", "")
    return text.strip()


def parse_alert(content_text):
    """
    从预警消息纯文本中解析字段。
    返回 dict：发件人/主题/命中规则/Hit_Score/法务类别/置信度/理由摘要/详情链接
    解析失败返回 None
    """
    if "法务/侵权邮件预警" not in content_text:
        return None
    lines = content_text.splitlines()

    def val(key):
        for ln in lines:
            if ln.startswith(key + ":"):
                return ln[len(key) + 1:].strip()
            if ln.startswith(key + "：") is False and key + ":" in ln:
                # 兼容 "key: value" 出现在行中
                idx = ln.index(key + ":")
                return ln[idx + len(key) + 1:].strip()
        return None

    fields = {}
    fields["发件人"] = clean_md_link(val("发件人") or "")
    fields["主题"] = clean_md_link(val("主题") or "")
    fields["命中规则"] = (val("命中规则") or "").strip()
    score = val("Hit_Score")
    fields["Hit_Score"] = int(float(score)) if score and score.isdigit() else (int(float(score)) if score else None)
    category = (val("法务类别") or "").strip()
    # select 单选：不在合法选项中的归入"其他"
    fields["法务类别"] = category if category in CATEGORY_OPTIONS else "其他"
    conf = val("置信度")
    try:
        fields["置信度"] = float(conf) if conf else None
    except ValueError:
        fields["置信度"] = None
    fields["理由摘要"] = (val("理由摘要") or "").strip()
    detail = val("详情请点击") or val("详情链接") or ""
    # 提取 URL（优先该行，其次全文兜底）
    m = re.search(r"https?://[^\s)\]]+", detail)
    if not m:
        m = re.search(r"https?://[^\s)\]]+", content_text)
    fields["详情链接"] = m.group(0) if m else ""
    return fields


def parse_interactive_card(card):
    """
    解析原始卡片 JSON（card_msg_content_type=user_card_content 返回的 2.0 结构）。
    - header.title.content 存标题
    - elements 里 div.text.content 存 markdown 字段文本（**字段名:** 值）
    - action.actions[].behaviors[].default_url 存「查看详情」按钮跳转链接
    筛选 title 含"法务/侵权邮件预警"的卡片，返回字段 dict；不符合返回 None。
    """
    header = card.get("header", {})
    title_obj = header.get("title", {})
    title = title_obj.get("content", "") if isinstance(title_obj, dict) else str(title_obj)
    if "法务/侵权邮件预警" not in title:
        return None

    # 提取 markdown 字段文本 与 详情链接
    md_text = ""
    detail_url = ""
    for el in card.get("elements", []):
        if not isinstance(el, dict):
            continue
        tag = el.get("tag")
        if tag == "div":
            t = el.get("text", {})
            if isinstance(t, dict) and t.get("tag") == "lark_md":
                md_text = t.get("content", "")
        elif tag == "action":
            for action in el.get("actions", []):
                if not isinstance(action, dict):
                    continue
                if action.get("tag") == "button":
                    for b in action.get("behaviors", []):
                        if isinstance(b, dict) and b.get("type") == "open_url":
                            u = b.get("default_url", "")
                            if u:
                                detail_url = u

    # 解析 markdown 字段（**字段名:** 值），按字段名位置分割，值可含换行
    kv = {}
    matches = list(re.finditer(r"\*\*([^*:]+):\*\*", md_text))
    for i, m in enumerate(matches):
        key = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        kv[key] = md_text[start:end].strip()

    def val(key):
        return kv.get(key, "").strip()

    fields = {}
    fields["发件人"] = clean_md_link(val("发件人"))
    fields["主题"] = clean_md_link(val("主题"))
    fields["命中规则"] = val("命中规则")
    score = val("Hit_Score")
    try:
        fields["Hit_Score"] = int(float(score)) if score else None
    except (ValueError, TypeError):
        fields["Hit_Score"] = None
    category = val("法务类别")
    fields["法务类别"] = category if category in CATEGORY_OPTIONS else "其他"
    conf = val("置信度")
    try:
        fields["置信度"] = float(conf) if conf else None
    except ValueError:
        fields["置信度"] = None
    fields["理由摘要"] = val("理由摘要")
    fields["详情链接"] = detail_url
    return fields


def parse_message(msg_type, content):
    """按消息类型解析，筛选含"法务/侵权邮件预警"的消息，返回字段 dict 或 None。"""
    if msg_type == "interactive":
        try:
            obj = json.loads(content)
        except (ValueError, TypeError):
            return None
        if isinstance(obj, dict):
            return parse_interactive_card(obj)
    else:
        text = extract_text(content)
        if "法务/侵权邮件预警" in text:
            return parse_alert(text)
    return None


def ts_to_ms_safe(ts_str):
    """create_time 毫秒时间戳字符串 -> int"""
    try:
        return int(ts_str)
    except (ValueError, TypeError):
        return None


def ts_to_date_ms(ts_ms):
    """毫秒时间戳 -> 当天 00:00 (Asia/Shanghai) 的毫秒时间戳，用于「日期（用以分析）」字段。"""
    if not ts_ms:
        return None
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz)
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day_start.timestamp() * 1000)


def fetch_existing_ids(token, base_token, table_id):
    """读取表中全部消息ID，返回 set。"""
    ids = set()
    page_token = ""
    while True:
        # 注：不传 field_names——飞书 API 当前版本对该参数（含中文）返回
        # InvalidFieldNames(1254024)，改为读取全部字段并从记录中提取消息ID。
        url = (
            f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records"
            f"?page_size=500"
        )
        if page_token:
            url += f"&page_token={urllib.parse.quote(page_token)}"
        resp = http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
        if resp.get("code") != 0:
            raise LarkError(f"读取表记录失败: {resp}")
        data = resp.get("data", {})
        for item in data.get("items", []):
            mid = item.get("fields", {}).get("消息ID")
            if mid:
                ids.add(mid)
        if data.get("has_more"):
            page_token = data.get("page_token", "")
        else:
            break
    return ids


def batch_create(token, base_token, table_id, records, batch_size=200, max_retries=4):
    """批量创建记录，遇 QPS 限流(800100001)退避重试。"""
    total = 0
    for i in range(0, len(records), batch_size):
        chunk = records[i:i + batch_size]
        payload = {"records": [{"fields": r} for r in chunk]}
        for attempt in range(max_retries):
            url = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records/batch_create"
            resp = http_json("POST", url, headers={"Authorization": f"Bearer {token}"}, payload=payload)
            code = resp.get("code")
            if code == 0:
                total += len(chunk)
                break
            if code == 800100001:  # QPS 限流
                time.sleep(3 * (attempt + 1))
                continue
            raise LarkError(f"批量写入失败: {resp}")
        else:
            raise LarkError(f"批次重试耗尽仍失败，共 {len(chunk)} 条未写入")
    return total


def main():
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    base_token = os.environ.get("FEISHU_BASE_TOKEN", "").strip()
    table_id = os.environ.get("FEISHU_TABLE_ID", "").strip()
    chat_id = os.environ.get("FEISHU_CHAT_ID", "").strip()
    webhook = os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
    lookback_hours = float(os.environ.get("LOOKBACK_HOURS", "24"))

    missing = [n for n, v in {
        "FEISHU_APP_ID": app_id, "FEISHU_APP_SECRET": app_secret,
        "FEISHU_BASE_TOKEN": base_token, "FEISHU_TABLE_ID": table_id,
        "FEISHU_CHAT_ID": chat_id,
    }.items() if not v]
    if missing:
        msg = f"缺少环境变量: {', '.join(missing)}"
        print(f"[FATAL] {msg}")
        if webhook:
            send_alert(webhook, build_alert_text("环境检查", msg))
        sys.exit(1)

    try:
        # 0. 获取应用凭证
        stage = "获取应用凭证"
        token = get_tenant_token(app_id, app_secret)
        print("[OK] tenant_access_token 获取成功")

        # 1. 拉取群消息（最近 lookback_hours 小时）
        stage = "拉取群消息"
        cutoff = time.time() * 1000 - lookback_hours * 3600 * 1000
        raw_alerts = []
        page_token = ""
        scanned = 0
        for _ in range(200):  # 最多 200 页
            items, has_more, page_token = list_chat_messages(token, chat_id, page_token=page_token)
            if not items:
                break
            for m in items:
                scanned += 1
                ct = ts_to_ms_safe(m.get("create_time"))
                if ct is None:
                    continue
                if ct < cutoff:
                    has_more = False
                    break
                body = m.get("body", {})
                fields = parse_message(m.get("msg_type", ""), body.get("content", ""))
                if fields is not None:
                    raw_alerts.append((m, fields))
            if not has_more:
                break
        print(f"[INFO] 扫描消息 {scanned} 条，其中法务/侵权预警 {len(raw_alerts)} 条")

        # 2. 读取已有消息ID做去重
        stage = "读取表内已有记录"
        existing = fetch_existing_ids(token, base_token, table_id)
        print(f"[INFO] 表中已有 {len(existing)} 条记录（用于去重）")

        # 3. 解析并过滤新消息
        stage = "解析新消息"
        new_records = []
        skipped = 0
        for m, f in raw_alerts:
            mid = m.get("message_id", "")
            if not mid or mid in existing:
                skipped += 1
                continue
            record = {
                "记录时间": ts_to_ms_safe(m.get("create_time")),
                "日期（用以分析）": ts_to_date_ms(ts_to_ms_safe(m.get("create_time"))),
                "发件人": f["发件人"],
                "主题": f["主题"],
                "命中规则": f["命中规则"],
                "Hit_Score": f["Hit_Score"],
                "法务类别": f["法务类别"],
                "置信度": f["置信度"],
                "理由摘要": f["理由摘要"],
                "消息ID": mid,
                "消息链接": f"https://applink.feishu.cn/client/chat/open?openChatId={urllib.parse.quote(chat_id)}&position={mid}",
            }
            # 「详情链接」为超链接类型(type=15)，仅当有 URL 时写对象格式
            if f.get("详情链接"):
                record["详情链接"] = {"text": f["详情链接"], "link": f["详情链接"]}
            # 移除值为 None 的字段（datetime/number 不接受 null）
            record = {k: v for k, v in record.items() if v is not None}
            new_records.append(record)

        print(f"[INFO] 新增待写入 {len(new_records)} 条，跳过重复/无法解析 {skipped} 条")

        # 4. 写入
        stage = "批量写入多维表格"
        if new_records:
            written = batch_create(token, base_token, table_id, new_records)
            print(f"[DONE] 本次新增写入 {written} 条")
        else:
            print("[DONE] 无新增")
    except LarkError as e:
        raise SyncFailed(stage, e) from e


if __name__ == "__main__":
    webhook = os.environ.get("FEISHU_ALERT_WEBHOOK", "").strip()
    job_url = os.environ.get("GITHUB_SERVER_URL", "") and (
        f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    try:
        main()
    except SyncFailed as e:
        text = build_alert_text(e.stage, e.cause, job_url)
        print(f"[FATAL] {text}")
        if webhook:
            send_alert(webhook, text)
        sys.exit(1)
    except Exception as e:  # 未预期异常（网络、编码等）
        text = build_alert_text("未预期异常", e, job_url)
        print(f"[FATAL] {text}")
        if webhook:
            send_alert(webhook, text)
        sys.exit(1)
