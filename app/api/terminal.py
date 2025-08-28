"""
终端API路由模块
处理与终端连接相关的HTTP和WebSocket端点
"""

from fastapi import APIRouter, Request, WebSocket, Query, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from ..services.k8s_service import k8s_service
from ..services.database import db_service
from ..handlers.websocket_handler import create_websocket_handler
from ..models import HealthCheckResponse, APIResponse
from ..utils.exceptions import K8sConnectionError, PodNotFoundError
from ..utils.logger import app_logger, log_async_function_call
from datetime import datetime

# 创建路由器
router = APIRouter(tags=["terminal"])

# 模板配置
templates = Jinja2Templates(directory="templates")

# 创建WebSocket处理器
websocket_handler = create_websocket_handler(k8s_service, db_service)


@router.get("/connect", response_class=HTMLResponse, summary="连接到Pod终端")
async def connect_to_pod(
    request: Request,
    chinesename: str = Query(..., description="用户名"),
    podname: str = Query(..., description="Pod名称"),
    namespace: str = Query(..., description="命名空间"),
):
    """
    连接到指定Pod的终端页面

    - **chinesename**: 用户名（用于日志记录）
    - **podname**: 要连接的Pod名称
    - **namespace**: Pod所在的命名空间
    """
    app_logger.info(
        f"收到终端连接请求：用户={chinesename}，Pod={podname}，命名空间={namespace}"
    )

    try:
        # 验证Pod是否存在
        if not k8s_service.check_pod_exists(namespace, podname):
            raise HTTPException(
                status_code=404,
                detail=f"Pod '{podname}' 在命名空间 '{namespace}' 中不存在",
            )

        return templates.TemplateResponse(
            "terminal.html",
            {
                "request": request,
                "podname": podname,
                "namespace": namespace,
                "chinesename": chinesename,
            },
        )

    except PodNotFoundError as e:
        app_logger.error(f"Pod未找到: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except K8sConnectionError as e:
        app_logger.error(f"Kubernetes连接错误: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        app_logger.error(f"连接终端时发生未知错误: {e}")
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.websocket("/ws/{namespace}/{podname}")
async def websocket_endpoint(
    websocket: WebSocket,
    namespace: str,
    podname: str,
    chinesename: str = Query(None, description="用户名"),
):
    """
    WebSocket终端连接端点

    - **namespace**: Pod所在的命名空间
    - **podname**: Pod名称
    - **chinesename**: 用户名（可选）
    """
    await websocket_handler.handle_connection(
        websocket, namespace, podname, chinesename
    )
