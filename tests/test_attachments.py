"""内容寻址附件库测试：去重、引用、检索、Range 下载。"""

import base64
import hashlib
import sqlite3

from fastapi.testclient import TestClient

from conftest import make_multipart, text_part

BLOB = bytes(range(256)) * 4  # 1024 字节确定性二进制内容
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()


def blob_part(
    data: bytes,
    *,
    media_type: str = "application/pdf",
    filename: str | None = "report.pdf",
    disposition: str | None = "attachment",
    name: str | None = None,
    cte: str = "base64",
) -> bytes:
    ct = f"Content-Type: {media_type}"
    if name is not None:
        ct += f'; name="{name}"'
    head = ct.encode() + b"\r\n"
    head += f"Content-Transfer-Encoding: {cte}\r\n".encode()
    if disposition is not None:
        disp = f"Content-Disposition: {disposition}"
        if filename is not None:
            disp += f'; filename="{filename}"'
        head += disp.encode() + b"\r\n"
    head += b"\r\n"
    if cte == "base64":
        body = base64.b64encode(data)
    else:
        body = data
    return head + body


def blob_message(
    data: bytes = BLOB,
    *,
    subject: str = "with attachment",
    **part_kwargs,
) -> bytes:
    return (
        b"From: alice@example.com\r\n"
        + f"Subject: {subject}\r\n".encode()
        + b"Content-Type: multipart/mixed; boundary=MIX\r\n\r\n"
        + make_multipart([text_part("see attached"), blob_part(data, **part_kwargs)], "MIX")
    )


def test_blob_leaf_creates_attachment(client):
    mid = client.post("/messages", content=blob_message()).json()["id"]

    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["sha256"] == BLOB_SHA
    assert item["size_bytes"] == len(BLOB)
    assert item["media_type"] == "application/pdf"
    assert item["ref_count"] == 1
    assert item["occurrences"] == [
        {
            "message_id": mid,
            "node_path": "2",
            "filename": "report.pdf",
            "disposition": "attachment",
            "content_type": "application/pdf",
        }
    ]


def test_text_leaf_creates_no_attachment(client):
    client.post("/messages", content=b"Subject: plain\r\n\r\nhello\r\n")
    assert client.get("/attachments").json()["total"] == 0


def test_root_level_blob_recorded_with_empty_path(client):
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(BLOB)
        + b"\r\n"
    )
    mid = client.post("/messages", content=msg).json()["id"]
    item = client.get("/attachments").json()["items"][0]
    assert item["occurrences"][0]["message_id"] == mid
    assert item["occurrences"][0]["node_path"] == ""
    assert item["occurrences"][0]["filename"] is None
    assert item["occurrences"][0]["disposition"] is None


def test_duplicate_archive_adds_no_entities_or_refs(client):
    msg = blob_message()
    r1 = client.post("/messages", content=msg).json()
    r2 = client.post("/messages", content=msg).json()
    assert r1["created"] is True and r2["created"] is False

    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    assert listing["items"][0]["ref_count"] == 1
    assert len(listing["items"][0]["occurrences"]) == 1


def test_same_blob_across_messages_deduped(client):
    m1 = client.post("/messages", content=blob_message(subject="one")).json()
    m2 = client.post("/messages", content=blob_message(subject="two")).json()
    assert m1["id"] != m2["id"]

    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["ref_count"] == 2
    assert {o["message_id"] for o in item["occurrences"]} == {m1["id"], m2["id"]}
    assert all(o["node_path"] == "2" for o in item["occurrences"])


def test_same_blob_twice_in_one_message(client):
    part = blob_part(BLOB)
    msg = (
        b"Subject: dup parts\r\n"
        b"Content-Type: multipart/mixed; boundary=D\r\n\r\n"
        + make_multipart([part, part], "D")
    )
    mid = client.post("/messages", content=msg).json()["id"]

    item = client.get("/attachments").json()["items"][0]
    assert item["ref_count"] == 2
    assert [o["node_path"] for o in item["occurrences"]] == ["1", "2"]
    assert all(o["message_id"] == mid for o in item["occurrences"])


def test_same_blob_different_cte_deduped(client):
    # 同一解码内容，一次 base64、一次 quoted-printable：附件实体只有一份。
    raw = b"AB\x00\xffCD"
    qp_body = b"AB=00=FFCD"
    m1 = (
        b"Subject: b64\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(raw)
        + b"\r\n"
    )
    m2 = (
        b"Subject: qp\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n" + qp_body
    )
    client.post("/messages", content=m1)
    client.post("/messages", content=m2)

    listing = client.get("/attachments").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert item["ref_count"] == 2


def test_filter_by_hash(client):
    other = blob_message(b"different content", filename="other.bin")
    client.post("/messages", content=blob_message())
    client.post("/messages", content=other)

    r = client.get(f"/attachments?hash={BLOB_SHA}").json()
    assert r["total"] == 1
    assert r["items"][0]["sha256"] == BLOB_SHA
    # 大写哈希同样匹配
    r = client.get(f"/attachments?hash={BLOB_SHA.upper()}").json()
    assert r["total"] == 1
    # 不存在的哈希
    assert client.get(f"/attachments?hash={'0' * 64}").json()["total"] == 0


def test_filter_by_filename(client):
    client.post("/messages", content=blob_message())
    client.post(
        "/messages",
        content=blob_message(b"xyz", subject="other", filename="notes.dat"),
    )

    r = client.get("/attachments?filename=notes.dat").json()
    assert r["total"] == 1
    assert r["items"][0]["occurrences"][0]["filename"] == "notes.dat"
    assert client.get("/attachments?filename=missing.txt").json()["total"] == 0


def test_filter_by_media_type_matches_any_occurrence(client):
    # 同一内容在两封邮件中声明了不同媒体类型：实体媒体类型取首次见到的，
    # 但过滤应匹配任一出现位置。
    client.post("/messages", content=blob_message(subject="pdf"))
    client.post(
        "/messages",
        content=blob_message(
            subject="png", media_type="image/png", filename="r.png"
        ),
    )

    assert client.get("/attachments").json()["total"] == 1
    by_pdf = client.get("/attachments?media_type=application/pdf").json()
    by_png = client.get("/attachments?media_type=image/png").json()
    assert by_pdf["total"] == 1
    assert by_png["total"] == 1
    assert client.get("/attachments?media_type=text/html").json()["total"] == 0


def test_pagination(client):
    for i in range(3):
        client.post(
            "/messages",
            content=blob_message(
                f"blob-{i}".encode(), subject=f"m{i}", filename=f"f{i}.bin"
            ),
        )
    page1 = client.get("/attachments?limit=2&offset=0").json()
    page2 = client.get("/attachments?limit=2&offset=2").json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    # 分页覆盖全部实体且不重复
    seen = {i["sha256"] for i in page1["items"] + page2["items"]}
    assert len(seen) == 3


def test_download_full_content(client):
    client.post("/messages", content=blob_message())
    r = client.get(f"/attachments/{BLOB_SHA}/content")
    assert r.status_code == 200
    assert r.content == BLOB
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == str(len(BLOB))
    assert "report.pdf" in r.headers["content-disposition"]
    # 哈希大小写不敏感
    assert client.get(f"/attachments/{BLOB_SHA.upper()}/content").status_code == 200


def test_download_range(client):
    client.post("/messages", content=blob_message())
    url = f"/attachments/{BLOB_SHA}/content"
    size = len(BLOB)

    r = client.get(url, headers={"Range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.content == BLOB[0:4]
    assert r.headers["content-range"] == f"bytes 0-3/{size}"

    r = client.get(url, headers={"Range": f"bytes={size - 10}-"})
    assert r.status_code == 206
    assert r.content == BLOB[size - 10:]
    assert r.headers["content-range"] == f"bytes {size - 10}-{size - 1}/{size}"

    r = client.get(url, headers={"Range": "bytes=-16"})
    assert r.status_code == 206
    assert r.content == BLOB[-16:]
    assert r.headers["content-range"] == f"bytes {size - 16}-{size - 1}/{size}"

    # end 超出末尾时截断到最后一个字节
    r = client.get(url, headers={"Range": f"bytes={size - 2}-{size + 100}"})
    assert r.status_code == 206
    assert r.content == BLOB[size - 2:]
    assert r.headers["content-range"] == f"bytes {size - 2}-{size - 1}/{size}"

    # 后缀长度超过整个文件：返回全部内容
    r = client.get(url, headers={"Range": f"bytes=-{size + 500}"})
    assert r.status_code == 206
    assert r.content == BLOB
    assert r.headers["content-range"] == f"bytes 0-{size - 1}/{size}"


def test_range_unsatisfiable_returns_416(client):
    client.post("/messages", content=blob_message())
    url = f"/attachments/{BLOB_SHA}/content"
    size = len(BLOB)

    for bad in (
        f"bytes={size}-",          # 起点越界
        f"bytes={size + 1}-{size + 9}",
        "bytes=10-2",              # 起点在终点之后
        "bytes=0-1,4-8",           # 多段不支持
        "bytes=-0",                # 后缀长度为 0
        "bytes=abc-def",           # 无法解析
        "bytes=5",                 # 缺少 '-'
    ):
        r = client.get(url, headers={"Range": bad})
        assert r.status_code == 416, bad
        assert r.headers["content-range"] == f"bytes */{size}"


def test_non_bytes_range_unit_ignored(client):
    client.post("/messages", content=blob_message())
    r = client.get(
        f"/attachments/{BLOB_SHA}/content",
        headers={"Range": "items=0-2"},
    )
    assert r.status_code == 200
    assert r.content == BLOB


def test_download_unknown_hash_404(client):
    r = client.get(f"/attachments/{'f' * 64}/content")
    assert r.status_code == 404


def test_failed_parse_creates_no_attachment(client):
    client.post("/messages", content=blob_message())
    before = client.get("/attachments").json()
    assert before["total"] == 1

    bad = (
        b"Content-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n@@@not-base64@@@\r\n"
    )
    assert client.post("/messages", content=bad).status_code == 422

    after = client.get("/attachments").json()
    assert after["total"] == 1
    assert after["items"][0]["ref_count"] == 1


def test_filename_from_content_type_name_param(client):
    # 没有 Content-Disposition 时回退到 Content-Type 的 name 参数。
    msg = (
        b"Subject: name param\r\n"
        b"Content-Type: multipart/mixed; boundary=N\r\n\r\n"
        + make_multipart(
            [blob_part(BLOB, disposition=None, filename=None, name="a.bin")],
            "N",
        )
    )
    client.post("/messages", content=msg)
    occ = client.get("/attachments").json()["items"][0]["occurrences"][0]
    assert occ["filename"] == "a.bin"
    assert occ["disposition"] is None


def test_rfc2231_filename_stored_decoded(client):
    part = (
        b"Content-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment;"
        b" filename*=utf-8''%E6%8A%A5%E5%91%8A.pdf\r\n\r\n"
        + base64.b64encode(BLOB)
    )
    msg = (
        b"Subject: rfc2231\r\n"
        b"Content-Type: multipart/mixed; boundary=R\r\n\r\n"
        + make_multipart([part], "R")
    )
    client.post("/messages", content=msg)
    occ = client.get("/attachments").json()["items"][0]["occurrences"][0]
    assert occ["filename"] == "报告.pdf"
    assert client.get("/attachments?filename=报告.pdf").json()["total"] == 1


def test_empty_blob_attachment(client):
    # 0 字节附件：可检索、可下载（空内容），任何 Range 都不可满足。
    msg = (
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
    )
    client.post("/messages", content=msg)
    empty_sha = hashlib.sha256(b"").hexdigest()

    item = client.get("/attachments").json()["items"][0]
    assert item["sha256"] == empty_sha
    assert item["size_bytes"] == 0

    r = client.get(f"/attachments/{empty_sha}/content")
    assert r.status_code == 200
    assert r.content == b""

    r = client.get(
        f"/attachments/{empty_sha}/content", headers={"Range": "bytes=0-0"}
    )
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */0"


def test_existing_db_gains_attachment_tables(tmp_path, monkeypatch):
    # 模拟旧版数据库（只有 messages / nodes）：启动后新表自动补齐，
    # 归档与附件检索照常工作。
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            raw_sha256 TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL
                REFERENCES messages(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            depth INTEGER NOT NULL,
            position INTEGER NOT NULL,
            content_type TEXT NOT NULL,
            kind TEXT NOT NULL,
            cte TEXT NOT NULL,
            disposition TEXT,
            headers_json TEXT NOT NULL,
            children_json TEXT NOT NULL,
            text_content TEXT,
            blob_sha256 TEXT,
            blob_length INTEGER,
            UNIQUE(message_id, path)
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("MIME_DB_PATH", db_path)
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/messages", content=blob_message())
        assert r.status_code == 201
        listing = client.get("/attachments").json()
        assert listing["total"] == 1
        assert listing["items"][0]["sha256"] == BLOB_SHA
        dl = client.get(f"/attachments/{BLOB_SHA}/content")
        assert dl.status_code == 200
        assert dl.content == BLOB
