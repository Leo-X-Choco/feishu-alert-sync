# feishu-alert-sync

飞书群「⚖️ 法务/侵权邮件预警」卡片消息 → 飞书多维表格（Bitable）自动同步。

通过 GitHub Actions 定时拉取指定飞书群的新消息，解析预警卡片字段并幂等写入多维表格；失败时通过飞书群机器人 Webhook 发送告警。

## 功能特性

- **定时同步**：GitHub Actions cron 调度（`*/10`），无需自建服务器
- **原始卡片解析**：使用 `card_msg_content_type=user_card_content` 拉取发送时的原始卡片 JSON（2.0 结构），可提取「查看详情」按钮的跳转链接（`behaviors[].default_url`）
- **幂等去重**：以消息 `message_id` 去重，重复运行安全
- **失败自愈**：读超时/网络错误自动重试；飞书服务端瞬时错误码（如 `1255002`）退避重试
- **失败告警**：脚本内告警 + workflow 级兜底通知，双通道推送飞书群机器人
- **零第三方依赖**：纯 Python 标准库（`urllib`/`json`），Python 3.10+ 即可

## 部署

1. 在飞书开放平台创建企业自建应用，开通 `bitable:app`、`im:message.group_msg` 等权限，并将应用机器人加入目标群
2. Fork / 使用本仓库，在 **Settings → Secrets and variables → Actions** 配置以下 Secrets：

   | Secret | 说明 |
   |--------|------|
   | `FEISHU_APP_ID` | 自建应用 App ID |
   | `FEISHU_APP_SECRET` | 自建应用 App Secret |
   | `FEISHU_BASE_TOKEN` | 多维表格 Base token（形如 `FjXXXXXXXXXXXXXXXXXXXXXX`） |
   | `FEISHU_TABLE_ID` | 数据表 ID（形如 `tblXXXXXXXXXXXXXXXXXX`） |
   | `FEISHU_CHAT_ID` | 群 chat_id（形如 `oc_xxxxxxxxxxxxxxxxxxxxxxxx`） |
   | `FEISHU_ALERT_WEBHOOK` | 可选，失败告警群机器人 Webhook |

3. 在多维表格中按脚本字段建表（记录时间 / 发件人 / 主题 / 命中规则 / Hit_Score / 法务类别 / 置信度 / 理由摘要 / 详情链接 / 消息ID / 消息链接）
4. 触发一次 **Run workflow** 验证，之后定时自动同步

> 注意：私有仓库有 Actions 分钟数额度限制，高频 cron 建议使用公开仓库或自建 runner。

## 许可与版权

Copyright © 2026 Leo-X-Choco. All Rights Reserved.

本项目仅供个人学习与研究参考。**未经作者书面授权，禁止复制、分发、再发布及任何形式的商业使用。** 如需授权或引用，请联系仓库所有者。
