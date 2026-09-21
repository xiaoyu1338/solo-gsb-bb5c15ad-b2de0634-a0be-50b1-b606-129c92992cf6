"""SQLite 存储层。

四张表：

* ``messages``：一封邮件一行，保存指纹、原始字节哈希、规范化 JSON、
  根节点信息与时间戳；
* ``nodes``：MIME 树节点，按 :attr:`MIMENode.path` 物化路径存储，
  可直接按 ``message_id + path`` 查询任意节点；
* ``attachments``：内容寻址附件库——非文本叶子的解码后字节按
  SHA-256 去重，一份内容只存一行；
* ``attachment_refs``：附件实体的每次出现（引用），记录消息 id、
  节点路径、解码文件名与 disposition。

写入在 *单个事务* 中完成：解析失败时根本不会进入此模块；写入中途
出错会整体回滚，数据库里不会留下半成品。建表语句全部幂等
（``IF NOT EXISTS``），已有数据库文件启动时自动补齐新表。
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

CREATE TABLE IF NOT EXISTS attachments (
    sha256          TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL,
    media_type      TEXT NOT NULL,
    content         BLOB NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachment_refs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT NOT NULL
                        REFERENCES attachments(sha256) ON DELETE CASCADE,
    message_id      INTEGER NOT NULL
                        REFERENCES messages(id) ON DELETE CASCADE,
    node_path       TEXT NOT NULL,
    filename        TEXT,
    disposition     TEXT,
    content_type    TEXT NOT NULL,
    UNIQUE(sha256, message_id, node_path)
);

CREATE INDEX IF NOT EXISTS idx_attachment_refs_sha ON attachment_refs(sha256);
CREATE INDEX IF NOT EXISTS idx_attachment_refs_message
    ON attachment_refs(message_id);
CREATE INDEX IF NOT EXISTS idx_attachment_refs_filename
    ON attachment_refs(filename);
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
            if node.is_leaf and not node.is_text:
                _save_attachment(conn, message_id, node)
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


def _save_attachment(
    conn: sqlite3.Connection, message_id: int, node: MIMENode
) -> None:
    """把非文本叶子的解码字节写入内容寻址附件库（与消息同事务）。

    实体按解码内容的 SHA-256 去重（``INSERT OR IGNORE``），每次出现
    追加一条引用：消息 id、节点路径、解码文件名与 disposition。
    重复归档同一封邮件在指纹层面已幂等返回，不会走到这里，因此
    实体与引用都不会因重复提交而增加。
    """

    blob = node.blob or b""
    digest = hashlib.sha256(blob).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO attachments
            (sha256, size_bytes, media_type, content, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            digest,
            len(blob),
            node.media_type,
            blob,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    # 解码文件名：优先 Content-Disposition 的 filename 参数，
    # 回退到 Content-Type 的 name 参数（均为解析期已完成
    # RFC 2047/2231 解码的值）。
    filename = node.disposition_params.get("filename")
    if filename is None:
        filename = node.content_type_params.get("name")
    conn.execute(
        """
        INSERT OR IGNORE INTO attachment_refs
            (sha256, message_id, node_path, filename, disposition,
             content_type)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            digest,
            message_id,
            node.path,
            filename,
            node.disposition,
            node.media_type,
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


# ---------------------------------------------------------------------------
# 附件库查询
# ---------------------------------------------------------------------------


def _attachment_where(
    sha256: Optional[str],
    filename: Optional[str],
    media_type: Optional[str],
) -> tuple[str, list[str]]:
    """组装附件检索的 WHERE 子句。

    * ``sha256``：实体哈希精确匹配（大小写不敏感，库中一律小写）；
    * ``filename`` / ``media_type``：按 *任一出现位置* 的解码文件名 /
      媒体类型匹配（同一内容在不同消息里可能声明不同类型）。
    """

    clauses: list[str] = []
    params: list[str] = []
    if sha256:
        clauses.append("a.sha256 = ?")
        params.append(sha256.lower())
    if filename is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM attachment_refs r"
            " WHERE r.sha256 = a.sha256 AND r.filename = ?)"
        )
        params.append(filename)
    if media_type:
        clauses.append(
            "EXISTS (SELECT 1 FROM attachment_refs r"
            " WHERE r.sha256 = a.sha256 AND r.content_type = ?)"
        )
        params.append(media_type.lower())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def list_attachments(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    sha256: Optional[str] = None,
    filename: Optional[str] = None,
    media_type: Optional[str] = None,
) -> list[sqlite3.Row]:
    where, params = _attachment_where(sha256, filename, media_type)
    return list(
        conn.execute(
            f"""
            SELECT a.sha256, a.size_bytes, a.media_type, a.created_at,
                   (SELECT COUNT(*) FROM attachment_refs r
                     WHERE r.sha256 = a.sha256) AS ref_count
            FROM attachments a{where}
            ORDER BY a.created_at DESC, a.sha256
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    )


def count_attachments(
    conn: sqlite3.Connection,
    *,
    sha256: Optional[str] = None,
    filename: Optional[str] = None,
    media_type: Optional[str] = None,
) -> int:
    where, params = _attachment_where(sha256, filename, media_type)
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM attachments a{where}", params
    ).fetchone()
    return int(row["c"])


def get_attachment(
    conn: sqlite3.Connection, sha256: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT sha256, size_bytes, media_type, content, created_at
        FROM attachments WHERE sha256 = ?
        """,
        (sha256.lower(),),
    ).fetchone()


def list_attachment_refs(
    conn: sqlite3.Connection, sha256: str
) -> list[sqlite3.Row]:
    """某附件实体的全部出现位置，按归档顺序（refs.id）返回。"""

    return list(
        conn.execute(
            """
            SELECT message_id, node_path, filename, disposition,
                   content_type
            FROM attachment_refs WHERE sha256 = ? ORDER BY id
            """,
            (sha256,),
        ).fetchall()
    )
