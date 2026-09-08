// feishu-bitable-proxy —— 客服小组工作台专用轻量代理
// ============================================================
// 作用：持有飞书自建应用凭证，转发工作台四张表（小结/排班/选项卡/队列）
//       与日报多维表格的增删改查，并补 CORS 头（飞书开放平台不支持浏览器直调）。
//
// 安全设计：
//   1. 凭证只存 Cloudflare 环境变量（FEISHU_APP_ID / FEISHU_APP_SECRET），不进代码库；
//   2. 路径白名单：仅放行指定 base/table 的 records 与 fields 接口，其余一律 403；
//   3. 共享口令：请求须带 X-Proxy-Token 头（等于 PROXY_TOKEN 环境变量）——
//      防止陌生人扫到 Worker 地址就白嫖/篡改（前端为公开页面，口令属弱防护，
//      与现有前端内嵌 webhook 的暴露级别一致）。
//
// 允许的飞书 API（全部只读/读写限定在白名单资源内）：
//   POST /api/search   → bitable records/search（body: {base, table, ...searchBody}）
//   POST /api/create   → bitable records/batch_create
//   POST /api/update   → bitable records/{record_id} PUT（body: {base, table, record_id, fields}）
//   POST /api/delete   → bitable records/batch_delete
//   GET  /api/fields   → 某表字段列表（query: base, table）
//   POST /api/token    → 换取短期 tenant_token（可选：前端自己调飞书只读接口时用）
//   POST /api/upload   → drive medias/upload_all（body: {file_name, data_base64, size}）
//                        上传为 bitable_image 附件（parent_node 固定为工作台 Base），
//                        返回 file_token 供前端写入小结表「图片」附件字段
//   POST /api/field-create  → 新增字段（body: {base, table, field_name, type, options?/property?}）
//   POST /api/field-update  → 字段改名/改属性（body: {base, table, field_id, field_name, type, options?}）
//   POST /api/field-delete  → 删除字段（body: {base, table, field_id, field_name}；初始字段受保护）

const ALLOWED = {
  // 客服小组工作数据（四张工作台表）
  Qd4ubeDLVazKBrspvMXcSY1Xn5c: new Set([
    "tblG2f1FnNCUyFSM", // 工作小结
    "tblVDbIezF6DSH6D", // 排班表
    "tbl37M2meI0EG3bM", // 工作选项卡
    "tblSTXDvYP8rKlaC", // 任务对接通知队列
    "tbl013TV9PFTT1Or", // 考核基本信息
    "tblEi5eafUHFZiXA", // 考核阶段规划
    "tblqsfGvFBIKY6V1", // 考核实操评分
  ]),
  // 海外客服三组日报（小结同步目标）
  CixAbQERqaOifistxhIcdTkcnue: new Set(["tbl7oMLDrYaWRGwd"]),
};

// 考核三表的初始字段（2026-09-08 迁移快照）——field-delete 拒绝删除这些列，防误删迁移数据
const EXAM_PROTECTED_FIELDS = {
  tbl013TV9PFTT1Or: ["姓名", "状态", "入职日期", "带教人", "培训周期", "表格维护人", "平时成绩",
    "实操成绩", "综合成绩", "结业理论成绩", "触碰红线", "验收结论", "进组结果", "签字确认"],
  tblEi5eafUHFZiXA: ["姓名", "阶段", "天数", "培训考核内容", "考核方式"],
  tblqsfGvFBIKY6V1: ["姓名", "日期", "分发量", "实际完成量", "质检合格分", "服务加分",
    "带教人服务分", "带教人评语", "新人处理情况反馈"],
};

// 允许的浏览器来源（工作台页面所在域）
const ALLOW_ORIGINS = new Set([
  "https://www.workbuddy.cn",
  "https://workbuddy.cn",
  // 资料库静态产物预览域（前缀匹配在下方单独处理）
]);

function corsHeaders(origin) {
  const ok = ALLOW_ORIGINS.has(origin) || /\.workbuddy\.cn$/.test(new URL(origin || "https://x.invalid").hostname) || /workbuddy/.test(origin || "");
  return {
    "Access-Control-Allow-Origin": ok ? origin : "",
    "Access-Control-Allow-Headers": "Content-Type, X-Proxy-Token",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

let _token = { v: "", exp: 0 };

async function tenantToken(env) {
  if (_token.v && Date.now() < _token.exp) return _token.v;
  const r = await fetch("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app_id: env.FEISHU_APP_ID, app_secret: env.FEISHU_APP_SECRET }),
  });
  const d = await r.json();
  if (d.code !== 0) throw new Error("tenant_token 获取失败: " + JSON.stringify(d).slice(0, 200));
  _token = { v: d.tenant_access_token, exp: Date.now() + (d.expire - 300) * 1000 };
  return _token.v;
}

function checkAllowed(base, table) {
  return !!ALLOWED[base] && ALLOWED[base].has(table);
}

function json(obj, status, origin) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(origin) },
  });
}

async function feishu(env, path, init) {
  const token = await tenantToken(env);
  const r = await fetch("https://open.feishu.cn" + path, {
    ...init,
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json; charset=utf-8",
      ...(init.headers || {}),
    },
  });
  return r.json();
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(origin) });

    const url = new URL(request.url);
    try {
      // 口令校验
      if (!env.PROXY_TOKEN || request.headers.get("X-Proxy-Token") !== env.PROXY_TOKEN) {
        return json({ code: 401, msg: "bad proxy token" }, 401, origin);
      }

      if (request.method === "POST" && url.pathname === "/api/token") {
        return json({ code: 0, data: { token: await tenantToken(env) } }, 200, origin);
      }

      // 图片上传（小结附件）：base64 JSON 入参 → 飞书 drive upload_all（bitable_image）
      if (request.method === "POST" && url.pathname === "/api/upload") {
        const body = await request.json();
        // parent_node 固定为工作台 Base，防止把文件挂到任意资源
        if (body.base !== "Qd4ubeDLVazKBrspvMXcSY1Xn5c") {
          return json({ code: 403, msg: "resource not allowed" }, 403, origin);
        }
        const raw = String(body.data_base64 || "");
        const size = Math.floor(raw.length * 3 / 4); // base64 近似原大小
        if (!body.file_name || !raw) return json({ code: 400, msg: "file_name/data_base64 required" }, 400, origin);
        if (size > 15 * 1024 * 1024) return json({ code: 400, msg: "file too large (max 15MB)" }, 400, origin);
        const bytes = Uint8Array.from(atob(raw), (c) => c.charCodeAt(0));
        const mime = /^image\/(png|jpeg|jpg|gif|webp)$/i.test(body.mime || "") ? body.mime : "image/png";
        const fd = new FormData();
        fd.append("file_name", String(body.file_name).slice(0, 200));
        fd.append("parent_type", "bitable_image");
        fd.append("parent_node", body.base);
        fd.append("size", String(bytes.byteLength));
        fd.append("file", new Blob([bytes], { type: mime }), String(body.file_name).slice(0, 200));
        const token = await tenantToken(env);
        const r = await fetch("https://open.feishu.cn/open-apis/drive/v1/medias/upload_all", {
          method: "POST",
          headers: { Authorization: "Bearer " + token },
          body: fd,
        });
        const d = await r.json();
        return json(d, 200, origin);
      }

      // 字段管理（考核三表）：create / update(改名/改类型) / delete（受保护名单约束）
      if (request.method === "POST" && url.pathname.startsWith("/api/field-")) {
        const body = await request.json();
        if (!checkAllowed(body.base, body.table)) return json({ code: 403, msg: "resource not allowed" }, 403, origin);
        const root = `/open-apis/bitable/v1/apps/${body.base}/tables/${body.table}/fields`;
        const op = url.pathname.slice("/api/field-".length);
        let path, init;
        if (op === "create") {
          path = root;
          init = { method: "POST", body: JSON.stringify({
            field_name: body.field_name, type: body.type,
            property: body.property || (body.options ? { options: body.options } : undefined),
          }) };
        } else if (op === "update") {
          if (!body.field_id) return json({ code: 400, msg: "field_id required" }, 400, origin);
          path = `${root}/${body.field_id}`;
          init = { method: "PUT", body: JSON.stringify({
            field_name: body.field_name, type: body.type,
            property: body.property || (body.options ? { options: body.options } : undefined),
          }) };
        } else if (op === "delete") {
          const protectedNames = EXAM_PROTECTED_FIELDS[body.table] || [];
          if (protectedNames.includes(body.field_name)) {
            return json({ code: 403, msg: `字段「${body.field_name}」为迁移初始字段，禁止删除` }, 403, origin);
          }
          if (!body.field_id) return json({ code: 400, msg: "field_id required" }, 400, origin);
          path = `${root}/${body.field_id}`;
          init = { method: "DELETE" };
        } else {
          return json({ code: 404, msg: "not found" }, 404, origin);
        }
        const d = await feishu(env, path, init);
        return json(d, 200, origin);
      }

      if (request.method !== "POST" && !(request.method === "GET" && url.pathname === "/api/fields")) {
        return json({ code: 405, msg: "method not allowed" }, 405, origin);
      }

      const body = request.method === "POST" ? await request.json() : {};

      if (url.pathname === "/api/fields") {
        const base = url.searchParams.get("base"), table = url.searchParams.get("table");
        if (!checkAllowed(base, table)) return json({ code: 403, msg: "resource not allowed" }, 403, origin);
        const d = await feishu(env, `/open-apis/bitable/v1/apps/${base}/tables/${table}/fields?page_size=100`, { method: "GET" });
        return json(d, 200, origin);
      }

      const { base, table } = body;
      if (!checkAllowed(base, table)) return json({ code: 403, msg: "resource not allowed" }, 403, origin);
      const root = `/open-apis/bitable/v1/apps/${base}/tables/${table}/records`;

      switch (url.pathname) {
        case "/api/search": {
          const { searchBody } = body;
          const d = await feishu(env, `${root}/search`, {
            method: "POST",
            body: JSON.stringify(searchBody || {}),
          });
          return json(d, 200, origin);
        }
        case "/api/create": {
          const d = await feishu(env, `${root}/batch_create`, {
            method: "POST",
            body: JSON.stringify({ records: (body.records || []).map((f) => ({ fields: f })) }),
          });
          return json(d, 200, origin);
        }
        case "/api/update": {
          const d = await feishu(env, `${root}/${body.record_id}`, {
            method: "PUT",
            body: JSON.stringify({ fields: body.fields }),
          });
          return json(d, 200, origin);
        }
        case "/api/delete": {
          const d = await feishu(env, `${root}/batch_delete`, {
            method: "POST",
            body: JSON.stringify({ records: body.record_ids || [] }),
          });
          return json(d, 200, origin);
        }
        default:
          return json({ code: 404, msg: "not found" }, 404, origin);
      }
    } catch (e) {
      return json({ code: 500, msg: String(e).slice(0, 300) }, 500, origin);
    }
  },
};
