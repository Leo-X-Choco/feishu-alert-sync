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

const ALLOWED = {
  // 客服小组工作数据（四张工作台表）
  Qd4ubeDLVazKBrspvMXcSY1Xn5c: new Set([
    "tblG2f1FnNCUyFSM", // 工作小结
    "tblVDbIezF6DSH6D", // 排班表
    "tbl37M2meI0EG3bM", // 工作选项卡
    "tblSTXDvYP8rKlaC", // 任务对接通知队列
  ]),
  // 海外客服三组日报（小结同步目标）
  CixAbQERqaOifistxhIcdTkcnue: new Set(["tbl7oMLDrYaWRGwd"]),
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
