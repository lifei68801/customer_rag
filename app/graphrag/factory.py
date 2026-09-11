from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.config.settings import Settings
from app.graphrag.neo4j_client import Neo4jDriverProtocol, Neo4jGraphClient
from app.graphrag.neptune_client import NeptuneClientProtocol, NeptuneGraphClient
from app.graphrag.ontology import Term, load_terminology


def _default_driver_factory(uri: str, *, auth: tuple[str, str]) -> Neo4jDriverProtocol:
    from neo4j import AsyncGraphDatabase, NotificationDisabledClassification

    # 关掉 UNRECOGNIZED 这一类通知（"你查的标签/类型/属性在库里不存在"）。
    #
    # 本体图页面会拿**草稿里**的关系类型去图里探扇出，而那些类型在数据导入
    # 之前根本不存在。这是预期内的状态——探测失败退回"未知"，见
    # admin_ontology_routes 里 fanout 端点的 docstring。但驱动会为每一次这样的
    # 查询打一条极长的通知对象，刷一次页面就是十几条，把 stderr 淹掉；真出错
    # 的那条堆栈混在里面根本看不见。
    #
    # **只关这一类。** DEPRECATION / PERFORMANCE / SECURITY 那些是真该看见的，
    # 一起关掉等于把日志变成哑巴。
    #
    # 实测依据（2026-09-11，neo4j:5.22-community + 驱动 6.2.0）：对一个不存在的
    # 关系类型跑一次查询，默认产出 1 条通知 / 873 字节日志；加上这个设置之后
    # 是 0 条 / 0 字节。参数名在驱动 6.x 叫 notifications_disabled_classifications
    # （5.x 那个 ..._categories 已废弃），同样是实测确认的。
    return AsyncGraphDatabase.driver(
        uri,
        auth=auth,
        notifications_disabled_classifications=[
            NotificationDisabledClassification.UNRECOGNIZED
        ],
    )


def _default_neptune_client_factory(endpoint: str, *, port: int) -> NeptuneClientProtocol:
    raise NotImplementedError(
        "真实 AWS Neptune 连接尚未实现——这个仓库目前没有 boto3/AWS 认证签名依赖。"
        "接入真实 Neptune 环境时需要实现一个满足 NeptuneClientProtocol 的具体 "
        "client（openCypher HTTPS 端点 + AWS 请求签名），并通过 "
        "build_graph_client_from_settings(settings, neptune_client_factory=...) "
        "注入，而不是依赖这个默认工厂。"
    )


def build_graph_client_from_settings(
    settings: Settings,
    *,
    driver_factory: Callable[..., Neo4jDriverProtocol] | None = None,
    neptune_client_factory: Callable[..., NeptuneClientProtocol] | None = None,
) -> Neo4jGraphClient | NeptuneGraphClient:
    if settings.graph_backend == "neptune":
        factory = neptune_client_factory or _default_neptune_client_factory
        client = factory(settings.neptune.endpoint, port=settings.neptune.port)
        return NeptuneGraphClient(client=client)
    factory = driver_factory or _default_driver_factory
    driver = factory(
        settings.neo4j.uri, auth=(settings.neo4j.user, settings.neo4j.password)
    )
    return Neo4jGraphClient(driver=driver)


def load_terms_from_settings(settings: Settings) -> list[Term]:
    return load_terminology(Path(settings.terminology_path))
