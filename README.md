# MIME 邮件归档指纹 API

接收**原始 RFC 5322 邮件**字节，递归解析 MIME 树（multipart 边界、
Content-Transfer-Encoding、字符集、RFC 2047 编码字、RFC 2231 参数），
按**规范化头字段**与**解码后的叶子内容**生成稳定 SHA-256 指纹，
并把可查询的 MIME 树存入 SQLite。

- 同一封邮件的不同换行风格（CRLF / LF / 裸 CR）、不同头字段大小写、
  不同 multipart `boundary`、不同 Content-Transfer-Encoding
  （8bit / base64 / quoted-printable）或等价字符集标注，
  **得到完全相同的指纹**；
- 损坏的边界、重复参数、无法解码的字符集 / base64 / quoted-printable
  等情况返回 **HTTP 422**，错误体带 **`node_path` 精确定位到 MIME 节点**，
  且**不会写入任何半成品数据**；
- 相同指纹重复提交是幂等的（返回同一 id，`created=false`）。

技术栈：FastAPI + SQLite（标准库 `sqlite3`，无 ORM），无第三方 MIME
解析库——解析器按 RFC 严格手工实现，刻意不使用容错的 Python `email` 包。

## 目录结构

```
app/
  main.py        FastAPI 路由与启动初始化
  mimeparser.py  严格 MIME/RFC 5322 解析、传输解码、规范化与指纹
  database.py    SQLite 建表、事务化写入、消息/节点查询
  errors.py      定位到节点的解析错误
  schemas.py     响应模型
tests/           86 个 pytest 用例（指纹稳定性、损坏输入、HTTP 端到端）
Dockerfile
docker-compose.yml
requirements.txt
```

## 快速开始（Docker）

```bash
docker compose up --build
```

服务监听 **8000** 端口，SQLite 数据文件位于容器内 `/data/mime.db`
（compose 中以命名卷 `mime-data` 持久化）。启动时自动建库建表。

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok","database":"/data/mime.db"}
```

## 本地运行（不用 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export MIME_DB_PATH=./data/mime.db   # 可选，默认 /data/mime.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 提交一封邮件

请求体就是邮件原始字节（建议 `Content-Type: message/rfc822`，
但服务端不依赖该头）：

```bash
curl -X POST http://localhost:8000/messages \
     -H 'Content-Type: message/rfc822' \
     --data-binary @message.eml
```

成功响应 `201`：

```json
{
  "id": 1,
  "fingerprint": "58d92498…431",
  "raw_sha256": "97d9604f…6fb",
  "content_type": "multipart/mixed",
  "size_bytes": 380,
  "created": true,
  "node_count": 3
}
```

再次提交语义相同但字节不同（换行、头大小写、boundary、CTE 不同）的邮件，
返回同一 `id` 与 `fingerprint`，`created` 为 `false`。

## 查询

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/messages` | 列表（`limit`、`offset` 分页） |
| GET | `/messages/{id}` | 邮件元信息 |
| GET | `/messages/{id}/tree` | **嵌套 MIME 树** |
| GET | `/messages/{id}/nodes` | 全部节点（扁平，children 为路径） |
| GET | `/messages/{id}/nodes/{path}` | 单个节点详情（头字段、文本/二进制哈希） |

节点路径规则：根节点为 `""`，子节点为 1 基序号 `1`、`2`，
深层为 `1/2` 这样的物化路径。根节点详情用末尾斜杠：

```bash
curl http://localhost:8000/messages/1/tree
curl http://localhost:8000/messages/1/nodes/        # 根节点
curl http://localhost:8000/messages/1/nodes/2       # 第 2 部分
curl http://localhost:8000/messages/1/nodes/2/1     # 第 2 部分里的封装邮件
```

文本叶子的 `text` 是按声明字符集解码、NFC 规范化、换行统一为 `\n`
后的内容；非文本叶子不回传字节，只给 `blob_sha256` 与 `blob_length`。

## 错误响应

所有解析 / 解码失败统一为 `422`：

```json
{
  "error": {
    "code": "UNDECODABLE_CONTENT",
    "message": "base64 内容含非法字符 0x40",
    "node_path": "2",
    "detail": {"offset": 0, "byte": 64}
  }
}
```

`node_path` 指向出问题的 MIME 节点（根节点为 `""`）。解析在写库之前
**完整完成**，因此失败请求不会在 `messages` / `nodes` 表中留下任何行；
写库本身也在单事务内，中途出错整体回滚。

错误码：

| `code` | 触发条件 |
| ---- | ---- |
| `INVALID_MESSAGE` / `INVALID_HEADER` / `INVALID_HEADER_NAME` | 报文/头块结构不合法 |
| `DUPLICATE_HEADER` | Content-Type / CTE / Content-Disposition 重复出现 |
| `INVALID_CONTENT_TYPE` / `INVALID_DISPOSITION` | 媒体类型或 disposition 非法 |
| `INVALID_BOUNDARY` | 缺 boundary、边界非法、正文中找不到边界、缺闭合边界 |
| `DUPLICATE_PARAMETER` | 同名参数重复（含 RFC 2231 续片序号重复、普通/扩展混用） |
| `INVALID_PARAMETER` | RFC 2231 续片序号不连续、扩展值损坏 |
| `INVALID_HEADER_VALUE` | 参数引号未闭合、结构头中出现 encoded-word 等 |
| `UNKNOWN_CHARSET` | 声明的字符集无法识别 |
| `INVALID_CTE` / `UNSUPPORTED_CTE` | 传输编码非法或不支持 |
| `UNDECODABLE_CONTENT` | base64 / quoted-printable / 字符集解码失败，或 7bit 含非 ASCII 字节 |
| `UNDECODABLE_HEADER` | RFC 2047 编码字（B/Q）本身损坏 |

## 指纹规范化规则

指纹输入是一棵 JSON 规范树（键排序、无多余空白、UTF-8、LF），
对其取 SHA-256。规范化时**剔除打包/传输噪音**：

- 头名统一小写；头值完成 RFC 2047 解码、Unicode NFC、线性空白压缩，
  相邻 encoded-word 之间的空白按 RFC 删除；
- 叶子文本按声明字符集解码为 Unicode，再 NFC、换行（CRLF/CR/LF）归一为 `\n`；
- 不纳入指纹：multipart `boundary`、文本节点的 `charset`（已解码）、
  Content-Transfer-Encoding（已解码）、头折叠方式、邮件换行风格；
- 非文本叶子取**解码后字节**的 SHA-256 与长度；
- 节点顺序、媒体类型、`Content-Disposition` 及其文件名等参数仍参与指纹
  （调换 part 顺序会得到不同指纹）。

可以用 `app.mimeparser` 直接查看规范化结果：

```python
from app.mimeparser import parse_message, fingerprint, canonical_tree, canonical_json
tree = parse_message(open("message.eml", "rb").read())
fp, canon = fingerprint(tree)
print(fp)
print(canon.decode())
```

## 运行测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## 环境变量

| 变量 | 默认 | 说明 |
| ---- | ---- | ---- |
| `MIME_DB_PATH` | `/data/mime.db` | SQLite 文件路径，启动时自动建库建表 |

## 设计说明与边界

- **严格而非容错**：不使用 Python 标准库 `email` 包。标准库会修复坏边界、
  吞掉非法字节、对重复参数“后者覆盖”，不适合需要可复现指纹的归档场景。
- **字面头字节宽容**：非 encoded-word 的“脏”头（如老式 latin-1 Subject）
  按 UTF-8 → latin-1 回退解码，不会因此拒收邮件；编码字本身仍严格校验。
- **7bit 语义**：保留“内容仅 US-ASCII”这一不变量，但不强制 CRLF 配对，
  以支持换行风格无关的指纹。
- **epilogue / preamble**：multipart 前导区与闭合边界后的尾注按 RFC 忽略，
  不参与指纹。
- `message/rfc822` 与 `message/global` 容器递归解析；容器自身的 CTE
  先还原再递归。
