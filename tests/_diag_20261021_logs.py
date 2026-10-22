# -*- coding: utf-8 -*-
"""重试拉取 job 日志：手动处理 302 跳转（跳转时剥离 Authorization）。"""
import json
import ssl
import urllib.request
import urllib.error

REPO = "Leo-X-Choco/feishu-alert-sync"
JOB_ID = 119065500102

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
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 第一步：请求日志 URL（allow_redirects=False）
req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/actions/jobs/{JOB_ID}/logs", method="GET")
req.add_header("Authorization", f"Bearer {TOKEN}")
req.add_header("Accept", "application/vnd.github+json")
req.add_header("User-Agent", "xl-diag")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener_nr = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
try:
    r = opener_nr.open(req, timeout=30)
    log = r.read().decode("utf-8", "ignore")
    print("(直接返回内容，前 200 行)")
    for ln in log.splitlines()[:200]:
        print(ln[:500])
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code} {e.reason}")
    loc = e.headers.get("Location")
    print(f"Location: {loc}")
    if loc and e.code in (301, 302, 303, 307, 308):
        req2 = urllib.request.Request(loc, method="GET")
        req2.add_header("User-Agent", "xl-diag")
        with OPENER.open(req2, timeout=60) as r2:
            log = r2.read().decode("utf-8", "ignore")
        print(f"(跳转后日志共 {len(log.splitlines())} 行)")
        # 找关键错误行
        keys = ["LarkError", "HTTP 500", "拉取群消息", "2200", "Traceback", "Error", "FAIL"]
        hit = [ln for ln in log.splitlines() if any(k in ln for k in keys)]
        print("=== 关键错误行 ===")
        for ln in hit[:40]:
            print(ln[:500])
        print("=== 尾部 40 行 ===")
        for ln in log.splitlines()[-40:]:
            print(ln[:500])
