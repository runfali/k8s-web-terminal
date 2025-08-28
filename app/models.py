"""
数据模型模块
定义应用中使用的所有数据模型
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """数据库配置模型"""

    host: str
    port: int
    user: str
    password: str
    database: str


class TerminalLog(BaseModel):
    """终端日志模型"""

    id: Optional[int] = None
    username: str = Field(..., description="用户名")
    namespace: str = Field(..., description="命名空间")
    podname: str = Field(..., description="Pod名称")
    connection_time: Optional[datetime] = Field(
        default_factory=datetime.utcnow, description="连接时间"
    )
    action: str = Field(default="连接", description="操作类型")


class FileUploadResponse(BaseModel):
    """文件上传响应模型"""

    message: Optional[str] = None
    error: Optional[str] = None
    status_code: Optional[int] = None


class WebSocketMessage(BaseModel):
    """WebSocket消息模型"""

    type: str = Field(..., description="消息类型")
    cols: Optional[int] = Field(None, description="终端列数")
    rows: Optional[int] = Field(None, description="终端行数")
    data: Optional[str] = Field(None, description="消息数据")


class HealthCheckResponse(BaseModel):
    """健康检查响应模型"""

    status: str = Field(..., description="服务状态")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="检查时间")
    version: str = Field(default="1.0.0", description="版本号")
    database: Optional[str] = Field(None, description="数据库状态")
    kubernetes: Optional[str] = Field(None, description="Kubernetes状态")


class APIResponse(BaseModel):
    """通用API响应模型"""

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应消息")
    data: Optional[dict] = Field(None, description="响应数据")
    error_code: Optional[str] = Field(None, description="错误代码")


class K8sConnectionInfo(BaseModel):
    """Kubernetes连接信息模型"""

    namespace: str = Field(..., description="命名空间")
    podname: str = Field(..., description="Pod名称")
    command: list[str] = Field(
        default_factory=lambda: ["/bin/bash"], description="执行命令"
    )


class ConnectionStatus(BaseModel):
    """连接状态模型 - 修复：使用浮点数时间戳而不是datetime对象"""

    is_active: bool = Field(..., description="连接是否活跃")
    last_activity_time: float = Field(..., description="最后活动时间(时间戳)")
    connection_start_time: float = Field(..., description="连接开始时间(时间戳)")
    idle_timeout: int = Field(default=300, description="空闲超时时间")
    connection_timeout: int = Field(default=3600, description="连接超时时间")
