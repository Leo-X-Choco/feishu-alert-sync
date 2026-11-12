# -*- coding: utf-8 -*-
"""manual_doc.py —— 客服手册「处理场景总览」飞书文档生成
用法（Actions 环境调用）：
  python manual_doc.py "<title>" <content_b64>
流程：tenant_token → 创建文档 → markdown→blocks（convert API）→ 分批插入（每批 50 块）
输出：打印 doc_url（供回传）
"""
import io
import base64
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import os
import urllib.request

APP_ID = "cli_aaad0f934aa21bed"
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
OPEN_ID_FALLBACK = "ou_716ef6bab4627dd72d16a05debf42ca9"  # 未用；备用


def http_json(url, body=None, token=None, method=None):
    headers = {"Content-Type": "application/json", "User-Agent": "manual-doc"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
    return json.load(urllib.request.urlopen(rq, timeout=30))


def replace_doc(doc_id, md):
    tk = http_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                   {"app_id": APP_ID, "app_secret": APP_SECRET})
    token = tk.get("tenant_access_token")
    if not token:
        print("no token")
        sys.exit(1)
    # 1. 列出现有顶层子块并删除（全量替换）
    children_url = ("https://open.feishu.cn/open-apis/docx/v1/documents/" + doc_id +
                    "/blocks/" + doc_id + "/children?document_revision_id=-1&page_size=500")
    lst = http_json(children_url, token=token, method="GET")
    if lst.get("code") != 0:
        print("list children failed:", json.dumps(lst)[:300])
        sys.exit(1)
    ids = [b["block_id"] for b in lst["data"].get("items", [])]
    if ids:
        del_r = http_json("https://open.feishu.cn/open-apis/docx/v1/documents/" + doc_id +
                          "/blocks/" + doc_id + "/children", {"start_index": 0, "end_index": len(ids)},
                          token=token, method="DELETE")
        if del_r.get("code") != 0:
            print("delete children failed:", json.dumps(del_r)[:300])
            sys.exit(1)
        print("deleted", len(ids), "old blocks")
    # 2. markdown → blocks 并分批插入
    conv = http_json("https://open.feishu.cn/open-apis/docx/v1/documents/blocks/convert",
                     {"content_type": "markdown", "content": md}, token=token)
    if conv.get("code") != 0:
        print("convert failed:", json.dumps(conv)[:300])
        sys.exit(1)
    blocks = conv["data"]["blocks"]
    idx = 0
    for start in range(0, len(blocks), 50):
        chunk = blocks[start:start + 50]
        url = ("https://open.feishu.cn/open-apis/docx/v1/documents/" + doc_id +
               "/blocks/" + doc_id + "/children?document_revision_id=-1")
        r = http_json(url, {"index": idx, "children": chunk}, token=token)
        if r.get("code") != 0:
            print("insert batch failed:", json.dumps(r)[:300])
            sys.exit(1)
        idx += len(chunk)
        print("batch inserted", len(chunk), "(total", idx, ")")
    print("REPLACE_OK blocks=", idx)
    print("DOC_URL=https://www.feishu.cn/docx/" + doc_id)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "create"
    if mode == "replace":
        doc_id = sys.argv[2]
        content_b64 = sys.argv[3]
        md = base64.b64decode(content_b64).decode("utf-8")
        replace_doc(doc_id, md)
        return
    title = sys.argv[1] if len(sys.argv) > 1 else "处理场景总览"
    content_b64 = sys.argv[2] if len(sys.argv) > 2 else ""
    md = base64.b64decode(content_b64).decode("utf-8")

    tk = http_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                   {"app_id": APP_ID, "app_secret": APP_SECRET})
    token = tk.get("tenant_access_token")
    if not token:
        print("no token:", json.dumps(tk)[:200])
        sys.exit(1)

    # 1. 创建文档
    doc = http_json("https://open.feishu.cn/open-apis/docx/v1/documents",
                    {"folder_token": "", "title": title}, token=token)
    if doc.get("code") != 0:
        print("create doc failed:", json.dumps(doc, ensure_ascii=False)[:300])
        sys.exit(1)
    doc_id = doc["data"]["document"]["document_id"]
    print("doc_id:", doc_id)

    # 2. markdown → blocks
    conv = http_json("https://open.feishu.cn/open-apis/docx/v1/documents/blocks/convert",
                     {"content_type": "markdown", "content": md}, token=token)
    if conv.get("code") != 0:
        print("convert failed:", json.dumps(conv, ensure_ascii=False)[:300])
        sys.exit(1)
    blocks = conv["data"]["blocks"]
    print("blocks:", len(blocks))

    # 3. 分批插入（每批 50 个顶层块；index 追加到文档根块）
    total = len(blocks)
    idx = 0
    batch_no = 0
    for start in range(0, total, 50):
        chunk = blocks[start:start + 50]
        url = ("https://open.feishu.cn/open-apis/docx/v1/documents/" + doc_id +
               "/blocks/" + doc_id + "/children?document_revision_id=-1")
        r = http_json(url, {"index": idx, "children": chunk}, token=token)
        batch_no += 1
        if r.get("code") != 0:
            print("insert batch", batch_no, "failed:", json.dumps(r, ensure_ascii=False)[:300])
            sys.exit(1)
        idx += len(chunk)
        print("batch", batch_no, "inserted", len(chunk), "blocks (total", idx, ")")

    print("DOC_URL=https://www.feishu.cn/docx/" + doc_id)


if __name__ == "__main__":
    main()
