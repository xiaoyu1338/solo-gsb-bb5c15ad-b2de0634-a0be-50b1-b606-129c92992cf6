"""SQLite 存储层。

两张表：

* ``messages``：一封邮件一行，保存指纹、原始字节哈希、规范化 JSON、
  根节点信息与时间戳；
* ``nodes``：MIME 树节点，按 :attr:`MIMENode.path` 物化路径存储，
  可直接按 ``message_id + path`` 查询任意节点。

写入在 *单个事务* 中完成：解析失败时根本不会进入此模块；写入中途
出错会整体回滚，数据库里不会留下半成品。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from .mimeparser import MIMENode

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,
    raw_sha256      TEXT NOT NULL,
    canonical_json  TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL
                        REFERENCES messages(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    depth           INTEGER NOT NULL,
    position        INTEGER NOT NULL,
    content_type    TEXT NOT NULL,
    kind            TEXT NOT NULL,
    cte             TEXT NOT NULL,
    disposition     TEXT,
    headers_json    TEXT NOT NULL,
    children_json   TEXT NOT NULL,
    text_content    TEXT,
    blob_sha256     TEXT,
    blob_length     INTEGER,
    UNIQUE(message_id, path)
);

CREATE INDEX IF NOT EXISTS idx_nodes_message ON nodes(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_fingerprint ON messages(fingerprint);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False：同步依赖在工作线程建连、async 端点在事件
    # 循环线程使用；每个请求独占一个连接、不会并发，故安全。
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    """启动时调用：建库建表（幂等）。"""

    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        # WAL 让读写并发更顺滑（同一卷不支持时 SQLite 会自动回退）。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def save_message(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    raw_hash: str,
    canonical: bytes,
    size: int,
    root: MIMENode,
) -> tuple[int, bool]:
    """原子保存邮件与整棵 MIME 树。

    相同指纹的邮件重复提交时幂等：直接返回已有 id，不重复写入。
    返回 ``(message_id, created)``。两个相同指纹并发提交时，靠
    ``messages.fingerprint`` 的 UNIQUE 约束兜底，后到者转为幂等返回。
    """

    try:
        return _insert_message(
            conn,
            fingerprint=fingerprint,
            raw_hash=raw_hash,
            canonical=canonical,
            size=size,
            root=root,
        )
    except sqlite3.IntegrityError:
        # 并发竞态：另一个请求已插入相同指纹。回滚本事务后读回。
        conn.rollback()
        row = conn.execute(
            "SELECT id FROM messages WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            raise
        return int(row["id"]), False


def _insert_message(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    raw_hash: str,
    canonical: bytes,
    size: int,
    root: MIMENode,
) -> tuple[int, bool]:
    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM messages WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False

        cur = conn.execute(
            """
            INSERT INTO messages
                (fingerprint, raw_sha256, canonical_json, content_type,
                 size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                raw_hash,
                canonical.decode("utf-8"),
                root.media_type,
                size,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        message_id = int(cur.lastrowid)
        for node, depth, position in _walk(root):
            _insert_node(conn, message_id, node, depth, position)
        return message_id, True


def _walk(root: MIMENode) -> Iterator[tuple[MIMENode, int, int]]:
    stack: list[tuple[MIMENode, int, int]] = [(root, 0, 0)]
    while stack:
        node, depth, position = stack.pop()
        yield node, depth, position
        # 逆序压栈以保持 1..n 的访问顺序
        for i in range(len(node.children) - 1, -1, -1):
            stack.append((node.children[i], depth + 1, i + 1))


def _insert_node(
    conn: sqlite3.Connection,
    message_id: int,
    node: MIMENode,
    depth: int,
    position: int,
) -> None:
    # 头值在节点构建时已完成 RFC 2047 解码并校验成功，直接存储；
    # 头名保留原始大小写。
    headers = [
        [h.raw_name.decode("ascii", "replace"), h.decoded]
        for h in node.headers
    ]
    children = [c.path for c in node.children]

    blob_hash = None
    blob_len = None
    if not node.is_leaf:
        kind = "multipart" if node.type == "multipart" else "message"
    elif node.is_text:
        kind = "text"
    else:
        kind = "blob"
        blob = node.blob or b""
        blob_hash = hashlib.sha256(blob).hexdigest()
        blob_len = len(blob)

    conn.execute(
        """
        INSERT INTO nodes
            (message_id, path, depth, position, content_type, kind, cte,
             disposition, headers_json, children_json, text_content,
             blob_sha256, blob_length)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            node.path,
            depth,
            position,
            node.media_type,
            kind,
            node.cte,
            node.disposition,
            json.dumps(headers, ensure_ascii=False),
            json.dumps(children),
            node.text,
            blob_hash,
            blob_len,
        ),
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_message(conn: sqlite3.Connection, message_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,)
    ).fetchone()


def get_message_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()


def list_messages(
    conn: sqlite3.Connection, *, limit: int, offset: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, fingerprint, raw_sha256, content_type, size_bytes,
                   created_at
            FROM messages ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    )


def count_messages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"])


def get_node(
    conn: sqlite3.Connection, message_id: int, path: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM nodes WHERE message_id = ? AND path = ?",
        (message_id, path),
    ).fetchone()


def list_nodes(conn: sqlite3.Connection, message_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM nodes WHERE message_id = ? ORDER BY id",
            (message_id,),
        ).fetchall()
    )
