# feishu-bitable-proxy 部署指引（Cloudflare Workers 免费版）

> 作用：工作台前端无法直调飞书 API（CORS 不支持，2026-09-07 实测），
> 本 Worker 持有应用凭证转发四张表 + 日报表读写，并补 CORS 头。
> 免费额度 10 万请求/天，工作台量级（每日几百次）余量极大。

## 部署步骤（约 10 分钟，全程网页操作）

1. **注册/登录 Cloudflare**：https://dash.cloudflare.com/sign-up （邮箱注册即可，免费版无需绑卡）；
2. 左侧菜单 → **Workers & Pages** → **Create** → **Create Worker** → 名称填 `feishu-bitable-proxy` → **Deploy**（先用空模板部署成功）→ **Edit code**；
3. 清空编辑器，粘贴本目录 `worker.js` 全部内容 → 右上 **Deploy**；
4. 配置环境变量（凭证不进代码）：Worker 页面 → **Settings** → **Variables and Secrets** → 依次添加：
   | 类型 | Name | Value |
   |---|---|---|
   | Secret | `FEISHU_APP_ID` | `cli_aaad0f934aa21bed` |
   | Secret | `FEISHU_APP_SECRET` | （应用密钥，与 GitHub Secrets 中同值） |
   | Secret | `PROXY_TOKEN` | 自拟随机串（≥24 位，如 `openssl rand -hex 24` 生成），同时发给 AI 配进前端 |
5. 测试（浏览器或 curl，替换 `<worker域名>` 与 `<PROXY_TOKEN>`）：
   ```bash
   curl -X POST "https://feishu-bitable-proxy.<你的子域>.workers.dev/api/search" \
     -H "Content-Type: application/json" -H "X-Proxy-Token: <PROXY_TOKEN>" \
     -d '{"base":"Qd4ubeDLVazKBrspvMXcSY1Xn5c","table":"tblVDbIezF6DSH6D",
          "searchBody":{"filter":{"conjunction":"and","conditions":[{"field_name":"日期","operator":"is","value":["2026-09-07"]}]}}}'
   ```
   返回 `"code":0` 且带当日排班数据 = 部署成功；
6. 把 **Worker 访问域名**（`https://feishu-bitable-proxy.<子域>.workers.dev`）和 **PROXY_TOKEN** 发给 AI，由 AI 完成前端改造。

## 安全说明

- 凭证仅存 Cloudflare 环境变量，代码库中无密钥；
- 路径白名单限定：只能读写「客服小组工作数据」4 张表 + 日报表，其余飞书资源一律 403；
- `X-Proxy-Token` 为共享口令（前端公开页面，属弱防护，与现有前端内嵌 webhook 同级）；若日后要求更强，可加 UA/频率限制或换签名方案。
