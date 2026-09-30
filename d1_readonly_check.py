# -*- coding: utf-8 -*-
"""d1_readonly_check.py —— 只读查证 D1 近期数据（工作台「新增记录丢失」排查）
仅 SELECT，零写入；凭据来自仓库 secrets（CF_ACCOUNT_ID / CF_API_TOKEN），绝不打印密钥。
退出码：0=查证完成；2=凭据/权限问题；3=网络/传输问题。"""
import os, sys, json, time, urllib.request, urllib.error

ACCT = os.environ.get("CF_ACCOUNT_ID", "").strip()
TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
SINCE = (os.environ.get("SINCE") or "2026-09-24").strip()
SINCE_W = "2026-09-21"  # 按日计数窗口起点

def api(method, path, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))

def sql(dbid, text, tag, tries=2):
    last = ""
    for i in range(tries):
        try:
            st, j = api("POST", "/accounts/%s/d1/database/%s/query" % (ACCT, dbid), {"sql": text})
            if st == 200 and j.get("success"):
                res0 = (j.get("result") or [{}])[0]
                return True, (res0.get("results") or [])
            last = "HTTP %s %s" % (st, json.dumps(j.get("errors"))[:160])
        except urllib.error.HTTPError as e:
            last = "HTTP %d %s" % (e.code, e.read(200).decode("utf-8", "replace"))
            if e.code == 403:
                print("[AUTH] token 无 D1 查询权限（403）：%s" % last); sys.exit(2)
        except Exception as e:
            last = "%s %s" % (type(e).__name__, str(e)[:160])
        time.sleep(1.5)
    print("[%s] 查询失败：%s" % (tag, last))
    return False, []

def dump(rows, cols, w=(42,)):
    print("   共 %d 行" % len(rows))
    for r in rows:
        cells = []
        for i, c in enumerate(cols):
            lim = w[0] if i >= len(w) else w[i]
            cells.append(str(r.get(c, "") if r.get(c) is not None else "").replace("\n", " ")[:lim])
        print("     " + " | ".join(cells))

def main():
    if not ACCT or not TOKEN:
        print("[AUTH] 缺 secret（CF_ACCOUNT_ID/CF_API_TOKEN）"); sys.exit(2)
    print("== D1 只读查证  since明细=%s  计数窗口=%s ==" % (SINCE, SINCE_W))
    try:
        st, j = api("GET", "/accounts/%s/d1/database" % ACCT)
    except urllib.error.HTTPError as e:
        print("[AUTH/NET] 数据库列表失败 HTTP %d" % e.code); sys.exit(2 if e.code in (401, 403) else 3)
    except Exception as e:
        print("[NET] 数据库列表失败：%s %s" % (type(e).__name__, str(e)[:160])); sys.exit(3)
    if not (st == 200 and j.get("success")):
        print("[AUTH] 列表失败：%s" % json.dumps(j.get("errors"))[:200]); sys.exit(2)
    dbid = None
    for d in (j.get("result") or []):
        print("   库: %s uuid=%s" % (d.get("name"), d.get("uuid")))
        if d.get("name") == "kf-workbench": dbid = d.get("uuid")
    if not dbid:
        print("[ERR] 未找到 kf-workbench"); sys.exit(3)
    Q = dbid

    ok, tabs = sql(Q, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", "表清单")
    if not ok: sys.exit(3)
    names = [r["name"] for r in tabs]
    print("[1] 表清单(%d): %s" % (len(names), ", ".join(names)))

    def pick(friendly, tblid):
        return friendly if friendly in names else (tblid if tblid in names else None)
    T_REC = pick("work_rec", "tblWUGZ41GD5IBwW")
    T_SUM = pick("summary", "tblG2f1FnNCUyFSM")
    T_TAB = pick("tabs", "tbl37M2meI0EG3bM")
    T_SCH = pick("schedule", "tblVDbIezF6DSH6D")
    print("[2] 目标表: work_rec=%s summary=%s tabs=%s schedule=%s" % (T_REC, T_SUM, T_TAB, T_SCH))

    print("\n[3] 总行数与最后写入（东八区）")
    for tag, t in (("工作记录", T_REC), ("工作小结", T_SUM), ("选项卡", T_TAB), ("排班", T_SCH)):
        if not t: continue
        ok, rows = sql(Q, "SELECT COUNT(*) n, MAX(updated_ms) mx FROM %s" % t, tag)
        r0 = rows[0] if rows else {}
        mx = r0.get("mx")
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(mx) / 1000 + 8 * 3600)) if mx else "-"
        print("   %-6s 总行数=%s  最后写入=%s" % (tag, r0.get("n"), last))

    if T_REC:
        print("\n[4] 工作记录·按日计数（%s 起）" % SINCE_W)
        ok, rows = sql(Q, "SELECT f_date d, COUNT(*) n FROM %s WHERE f_date>='%s' GROUP BY f_date ORDER BY f_date" % (T_REC, SINCE_W), "rec按日")
        if ok:
            for r in rows: print("   %s  %s 条" % (r["d"], r["n"]))
        print("[5] 工作记录·明细（%s 起，新→旧）" % SINCE)
        ok, rows = sql(Q, ("SELECT record_id, f_date, f_member, f_item, f_status, substr(COALESCE(f_desc,''),1,36) d,"
                           " datetime(updated_ms/1000,'unixepoch','+8 hours') u FROM %s"
                           " WHERE f_date>='%s' ORDER BY f_date DESC, _ord LIMIT 80") % (T_REC, SINCE), "rec明细")
        if ok: dump(rows, ["f_date", "f_member", "f_item", "f_status", "d", "u"])

    if T_SUM:
        print("\n[6] 工作小结·按日计数（%s 起）" % SINCE_W)
        ok, rows = sql(Q, "SELECT f_date d, COUNT(*) n FROM %s WHERE f_date>='%s' GROUP BY f_date ORDER BY f_date" % (T_SUM, SINCE_W), "sum按日")
        if ok:
            for r in rows: print("   %s  %s 条" % (r["d"], r["n"]))
        print("[7] 工作小结·明细（%s 起，新→旧）" % SINCE)
        ok, rows = sql(Q, ("SELECT record_id, f_date, f_member, substr(COALESCE(f_summary,''),1,40) s,"
                           " datetime(updated_ms/1000,'unixepoch','+8 hours') u FROM %s"
                           " WHERE f_date>='%s' ORDER BY f_date DESC LIMIT 60") % (T_SUM, SINCE), "sum明细")
        if ok: dump(rows, ["f_date", "f_member", "s", "u"])

    if T_TAB:
        print("\n[8] 选项卡·近 10 天新建/更新")
        ten_ms = int((time.time() - 10 * 86400) * 1000)
        ok, rows = sql(Q, ("SELECT f_name, f_creator, f_current_owner,"
                           " datetime(created_ms/1000,'unixepoch','+8 hours') c,"
                           " datetime(updated_ms/1000,'unixepoch','+8 hours') u FROM %s"
                           " WHERE created_ms>%d OR updated_ms>%d"
                           " ORDER BY MAX(created_ms,updated_ms) DESC LIMIT 30") % (T_TAB, ten_ms, ten_ms), "tabs")
        if ok: dump(rows, ["c", "u", "f_name", "f_creator", "f_current_owner"], w=(20, 20, 16, 12, 12))

    if T_SCH:
        print("\n[9] 排班·%s 起按日行数" % SINCE)
        ok, rows = sql(Q, "SELECT f_date d, COUNT(*) n FROM %s WHERE f_date>='%s' GROUP BY f_date ORDER BY f_date" % (T_SCH, SINCE), "sch按日")
        if ok:
            for r in rows: print("   %s  %s 条" % (r["d"], r["n"]))

    print("\n== 查证结束（只读，零写入） ==")

if __name__ == "__main__":
    main()
