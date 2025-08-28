"""
配置管理模块
将所有硬编码配置提取到此文件中，支持环境变量覆盖
"""

import os
from dataclasses import dataclass
from typing import Dict, Any


@dataclass
class DatabaseConfig:
    """数据库配置"""

    host: str
    port: int
    user: str
    password: str
    database: str
    min_size: int = 5
    max_size: int = 20
    max_inactive_connection_lifetime: float = 300.0
    timeout: float = 10.0


@dataclass
class K8sConfig:
    """Kubernetes配置"""

    config_file: str
    verify_ssl: bool = False
    ping_interval: int = 30
    ping_timeout: int = 120
    max_size: int = 10 * 1024 * 1024
    skip_utf8_validation: bool = True
    close_timeout: int = 30


@dataclass
class WebSocketConfig:
    """WebSocket配置"""

    idle_timeout: int = 300  # 5分钟无活动超时
    connection_timeout: int = 3600  # 1小时总连接超时
    ping_interval: int = 30
    ping_timeout: int = 60
    timeout_keep_alive: int = 300


@dataclass
class ServerConfig:
    """服务器配置"""

    host: str = "0.0.0.0"
    port: int = 8006
    limit_concurrency: int = 50
    limit_max_requests: int = 5000
    workers: int = 4


@dataclass
class LogConfig:
    """日志配置"""

    log_dir: str = "logs"
    log_file: str = "terminal.log"
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"


@dataclass
class CorsConfig:
    """CORS配置"""

    allow_origins: list = None
    allow_credentials: bool = True
    allow_methods: list = None
    allow_headers: list = None

    def __post_init__(self):
        if self.allow_origins is None:
            self.allow_origins = ["*"]
        if self.allow_methods is None:
            self.allow_methods = ["*"]
        if self.allow_headers is None:
            self.allow_headers = ["*"]


@dataclass
class AppConfig:
    """应用总配置"""

    database: DatabaseConfig
    k8s: K8sConfig
    websocket: WebSocketConfig
    server: ServerConfig
    log: LogConfig
    cors: CorsConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        """从环境变量加载配置"""
        # 数据库配置
        database = DatabaseConfig(
            host=os.getenv("POSTGRES_HOST", "10.200.1.171"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "kube"),
            password=os.getenv("POSTGRES_PASSWORD", "kube"),
            database=os.getenv("POSTGRES_DB", "kube"),
            min_size=int(os.getenv("DB_MIN_SIZE", "5")),
            max_size=int(os.getenv("DB_MAX_SIZE", "20")),
            max_inactive_connection_lifetime=float(
                os.getenv("DB_MAX_INACTIVE_TIME", "300.0")
            ),
            timeout=float(os.getenv("DB_TIMEOUT", "10.0")),
        )

        # Kubernetes配置
        config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
        k8s = K8sConfig(
            config_file=os.path.join(config_dir, "config"),
            verify_ssl=os.getenv("K8S_VERIFY_SSL", "false").lower() == "true",
            ping_interval=int(os.getenv("K8S_PING_INTERVAL", "30")),
            ping_timeout=int(os.getenv("K8S_PING_TIMEOUT", "120")),
            max_size=int(os.getenv("K8S_MAX_SIZE", str(10 * 1024 * 1024))),
            skip_utf8_validation=os.getenv("K8S_SKIP_UTF8_VALIDATION", "true").lower()
            == "true",
            close_timeout=int(os.getenv("K8S_CLOSE_TIMEOUT", "30")),
        )

        # WebSocket配置
        websocket = WebSocketConfig(
            idle_timeout=int(os.getenv("WS_IDLE_TIMEOUT", "300")),
            connection_timeout=int(os.getenv("WS_CONNECTION_TIMEOUT", "3600")),
            ping_interval=int(os.getenv("WS_PING_INTERVAL", "30")),
            ping_timeout=int(os.getenv("WS_PING_TIMEOUT", "60")),
            timeout_keep_alive=int(os.getenv("WS_TIMEOUT_KEEP_ALIVE", "300")),
        )

        # 服务器配置
        server = ServerConfig(
            host=os.getenv("SERVER_HOST", "0.0.0.0"),
            port=int(os.getenv("SERVER_PORT", "8006")),
            limit_concurrency=int(os.getenv("SERVER_LIMIT_CONCURRENCY", "50")),
            limit_max_requests=int(os.getenv("SERVER_LIMIT_MAX_REQUESTS", "5000")),
            workers=int(os.getenv("SERVER_WORKERS", "4")),
        )

        # 日志配置
        log = LogConfig(
            log_dir=os.getenv("LOG_DIR", "logs"),
            log_file=os.getenv("LOG_FILE", "terminal.log"),
            max_bytes=int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024))),
            backup_count=int(os.getenv("LOG_BACKUP_COUNT", "5")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_format=os.getenv(
                "LOG_FORMAT", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            ),
            date_format=os.getenv("LOG_DATE_FORMAT", "%Y-%m-%d %H:%M:%S"),
        )

        # CORS配置
        cors_origins = os.getenv("CORS_ORIGINS")
        cors = CorsConfig(
            allow_origins=cors_origins.split(",") if cors_origins else ["*"],
            allow_credentials=os.getenv("CORS_ALLOW_CREDENTIALS", "true").lower()
            == "true",
            allow_methods=os.getenv("CORS_ALLOW_METHODS", "*").split(","),
            allow_headers=os.getenv("CORS_ALLOW_HEADERS", "*").split(","),
        )

        return cls(
            database=database,
            k8s=k8s,
            websocket=websocket,
            server=server,
            log=log,
            cors=cors,
        )


# 全局配置实例
config = AppConfig.from_env()
