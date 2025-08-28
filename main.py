"""
K8s Web Terminal 应用入口
重构后的主入口文件，包含应用初始化、健康检查和API文档配置
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Path
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import uvicorn

from app.config import config
from app.api.terminal import router as terminal_router
from app.services.database import db_service
from app.services.k8s_service import k8s_service
from app.services.upload_service import create_upload_service
from app.models import HealthCheckResponse, APIResponse, FileUploadResponse
from app.utils.exceptions import BaseAppException
from app.utils.logger import app_logger, Logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化
    app_logger.info("正在启动 K8s Web Terminal 应用...")

    try:
        # 初始化数据库服务
        await db_service.initialize()
        app_logger.info("数据库服务初始化完成")

        # 初始化Kubernetes服务
        k8s_service.initialize()
        app_logger.info("Kubernetes服务初始化完成")

        app_logger.info("K8s Web Terminal 应用启动成功")

    except Exception as e:
        app_logger.error(f"应用启动失败: {e}")
        # 清理已初始化的服务
        try:
            await db_service.close()
            k8s_service.close()
        except Exception:
            pass
        raise

    yield  # 应用运行

    # 关闭时清理
    app_logger.info("正在关闭 K8s Web Terminal 应用...")

    try:
        await db_service.close()
        k8s_service.close()
        Logger.shutdown()
        app_logger.info("K8s Web Terminal 应用已安全关闭")
    except Exception as e:
        app_logger.error(f"应用关闭时出错: {e}")


# 创建FastAPI应用
app = FastAPI(
    title="K8s Web Terminal API",
    description="""
    ## Kubernetes Pod Web Terminal Interface
    
    这是一个通过Web浏览器访问Kubernetes Pod终端的工具，提供以下功能：
    
    ### 主要功能
    * **终端连接**: 通过WebSocket连接到Kubernetes Pod
    * **文件上传**: 将文件上传到Pod的/tmp目录
    * **日志记录**: 记录终端连接和操作日志
    * **Pod管理**: 查看Pod信息和状态
    
    ### 技术特性
    * 基于FastAPI和WebSocket的实时通信
    * 支持终端大小自适应
    * 异步高性能处理
    * 完善的错误处理和日志记录
    
    ### 使用方法
    1. 访问 `/connect` 端点连接到Pod终端
    2. 使用 `/upload` 端点上传文件到Pod
    3. 通过 `/logs` 端点查看操作日志
    4. 使用 `/health` 端点检查服务状态
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 配置CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors.allow_origins,
    allow_credentials=config.cors.allow_credentials,
    allow_methods=config.cors.allow_methods,
    allow_headers=config.cors.allow_headers,
)

# 挂载静态文件
app.mount("/static", StaticFiles(directory="templates/static"), name="static")

# 注册路由
app.include_router(terminal_router, prefix="/api/v1")

# 兼容原始路由（保持向后兼容）
app.include_router(terminal_router)


@app.get("/health", response_model=HealthCheckResponse, summary="健康检查")
async def health_check():
    """
    应用健康检查端点

    检查各个服务组件的状态：
    - 数据库连接状态
    - Kubernetes连接状态
    - 整体服务状态
    """
    try:
        # 检查数据库状态
        db_status = "connected" if db_service.is_connected() else "disconnected"

        # 检查Kubernetes状态
        k8s_status = "connected" if k8s_service.is_connected() else "disconnected"

        # 确定整体状态
        overall_status = (
            "healthy"
            if (db_status == "connected" and k8s_status == "connected")
            else "degraded"
        )

        response = HealthCheckResponse(
            status=overall_status, database=db_status, kubernetes=k8s_status
        )

        app_logger.info(f"健康检查: {overall_status}")
        return response

    except Exception as e:
        app_logger.error(f"健康检查失败: {e}")
        return HealthCheckResponse(
            status="unhealthy", database="error", kubernetes="error"
        )


@app.get("/", summary="根路径")
async def root():
    """
    根路径端点

    返回API基本信息和可用端点
    """
    return APIResponse(
        success=True,
        message="K8s Web Terminal API",
        data={
            "version": "1.0.0",
            "description": "Kubernetes Pod Web Terminal Interface",
            "endpoints": {
                "docs": "/docs",
                "health": "/health",
                "connect": "/connect",
                "upload": "/upload/{namespace}/{podname}",
                "version": "/version",
                "websocket": "/ws/{namespace}/{podname}",
            },
        },
    )


@app.get("/version", summary="版本信息")
async def get_version():
    """获取应用版本信息"""
    return APIResponse(
        success=True,
        message="版本信息",
        data={
            "version": "1.0.0",
            "build_time": "2024-08-27",
            "python_version": "3.8+",
            "fastapi_version": "0.104.1",
        },
    )


# 全局异常处理器
@app.exception_handler(BaseAppException)
async def app_exception_handler(request, exc: BaseAppException):
    """处理自定义应用异常"""
    app_logger.error(f"应用异常: {exc.message} (错误码: {exc.error_code})")
    return JSONResponse(
        status_code=400,
        content={
            "success": False,
            "message": exc.message,
            "error_code": exc.error_code,
        },
    )


# 直接添加原版本的上传端点，确保兼容性
@app.post(
    "/upload/{namespace}/{podname}",
    response_model=FileUploadResponse,
    summary="上传文件到Pod",
)
async def upload_file_to_pod_compat(
    namespace: str = Path(..., description="命名空间"),
    podname: str = Path(..., description="Pod名称"),
    file: UploadFile = File(..., description="要上传的文件"),
) -> FileUploadResponse:
    """兼容原版本的文件上传端点"""
    # 使用与原版本完全相同的logger和日志格式
    import logging

    logger = logging.getLogger("k8s_web_terminal")

    # 确保logger有合适的处理器
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    logger.info(
        f"接收到文件上传请求: namespace={namespace}, podname={podname}, original_filename={file.filename}, safe_filename={file.filename}, destination=/tmp/{file.filename}"
    )

    try:
        # 使用upload_service处理上传
        upload_service = create_upload_service()
        result = await upload_service.upload_file(namespace, podname, file)

        # 如果有错误，抛出HTTP异常
        if result.error:
            logger.error(f"上传失败: {result.error}")
            raise HTTPException(
                status_code=result.status_code or 500, detail=result.error
            )

        logger.info(f"上传成功: {result.message}")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"上传过程中发生未知错误: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.exception_handler(Exception)
async def general_exception_handler(request, exc: Exception):
    """处理通用异常"""
    app_logger.error(f"未处理的异常: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "内部服务器错误",
            "error_code": "INTERNAL_SERVER_ERROR",
        },
    )


if __name__ == "__main__":
    """应用入口点"""
    app_logger.info("正在启动 K8s Web Terminal 服务器...")

    uvicorn.run(
        "main:app",
        host=config.server.host,
        port=config.server.port,
        ws_ping_interval=config.websocket.ping_interval,
        ws_ping_timeout=config.websocket.ping_timeout,
        timeout_keep_alive=config.websocket.timeout_keep_alive,
        limit_concurrency=config.server.limit_concurrency,
        limit_max_requests=config.server.limit_max_requests,
        workers=1,  # WebSocket应用建议使用单worker
        reload=False,  # 生产环境建议关闭reload
        log_level=config.log.log_level.lower(),
    )
