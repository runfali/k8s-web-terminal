"""
API路由模块
包含终端相关的API端点
"""

from .terminal import router as terminal_router

__all__ = ["terminal_router"]
