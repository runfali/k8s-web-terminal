"""
日志工具模块
统一管理应用的日志配置和记录
"""

import os
import logging
import logging.handlers
from typing import Optional
from ..config import config


class Logger:
    """日志管理类"""

    _loggers = {}

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """获取日志记录器"""
        if name not in cls._loggers:
            cls._loggers[name] = cls._setup_logger(name)
        return cls._loggers[name]

    @classmethod
    def _setup_logger(cls, name: str) -> logging.Logger:
        """设置日志记录器"""
        # 确保日志目录存在
        os.makedirs(config.log.log_dir, exist_ok=True)

        # 创建日志记录器
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, config.log.log_level.upper()))

        # 避免重复添加处理器
        if logger.handlers:
            return logger

        # 创建文件处理器
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(config.log.log_dir, config.log.log_file),
            maxBytes=config.log.max_bytes,
            backupCount=config.log.backup_count,
            encoding="utf-8",
        )

        # 创建控制台处理器
        console_handler = logging.StreamHandler()

        # 设置日志格式
        formatter = logging.Formatter(
            config.log.log_format, datefmt=config.log.date_format
        )

        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # 添加处理器到日志记录器
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        return logger

    @classmethod
    def shutdown(cls):
        """关闭所有日志记录器"""
        logging.shutdown()


# 预定义的日志记录器
app_logger = Logger.get_logger("k8s_web_terminal")
db_logger = Logger.get_logger("database")
k8s_logger = Logger.get_logger("kubernetes")
ws_logger = Logger.get_logger("websocket")
upload_logger = Logger.get_logger("upload")


def log_function_call(logger: logging.Logger = None):
    """函数调用日志装饰器"""

    def decorator(func):
        def wrapper(*args, **kwargs):
            if logger is None:
                current_logger = app_logger
            else:
                current_logger = logger

            current_logger.debug(f"调用函数: {func.__name__}")
            try:
                result = func(*args, **kwargs)
                current_logger.debug(f"函数 {func.__name__} 执行成功")
                return result
            except Exception as e:
                current_logger.error(f"函数 {func.__name__} 执行失败: {e}")
                raise

        return wrapper

    return decorator


def log_async_function_call(logger: logging.Logger = None):
    """异步函数调用日志装饰器"""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            if logger is None:
                current_logger = app_logger
            else:
                current_logger = logger

            current_logger.debug(f"调用异步函数: {func.__name__}")
            try:
                result = await func(*args, **kwargs)
                current_logger.debug(f"异步函数 {func.__name__} 执行成功")
                return result
            except Exception as e:
                current_logger.error(f"异步函数 {func.__name__} 执行失败: {e}")
                raise

        return wrapper

    return decorator


def log_terminal_operation(username: str, namespace: str, podname: str, action: str):
    """记录终端操作日志"""
    app_logger.info(
        f"终端操作 - 用户: {username}, 命名空间: {namespace}, Pod: {podname}, 操作: {action}"
    )


def log_file_upload(
    username: str, namespace: str, podname: str, filename: str, status: str
):
    """记录文件上传日志"""
    upload_logger.info(
        f"文件上传 - 用户: {username}, 命名空间: {namespace}, Pod: {podname}, 文件: {filename}, 状态: {status}"
    )


def log_database_operation(operation: str, status: str, details: str = ""):
    """记录数据库操作日志"""
    message = f"数据库操作 - 操作: {operation}, 状态: {status}"
    if details:
        message += f", 详情: {details}"
    db_logger.info(message)


def log_k8s_operation(
    operation: str, namespace: str, podname: str, status: str, details: str = ""
):
    """记录Kubernetes操作日志"""
    message = f"K8s操作 - 操作: {operation}, 命名空间: {namespace}, Pod: {podname}, 状态: {status}"
    if details:
        message += f", 详情: {details}"
    k8s_logger.info(message)


def log_websocket_event(event: str, namespace: str, podname: str, details: str = ""):
    """记录WebSocket事件日志"""
    message = f"WebSocket事件 - 事件: {event}, 命名空间: {namespace}, Pod: {podname}"
    if details:
        message += f", 详情: {details}"
    ws_logger.info(message)
