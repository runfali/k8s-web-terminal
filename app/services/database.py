"""
数据库服务模块
管理数据库连接池和数据库操作
"""

import asyncpg
from datetime import datetime
from typing import Optional
from ..config import config
from ..models import TerminalLog
from ..utils.exceptions import DatabaseConnectionError, DatabaseOperationError
from ..utils.logger import db_logger, log_async_function_call


class DatabaseService:
    """数据库服务类"""

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    @log_async_function_call(db_logger)
    async def initialize(self) -> None:
        """初始化数据库连接池"""
        try:
            self.pool = await asyncpg.create_pool(
                user=config.database.user,
                password=config.database.password,
                database=config.database.database,
                host=config.database.host,
                port=config.database.port,
                min_size=config.database.min_size,
                max_size=config.database.max_size,
                max_inactive_connection_lifetime=config.database.max_inactive_connection_lifetime,
                timeout=config.database.timeout,
            )

            # 创建表
            await self._create_tables()

            db_logger.info("数据库连接池已创建，terminal_logs 表已准备就绪")

        except Exception as e:
            db_logger.error(f"数据库初始化失败: {e}")
            raise DatabaseConnectionError(f"数据库初始化失败: {e}")

    @log_async_function_call(db_logger)
    async def close(self) -> None:
        """关闭数据库连接池"""
        if self.pool:
            await self.pool.close()
            db_logger.info("数据库连接池已关闭")

    async def _create_tables(self) -> None:
        """创建数据库表"""
        if not self.pool:
            raise DatabaseConnectionError("数据库连接池未初始化")

        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS terminal_logs (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(255) NOT NULL,
                    namespace VARCHAR(255) NOT NULL,
                    podname VARCHAR(255) NOT NULL,
                    connection_time TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'UTC'),
                    action VARCHAR(255) DEFAULT '连接'
                );
            """
            )

    @log_async_function_call(db_logger)
    async def log_terminal_connection(
        self, username: str, namespace: str, podname: str, action: str = "连接"
    ) -> None:
        """记录终端连接日志"""
        if not self.pool:
            db_logger.error("数据库连接池不可用，无法记录日志")
            return

        try:
            async with self.pool.acquire() as connection:
                await connection.execute(
                    "INSERT INTO terminal_logs (username, namespace, podname, connection_time, action) VALUES ($1, $2, $3, $4, $5)",
                    username,
                    namespace,
                    podname,
                    datetime.utcnow(),
                    action,
                )

                # 立即提交事务
                await connection.execute("COMMIT")

                db_logger.info(
                    f"日志记录成功: 用户名={username}, 命名空间={namespace}, Pod名称={podname}, 操作={action}"
                )

        except Exception as e:
            db_logger.error(f"记录终端连接日志时出错: {e}")
            raise DatabaseOperationError(f"记录终端连接日志失败: {e}")

    @log_async_function_call(db_logger)
    async def get_terminal_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        username: Optional[str] = None,
        namespace: Optional[str] = None,
        podname: Optional[str] = None,
    ) -> list[TerminalLog]:
        """获取终端日志"""
        if not self.pool:
            raise DatabaseConnectionError("数据库连接池不可用")

        try:
            query = "SELECT * FROM terminal_logs WHERE 1=1"
            params = []
            param_count = 0

            if username:
                param_count += 1
                query += f" AND username = ${param_count}"
                params.append(username)

            if namespace:
                param_count += 1
                query += f" AND namespace = ${param_count}"
                params.append(namespace)

            if podname:
                param_count += 1
                query += f" AND podname = ${param_count}"
                params.append(podname)

            query += " ORDER BY connection_time DESC"

            param_count += 1
            query += f" LIMIT ${param_count}"
            params.append(limit)

            param_count += 1
            query += f" OFFSET ${param_count}"
            params.append(offset)

            async with self.pool.acquire() as connection:
                rows = await connection.fetch(query, *params)

                logs = []
                for row in rows:
                    log = TerminalLog(
                        id=row["id"],
                        username=row["username"],
                        namespace=row["namespace"],
                        podname=row["podname"],
                        connection_time=row["connection_time"],
                        action=row["action"],
                    )
                    logs.append(log)

                return logs

        except Exception as e:
            db_logger.error(f"获取终端日志时出错: {e}")
            raise DatabaseOperationError(f"获取终端日志失败: {e}")

    @log_async_function_call(db_logger)
    async def get_connection_stats(self) -> dict:
        """获取连接统计信息"""
        if not self.pool:
            raise DatabaseConnectionError("数据库连接池不可用")

        try:
            async with self.pool.acquire() as connection:
                # 总连接数
                total_count = await connection.fetchval(
                    "SELECT COUNT(*) FROM terminal_logs"
                )

                # 今日连接数
                today_count = await connection.fetchval(
                    """
                    SELECT COUNT(*) FROM terminal_logs 
                    WHERE DATE(connection_time) = CURRENT_DATE
                """
                )

                # 活跃用户数
                active_users = await connection.fetchval(
                    """
                    SELECT COUNT(DISTINCT username) FROM terminal_logs 
                    WHERE connection_time >= NOW() - INTERVAL '24 hours'
                """
                )

                # 最近连接
                recent_connections = await connection.fetch(
                    """
                    SELECT username, namespace, podname, connection_time, action 
                    FROM terminal_logs 
                    ORDER BY connection_time DESC 
                    LIMIT 10
                """
                )

                return {
                    "total_connections": total_count,
                    "today_connections": today_count,
                    "active_users_24h": active_users,
                    "recent_connections": [dict(row) for row in recent_connections],
                }

        except Exception as e:
            db_logger.error(f"获取连接统计信息时出错: {e}")
            raise DatabaseOperationError(f"获取连接统计信息失败: {e}")

    def is_connected(self) -> bool:
        """检查数据库是否连接"""
        return self.pool is not None and not self.pool._closed


# 全局数据库服务实例
db_service = DatabaseService()
