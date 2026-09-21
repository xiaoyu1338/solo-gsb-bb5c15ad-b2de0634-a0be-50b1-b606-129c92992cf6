"""FastAPI 应用入口。

启动时初始化 SQLite（``MIME_DB_PATH`` 指定路径，默认
``/data/mime.db``），监听 8000 端口（见 Dockerfile / compose）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response

from . import database
from .errors import MIMEParseError
from .mimeparser import fingerprint, parse_message, raw_sha256
from .schemas import (
    AttachmentListResponse,
    AttachmentOccurrence,
    AttachmentSummary,
    MessageCreated,
    MessageListResponse,
    MessageSummary,
    NodeDetail,
    NodeDetailResponse,
    NodeListResponse,
    NodeSummary,
    TreeNode,
    TreeResponse,
)

DEFAULT_DB_PATH = "/data/mime.db"


def _resolve_db_path() -> str:
    """运行时读取，便于环境变量在启动/测试时覆盖。"""

    return os.environ.get("MIME_DB_PATH", DEFAULT_DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库（幂等）。
    db_path = _resolve_db_path()
    database.init_db(db_path)
    app.state.db_path = db_path
    yield


app = FastAPI(
    title="MIME 邮件归档指纹 API",
    description=(
        "接收原始 RFC 5322 邮件，递归解析 MIME 树，按规范化头字段与"
        "叶子内容生成稳定指纹并入库。相同内容不同换行/头字段大小写"
        "得到同一指纹；损坏输入返回定位到节点的 422 错误且不写入。"
        "非文本叶子的解码字节按 SHA-256 去重进入内容寻址附件库，"
        "支持分页检索与带 Range 的下载。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_conn(request: Request) -> sqlite3.Connection:
    conn = database.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(MIMEParseError)
async def mime_parse_error_handler(request: Request, exc: MIMEParseError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=422, content=exc.to_dict())


# ---------------------------------------------------------------------------
# 节点序列化
# ---------------------------------------------------------------------------


def _node_summary(row: sqlite3.Row) -> NodeSummary:
    return NodeSummary(
        path=row["path"],
        depth=row["depth"],
        position=row["position"],
        content_type=row["content_type"],
        kind=row["kind"],
        cte=row["cte"],
        disposition=row["disposition"],
        children=json.loads(row["children_json"]),
    )


def _message_404(message_id: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"邮件 {message_id} 不存在")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@app.get("/health")
def health(request: Request) -> dict:
    return {"status": "ok", "database": request.app.state.db_path}


@app.post("/messages", status_code=201, response_model=MessageCreated)
async def create_message(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> MessageCreated:
    """归档一封原始邮件。

    请求体即邮件原始字节（建议 ``Content-Type: message/rfc822``，
    但服务端只关心字节，不依赖该头）。解析或解码失败时返回 422，
    响应中的 ``node_path`` 标出出错节点。
    """

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="请求体为空：需要原始 RFC 5322 邮件")

    # 先完整解析+指纹；任何错误都在 save_message 之前抛出，
    # 数据库不会被触碰（“不写入半成品”）。
    tree = parse_message(data)
    fp, canonical = fingerprint(tree)

    message_id, created = database.save_message(
        conn,
        fingerprint=fp,
        raw_hash=raw_sha256(data),
        canonical=canonical,
        size=len(data),
        root=tree,
    )
    row = database.get_message(conn, message_id)
    node_count = len(database.list_nodes(conn, message_id))
    assert row is not None
    return MessageCreated(
        id=message_id,
        fingerprint=row["fingerprint"],
        raw_sha256=row["raw_sha256"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        created=created,
        node_count=node_count,
    )


@app.get("/messages", response_model=MessageListResponse)
def list_messages(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> MessageListResponse:
    rows = database.list_messages(conn, limit=limit, offset=offset)
    return MessageListResponse(
        items=[
            MessageSummary(
                id=r["id"],
                fingerprint=r["fingerprint"],
                raw_sha256=r["raw_sha256"],
                content_type=r["content_type"],
                size_bytes=r["size_bytes"],
                created_at=r["created_at"],
            )
            for r in rows
        ],
        total=database.count_messages(conn),
        limit=limit,
        offset=offset,
    )


@app.get("/messages/{message_id}", response_model=MessageSummary)
def get_message(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> MessageSummary:
    row = database.get_message(conn, message_id)
    if row is None:
        raise _message_404(message_id)
    return MessageSummary(
        id=row["id"],
        fingerprint=row["fingerprint"],
        raw_sha256=row["raw_sha256"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
    )


@app.get("/messages/{message_id}/tree", response_model=TreeResponse)
def get_tree(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> TreeResponse:
    """返回嵌套 MIME 树（children 内嵌完整子节点）。"""

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)
    root_row = next(r for r in rows if r["path"] == "")
    root = _build_nested(root_row, rows)
    return TreeResponse(id=message_id, fingerprint=msg["fingerprint"], tree=root)


def _build_nested(row: sqlite3.Row, rows: list[sqlite3.Row]) -> TreeNode:
    by_path = {r["path"]: r for r in rows}
    children_paths = json.loads(row["children_json"])
    return TreeNode(
        path=row["path"],
        depth=row["depth"],
        position=row["position"],
        content_type=row["content_type"],
        kind=row["kind"],
        cte=row["cte"],
        disposition=row["disposition"],
        children=[
            _build_nested(by_path[p], rows) for p in children_paths
        ],
    )


@app.get("/messages/{message_id}/nodes", response_model=NodeListResponse)
def list_nodes(
    message_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> NodeListResponse:
    """扁平返回全部节点（children 为子节点路径）。"""

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    rows = database.list_nodes(conn, message_id)
    return NodeListResponse(
        id=message_id,
        fingerprint=msg["fingerprint"],
        nodes=[_node_summary(r) for r in rows],
    )


@app.get(
    "/messages/{message_id}/nodes/{node_path:path}",
    response_model=NodeDetailResponse,
)
def get_node(
    message_id: int,
    node_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> NodeDetailResponse:
    """查询单个节点详情（含头字段与叶子内容）。

    根节点路径为空，使用 ``/messages/{id}/nodes/``（末尾斜杠）。
    """

    msg = database.get_message(conn, message_id)
    if msg is None:
        raise _message_404(message_id)
    row = database.get_node(conn, message_id, node_path)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"节点 {node_path!r} 不存在",
        )
    detail = NodeDetail(
        **_node_summary(row).model_dump(),
        headers=json.loads(row["headers_json"]),
        text=row["text_content"],
        blob_sha256=row["blob_sha256"],
        blob_length=row["blob_length"],
    )
    return NodeDetailResponse(
        id=message_id, fingerprint=msg["fingerprint"], node=detail
    )


# ---------------------------------------------------------------------------
# 附件库
# ---------------------------------------------------------------------------


@app.get("/attachments", response_model=AttachmentListResponse)
def list_attachments(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sha256: Optional[str] = Query(
        None,
        pattern=r"^[0-9a-fA-F]{1,64}$",
        description="按哈希前缀过滤（传完整 64 位即精确查找）",
    ),
    filename: Optional[str] = Query(
        None, max_length=200, description="按解码文件名子串过滤"
    ),
    media_type: Optional[str] = Query(
        None, max_length=100, description="按出现位置的媒体类型精确过滤"
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> AttachmentListResponse:
    """分页列出附件实体，每项含引用次数与全部出现位置。"""

    rows = database.list_attachments(
        conn,
        limit=limit,
        offset=offset,
        sha256=sha256,
        filename=filename,
        media_type=media_type,
    )
    items = [
        AttachmentSummary(
            sha256=r["sha256"],
            size_bytes=r["size_bytes"],
            media_type=r["media_type"],
            reference_count=r["reference_count"],
            created_at=r["created_at"],
            occurrences=[
                AttachmentOccurrence(**dict(ref))
                for ref in database.list_attachment_refs(conn, r["sha256"])
            ],
        )
        for r in rows
    ]
    return AttachmentListResponse(
        items=items,
        total=database.count_attachments(
            conn, sha256=sha256, filename=filename, media_type=media_type
        ),
        limit=limit,
        offset=offset,
    )


def _parse_single_range(header: str, size: int) -> Optional[tuple[int, int]]:
    """解析单段 ``bytes`` Range，返回闭区间 ``(start, end)``。

    多段、语法非法、越界等一律返回 ``None``（调用方据此回 416）。
    合法的开口/后缀/超界末端按 RFC 7233 收敛到实际范围。
    """

    unit, sep, spec = header.partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None
    ranges = [r.strip() for r in spec.split(",")]
    if len(ranges) != 1 or not ranges[0]:
        return None
    start_s, dash, end_s = ranges[0].partition("-")
    if not dash:
        return None
    if not start_s:
        # 后缀范围：最后 N 字节
        if not end_s.isdigit():
            return None
        suffix = int(end_s)
        if suffix <= 0:
            return None
        start = max(size - suffix, 0)
        end = size - 1
    else:
        if not start_s.isdigit() or (end_s and not end_s.isdigit()):
            return None
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
        if start > end:
            return None
        end = min(end, size - 1)
    if start >= size:  # 越界（含空附件）
        return None
    return start, end


@app.get("/attachments/{sha256}/content")
def get_attachment_content(
    sha256: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Response:
    """下载附件的原始解码字节。

    支持单段 ``bytes`` Range：合法范围返回 206 与 ``Content-Range``；
    越界、多段或非法范围返回 416（带 ``Content-Range: bytes */N``）。
    """

    row = database.get_attachment(conn, sha256.lower())
    if row is None:
        raise HTTPException(status_code=404, detail=f"附件 {sha256} 不存在")
    content: bytes = row["content"]
    size: int = row["size_bytes"]
    media_type: str = row["media_type"]

    range_header = request.headers.get("range")
    if range_header is None:
        return Response(
            content=content,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes"},
        )

    parsed = _parse_single_range(range_header, size)
    if parsed is None:
        raise HTTPException(
            status_code=416,
            detail=(
                f"Range 不可满足：{range_header!r}"
                f"（资源大小 {size} 字节，仅支持单段 bytes 范围）"
            ),
            headers={"Content-Range": f"bytes */{size}"},
        )
    start, end = parsed
    return Response(
        content=content[start:end + 1],
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
        },
    )
