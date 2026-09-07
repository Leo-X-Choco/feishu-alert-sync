# -*- coding: utf-8 -*-
"""飞书日报文档操作：定位日期节的工作表格，覆盖写入成员单元格。

通过 docx 原生 Block API 实现（替代本地版基于 lark-cli XML 的解析）：
  1. 拉取全量 blocks，按 block_type 识别 标题1(3)/表格(31)/单元格(32)/有序列表(13)
  2. 定位日期标题（如 2026.9.7）所在 h1 节内的「今日工作情况」表格
  3. 按行取姓名（mention user_id 优先，文本兜底），定位目标单元格
  4. 写入 = 删除单元格原 children + 新建有序列表块
"""

from . import config, feishu_api

# docx block_type 常量
BT_PAGE, BT_TEXT, BT_H1 = 1, 2, 3
BT_ORDERED, BT_TABLE, BT_CELL = 13, 31, 32


def _elements_text(elements: list) -> str:
    out = []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        data = el.get(el.get("type")) or {}
        out.append(data.get("content") or data.get("text") or "")
    return "".join(out)


def _block_text(block: dict) -> str:
    """提取块可见文本（text/heading/ordered 均适用）。"""
    for key in ("text", "heading1", "heading2", "heading3",
                "heading4", "heading5", "ordered", "bullet"):
        if key in block:
            return _elements_text(block[key].get("elements"))
    return ""


def get_all_blocks(doc_id: str) -> dict[str, dict]:
    """分页拉取文档全部 blocks，返回 {block_id: block}。"""
    blocks, page_token = {}, ""
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        d = feishu_api.api("GET", f"/open-apis/docx/v1/documents/{doc_id}/blocks",
                           params=params)
        for b in d.get("items") or []:
            blocks[b["block_id"]] = b
        page_token = d.get("page_token") or ""
        if not d.get("has_more") or not page_token:
            return blocks


def find_work_table(doc_id: str, iso_date: str, blocks: dict | None = None,
                    dry_run: bool = False) -> dict | None:
    """定位日期节的「今日工作情况」表格，返回
    {table_block, rows: [{name, open_id, cell_id, children}]}。"""
    blocks = blocks or get_all_blocks(doc_id)
    title = config.iso_to_daily_title(iso_date)

    # 1. 定位日期 h1 及其后兄弟块（到下一个 h1 为止）
    root = next((b for b in blocks.values() if b.get("block_type") == BT_PAGE), None)
    if not root:
        raise RuntimeError("文档无 page 根块")
    siblings = root.get("children") or []
    try:
        h_idx = next(i for i, cid in enumerate(siblings)
                     if blocks.get(cid, {}).get("block_type") == BT_H1
                     and (_block_text(blocks[cid]).startswith(title)
                          or _block_text(blocks[cid]).split("（")[0].split("(")[0].strip() == title))
    except StopIteration:
        return None
    scope_ids: list[str] = []
    for cid in siblings[h_idx + 1:]:
        if blocks.get(cid, {}).get("block_type") == BT_H1:
            break
        scope_ids.append(cid)

    # 2. 在节内（含嵌套）找目标表格
    target = None
    stack = list(scope_ids)
    while stack and target is None:
        bid = stack.pop(0)
        b = blocks.get(bid)
        if not b:
            continue
        if b.get("block_type") == BT_TABLE:
            prop = (b.get("table") or {}).get("property") or {}
            cells = b.get("children") or b.get("table", {}).get("cells") or []
            cols = prop.get("column_size") or 0
            if not cols or len(cells) < cols * 2:
                continue
            header = [_block_text(blocks.get(cid, {})) for cid in cells[:cols]]
            if config.WORK_COLUMN in header and config.NAME_COLUMN in header:
                target = (b, cells, cols, header)
        stack.extend(b.get("children") or [])
    if not target:
        return None

    table, cells, cols, header = target
    name_col = header.index(config.NAME_COLUMN)
    work_col = header.index(config.WORK_COLUMN)
    id_by_name = {v: k for k, v in config.OPEN_IDS.items()}

    rows = []
    for r in range(1, (len(cells)) // cols):
        name_cell = blocks.get(cells[r * cols + name_col], {})
        work_cell = blocks.get(cells[r * cols + work_col], {})
        # 姓名解析：mention user_id 优先
        name, open_id = "", ""
        for cid in name_cell.get("children") or []:
            cb = blocks.get(cid, {})
            for key in ("text", "heading1", "ordered", "bullet"):
                for el in (cb.get(key) or {}).get("elements") or []:
                    if el.get("type") == "mention_user":
                        uid = (el.get("mention_user") or {}).get("user_id") or ""
                        if uid in id_by_name:
                            name, open_id = id_by_name[uid], uid
            if not name:
                name = _block_text(cb).strip() or name
        name = config.NAME_ALIASES.get(name, name)
        rows.append({"name": name, "open_id": open_id,
                     "cell_id": work_cell.get("block_id"),
                     "children": work_cell.get("children") or []})
    return {"table_id": table.get("block_id"), "rows": rows}


def write_cell_items(doc_id: str, cell_id: str, children: list[str],
                     items: list[str]) -> None:
    """将 items 覆盖写入单元格（先删原有内容块，再新建有序列表）。"""
    if children:
        feishu_api.api("DELETE",
                       f"/open-apis/docx/v1/documents/{doc_id}/blocks/{cell_id}/children/batch_delete",
                       body={"start_index": 0, "end_index": len(children),
                             "document_revision_id": -1})
    if not items:
        return
    payload = {"index": 0, "document_revision_id": -1, "children": [
        {"block_type": BT_ORDERED,
         "ordered": {"elements": [{"type": "text_run",
                                   "text_run": {"content": it}}]}}
        for it in items]}
    feishu_api.api("POST",
                   f"/open-apis/docx/v1/documents/{doc_id}/blocks/{cell_id}/children",
                   body=payload)


def split_items(text: str) -> list[str]:
    """小结文本 → 有序条目（与本地版一致：按行拆分、去编号）。"""
    import re
    items = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:\d+|[一二三四五六七八九十]+)\s*[、.．:：)）]\s*", "", line)
        line = line.strip()
        if line:
            items.append(line)
    return items
