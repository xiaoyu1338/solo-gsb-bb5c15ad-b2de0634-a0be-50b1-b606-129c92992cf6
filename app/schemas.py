"""API 响应模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class MessageSummary(BaseModel):
    id: int
    fingerprint: str
    raw_sha256: str
    content_type: str
    size_bytes: int
    created_at: str


class MessageListResponse(BaseModel):
    items: list[MessageSummary]
    total: int
    limit: int
    offset: int


class MessageCreated(BaseModel):
    id: int
    fingerprint: str
    raw_sha256: str
    content_type: str
    size_bytes: int
    created: bool  # True=新入库；False=同指纹邮件已存在（幂等）
    node_count: int


class NodeSummary(BaseModel):
    path: str
    depth: int
    position: int
    content_type: str
    kind: str
    cte: str
    disposition: Optional[str] = None
    children: list[str]


class TreeNode(BaseModel):
    """嵌套 MIME 树节点，children 为完整子树。"""

    path: str
    depth: int
    position: int
    content_type: str
    kind: str
    cte: str
    disposition: Optional[str] = None
    children: list["TreeNode"] = []


TreeNode.model_rebuild()


class TreeResponse(BaseModel):
    id: int
    fingerprint: str
    tree: TreeNode


class NodeListResponse(BaseModel):
    id: int
    fingerprint: str
    nodes: list[NodeSummary]


class NodeDetail(NodeSummary):
    headers: list[list[str]]
    text: Optional[str] = None
    blob_sha256: Optional[str] = None
    blob_length: Optional[int] = None


class NodeDetailResponse(BaseModel):
    id: int
    fingerprint: str
    node: NodeDetail


class ErrorBody(BaseModel):
    code: str
    message: str
    node_path: str
    detail: dict


class ErrorResponse(BaseModel):
    error: ErrorBody
