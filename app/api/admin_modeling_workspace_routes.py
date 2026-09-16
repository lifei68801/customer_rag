"""建模工作台的后端接口。

只做编排：参数校验、调 app/graphrag/ 下的纯逻辑、把领域异常翻译成 HTTP 状态。
对齐、投影这些"想法"全在前端（modelingWorkbench/alignToSkeleton.ts、
projectToDraft.ts）——读表和列统计已经在浏览器里做完了，把列名传回后端再对
一次只是多一趟往返。

写草稿没有自己的端点：前端拿 apply-preview 的 diff 给用户看过之后，直接调
既有的 POST /api/admin/ontology/{tenant_id}/draft/replace。那条端点已经有
整份校验、变更日志和映射同提交，再包一层只会把它的错误映射复制一遍。

prefix 用 /api/admin/ontology 而不是新起一个：
tests/api/test_admin_route_shapes.py 的 _TENANT_SCOPED_PREFIXES 里已经有
"/api/admin/ontology/{tenant_id}/"，沿用它就不必再往那份白名单里加条目。
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.ontology_grounding import derive_grounding
from app.graphrag.ontology_modeling_workspace import (
    InvalidWorkspaceStateError,
    WorkspaceConflictError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
    create_workspace,
    delete_workspace,
    get_workspace,
    save_workspace,
)
from app.graphrag.ontology_skill_export import NothingToExportError, export_skill_yaml
from app.graphrag.ontology_skills import SkillRegistry, UnknownSkillError
from app.graphrag.ontology_workspace_apply import diff_against_draft

router = APIRouter(
    prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)]
)


class CreateWorkspaceRequest(BaseModel):
    #: 用哪个内置 skill 起步；None = 空白起步
    skill_name: str | None = None


class SaveWorkspaceRequest(BaseModel):
    state: dict
    #: 乐观锁：手上这份工作区的 updated_at。对不上说明期间有人存过。
    updated_at: str


class ApplyPreviewRequest(BaseModel):
    #: 三段的形状跟 /draft/replace 的 payload 一致——预览的就是那次提交。
    term_types: list[dict] = []
    relation_types: list[dict] = []
    constraints: list[dict] = []


class ExportSkillRequest(BaseModel):
    skill_name: str
    display_name: str


@router.get("/{tenant_id}/modeling-workspace/skills")
async def list_modeling_skills(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    registry: SkillRegistry = Depends(deps.get_skill_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    return {"skills": [skill.to_dict() for skill in registry.all()]}


@router.get("/{tenant_id}/modeling-workspace")
async def read_modeling_workspace(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    """没有工作区时返回 {"workspace": null} 而不是 404。

    404 在前端是"这个租户不存在"的信号（require_active_tenant_or_404 用的就是
    它）；"还没建工作区"是工作台的正常首屏状态，用同一个状态码会让起步页跟
    错误页混在一起。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    workspace = await get_workspace(review_conn, tenant_id)
    return {"workspace": None if workspace is None else workspace.to_dict()}


@router.post("/{tenant_id}/modeling-workspace")
async def create_modeling_workspace(
    tenant_id: str,
    payload: CreateWorkspaceRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
    registry: SkillRegistry = Depends(deps.get_skill_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    skill = None
    if payload.skill_name is not None:
        try:
            skill = registry.get(payload.skill_name)
        except UnknownSkillError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    try:
        workspace = await create_workspace(
            review_conn,
            tenant_id,
            skill=skill,
            actor=session.username,
            now=datetime.now().isoformat(),
        )
    except WorkspaceExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"workspace": workspace.to_dict()}


@router.put("/{tenant_id}/modeling-workspace")
async def save_modeling_workspace(
    tenant_id: str,
    payload: SaveWorkspaceRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        workspace = await save_workspace(
            review_conn,
            tenant_id,
            state=payload.state,
            expected_updated_at=payload.updated_at,
            actor=session.username,
            now=datetime.now().isoformat(),
        )
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InvalidWorkspaceStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"workspace": workspace.to_dict()}


@router.delete("/{tenant_id}/modeling-workspace")
async def delete_modeling_workspace(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    """重新起步。删的只是过程状态——已经应用进草稿的本体不受影响。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    await delete_workspace(review_conn, tenant_id)
    return {"deleted": True}


@router.get("/{tenant_id}/modeling-workspace/grounding")
async def read_modeling_grounding(
    tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    return (await derive_grounding(review_conn, tenant_id)).to_dict()


@router.post("/{tenant_id}/modeling-workspace/apply-preview")
async def preview_modeling_apply(
    tenant_id: str,
    payload: ApplyPreviewRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    """只算差异，不写库。写库是前端下一步调 /draft/replace 的事。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        diff = await diff_against_draft(
            review_conn,
            tenant_id,
            term_types=payload.term_types,
            relation_types=payload.relation_types,
            constraints=payload.constraints,
        )
    except KeyError as exc:
        # 提交里少了 value / relation_type / subject_term_type 这类必需键。
        # 让它变成裸 500 的话，界面上只会显示"服务器错误"。
        raise HTTPException(status_code=400, detail=f"提交里缺少必需字段 {exc.args[0]!r}")
    return diff.to_dict()


@router.post("/{tenant_id}/modeling-workspace/export-skill")
async def export_modeling_skill(
    tenant_id: str,
    payload: ExportSkillRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict:
    """导出 YAML 文本。用 POST 而不是 GET：要带 skill_name/display_name 两个
    参数，且产物是给人下载的一次性文件，不该被缓存。"""
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        text = await export_skill_yaml(
            review_conn,
            tenant_id,
            skill_name=payload.skill_name,
            display_name=payload.display_name,
            today=datetime.now().date().isoformat(),
        )
    except (NothingToExportError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"yaml": text}
