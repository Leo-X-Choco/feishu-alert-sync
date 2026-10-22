# -*- coding: utf-8 -*-
"""排查 2026-10-21 14:00 预警同步失败：拉取 Actions 运行列表与失败 run 的 job 详情。"""
import json
import ssl
import sys
import urllib.request

REPO = "Leo-X-Choco/feishu-alert-sync"
FAILED_RUN = 39552770287

def get_token():
    import subprocess
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return None

TOKEN = get_token()
CTX = ssl.create_default_context()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(url):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "xl-diag")
    with OPENER.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

# 1) 今天的运行列表（重点 05:00Z-08:00Z 即北京 13:00-16:00）
runs = api(f"https://api.github.com/repos/{REPO}/actions/runs?per_page=60&created=2026-10-21")
print("=== 2026-10-21 全部运行 ===")
print(f"total_count={runs['total_count']}")
rows = []
for r in runs["workflow_runs"]:
    rows.append({
        "id": r["id"], "wf": r["name"], "event": r["event"],
        "created": r["created_at"], "status": r["status"], "concl": r["conclusion"],
    })
# 按 created 排序输出
for row in sorted(rows, key=lambda x: x["created"]):
    print(json.dumps(row, ensure_ascii=False))

# 2) 失败 run 的 job / step 详情
print("\n=== 失败 run 39552770287 的 jobs ===")
jobs = api(f"https://api.github.com/repos/{REPO}/actions/runs/{FAILED_RUN}/jobs")
for j in jobs["jobs"]:
    print(f"job: {j['name']} status={j['status']} concl={j['conclusion']} started={j['started_at']} finished={j['completed_at']}")
    for s in j["steps"]:
        print(f"  step: {s['name']} status={s['status']} concl={s['conclusion']} at={s['completed_at']}")

# 3) 拉失败 job 的日志尾部
if jobs["jobs"]:
    jid = jobs["jobs"][0]["id"]
    print(f"\n=== job {jid} 日志尾部 ===")
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/actions/jobs/{jid}/logs", method="GET")
        req.add_header("Authorization", f"Bearer {TOKEN}")
        req.add_header("User-Agent", "xl-diag")
        with OPENER.open(req, timeout=60) as r:
            log = r.read().decode("utf-8", "ignore")
        lines = log.splitlines()
        print(f"(共 {len(lines)} 行，输出最后 80 行)")
        for ln in lines[-80:]:
            print(ln[:400])
    except urllib.error.HTTPError as e:
        print(f"日志拉取失败 HTTP {e.code}（日志过期或被清理）")
