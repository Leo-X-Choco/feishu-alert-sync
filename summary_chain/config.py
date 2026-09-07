# -*- coding: utf-8 -*-
"""云端链路共享配置：表 ID、成员、群、日报文档等。"""

import os

FEISHU_HOST = "https://open.feishu.cn"

# 多维表格「客服小组工作数据」
BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "Qd4ubeDLVazKBrspvMXcSY1Xn5c")
SUMMARY_TABLE = os.environ.get("FEISHU_SUMMARY_TABLE", "tblG2f1FnNCUyFSM")
SCHED_TABLE = os.environ.get("FEISHU_SCHED_TABLE", "tblVDbIezF6DSH6D")
TAB_TABLE = os.environ.get("FEISHU_TAB_TABLE", "tbl37M2meI0EG3bM")
QUEUE_TABLE = os.environ.get("FEISHU_QUEUE_TABLE", "tblSTXDvYP8rKlaC")

# 飞书日报多维表格「海外客服三组日报」（2026-09-07 起，替代原 docx 日报文档）
# wiki 节点 OAuLw7z9Sixj9jkYoykcEgPinsh → base CixAbQERqaOifistxhIcdTkcnue
DAILY_BITABLE_TOKEN = os.environ.get("FEISHU_DAILY_BITABLE", "CixAbQERqaOifistxhIcdTkcnue")
DAILY_BITABLE_TABLE = os.environ.get("FEISHU_DAILY_BITABLE_TABLE", "tbl7oMLDrYaWRGwd")
DAILY_FIELD_DATE = "时间"            # datetime，行日期
DAILY_FIELD_NAME = "姓名"            # select（李燕芳/胡镭/李玉婷/方佳莹）
DAILY_FIELD_WORK = "今日工作情况"     # text，小结写入目标

# 飞书日报文档与提醒群
DAILY_DOC_ID = os.environ.get("FEISHU_DAILY_DOC", "GlpCduwAropB88xvWpLcuEFinff")
CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_485dd8d59a0115a43870c289994f429d")
ALERT_WEBHOOK = os.environ.get("FEISHU_ALERT_WEBHOOK", "")

# 成员与 open_id（企业租户内固定）
MEMBERS = ["许磊", "李燕芳", "胡镭", "李玉婷", "方佳莹"]
OPEN_IDS = {
    "许磊": "ou_716ef6bab4627dd72d16a05debf42ca9",
    "李燕芳": "ou_a3789ac8629f6002adf72686996e609c",
    "胡镭": "ou_aa7d598d3a0141410c7dbfbfdff52727",
    "李玉婷": "ou_5564e4682e3352ecab51d9d2ba432dee",
    "方佳莹": "ou_9e78dc404bfb1d536b1cf6d34615763d",
}
NAME_ALIASES = {"Emily": "李玉婷"}

# 在岗判定（2026-09-06 确认）：以下状态 = 不在岗；无记录/其他状态 = 在岗
OFF_DUTY_STATUSES = ("休息", "请假", "节假日")

# 日报文档结构性跳过成员（日报表格中无对应行、无法回写）
STRUCTURAL_SKIP = {"许磊"}

# 日报表格关键列名
NAME_COLUMN = "姓名"
WORK_COLUMN = "今日工作情况"

# 好评提醒目标选项卡
PRAISE_TAB_NAME = "好评相关"
PRAISE_REMIND_TIME = "10:00"


def iso_today_cst() -> str:
    """当前北京时区日期 YYYY-MM-DD。"""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def iso_to_daily_title(iso_date: str) -> str:
    """2026-09-01 → 2026.9.1（去前导零，匹配日报标题格式）。"""
    y, m, d = iso_date.split("-")
    return f"{int(y)}.{int(m)}.{int(d)}"


def send_alert(text: str) -> bool:
    """群 Webhook 告警（best-effort，失败不抛异常）。"""
    if not ALERT_WEBHOOK:
        print("[告警未配置] " + text)
        return False
    import json
    import urllib.request
    try:
        payload = {"msg_type": "text", "content": {"text": text}}
        req = urllib.request.Request(ALERT_WEBHOOK, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(
                req, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=15) as resp:
            resp.read()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[告警发送失败] {e}")
        return False
