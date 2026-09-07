# -*- coding: utf-8 -*-
"""任务对接通知兜底队列消费（云端版）。

前端「流转点击→Webhook 直发」失败时写入「任务对接通知队列」（通知状态=待发送）；
本脚本逐条补发到群（@ 接收人），成功→已发送，失败→失败+错误信息。
队列通常为空、静默结束；发送失败时更新记录状态（不重复群告警）。
"""

import sys
from datetime import datetime, timezone, timedelta

from . import config, feishu_api
from .feishu_api import base_search

CST = timezone(timedelta(hours=8))


def main() -> int:
    queue = base_search(config.QUEUE_TABLE,
                        conditions=[{"field_name": "通知状态", "operator": "is",
                                     "value": ["待发送"]}],
                        field_names=["任务名称", "文档链接", "接收人", "操作人",
                                     "通知状态", "重试次数"])
    if not queue:
        print("[queue] 队列为空，静默结束")
        return 0
    print(f"[queue] 待发送 {len(queue)} 条")
    fail = 0
    for q in queue:
        f = q["fields"]
        receivers = [n.strip() for n in (f.get("接收人") or "").replace("，", "、").split("、") if n.strip()]
        lines = [f"📋 任务流转通知：{f.get('任务名称') or ''}"]
        if f.get("文档链接"):
            lines.append(f"文档：{f['文档链接']}")
        if f.get("操作人"):
            lines.append(f"操作人：{f['操作人']}")
        ok_all = True
        err = ""
        for name in receivers:
            try:
                feishu_api.send_group_message("任务对接通知",
                                              config.OPEN_IDS.get(name), lines)
            except Exception as e:  # noqa: BLE001
                ok_all = False
                err = str(e)
                print(f"   ❌ {name}: {e}")
        status = "已发送" if ok_all else "失败"
        if not ok_all:
            fail += 1
        try:
            feishu_api.base_update(config.QUEUE_TABLE, q["record_id"], {
                "通知状态": status,
                "发送时间": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                "错误信息": err,
            })
        except Exception as e:  # noqa: BLE001
            print(f"   [状态回写失败] {f.get('任务名称')}: {e}")
    print(f"[queue] 完成，失败 {fail} 条")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
