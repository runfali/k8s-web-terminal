"""
WebSocket处理器模块
处理WebSocket连接和Pod终端交互
"""

import asyncio
import json
from datetime import datetime
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from ..config import config
from ..models import K8sConnectionInfo, ConnectionStatus, WebSocketMessage
from ..services.k8s_service import KubernetesService
from ..services.database import DatabaseService
from ..utils.exceptions import (
    WebSocketConnectionError,
    WebSocketTimeoutError,
    K8sConnectionError,
    PodConnectionError,
)
from ..utils.logger import ws_logger, log_async_function_call


class WebSocketHandler:
    """WebSocket处理器类"""

    def __init__(self, k8s_service: KubernetesService, db_service: DatabaseService):
        self.k8s_service = k8s_service
        self.db_service = db_service

    @log_async_function_call(ws_logger)
    async def handle_connection(
        self,
        websocket: WebSocket,
        namespace: str,
        podname: str,
        chinesename: str = None,
    ) -> None:
        """处理WebSocket连接的主要逻辑"""
        await websocket.accept()

        effective_username = chinesename if chinesename else "unknown_user"
        ws_logger.info(
            f"为命名空间 {namespace} 中的 {podname} (用户: {effective_username}) 建立了 WebSocket 连接"
        )

        # 记录连接建立日志
        await self.db_service.log_terminal_connection(
            effective_username, namespace, podname, "连接建立"
        )

        try:
            # 验证Kubernetes连接
            if not self.k8s_service.is_connected():
                await websocket.send_text("错误：Kubernetes 配置未加载\r\n")
                await websocket.close()
                return

            # 创建Pod连接
            connection_info = K8sConnectionInfo(
                namespace=namespace, podname=podname, command=["/bin/bash"]
            )

            resp = self.k8s_service.create_exec_stream(connection_info)

            # 创建连接状态 - 修复：直接使用asyncio时间戳
            current_time = asyncio.get_event_loop().time()
            connection_status = ConnectionStatus(
                is_active=True,
                last_activity_time=current_time,
                connection_start_time=current_time,
                idle_timeout=config.websocket.idle_timeout,
                connection_timeout=config.websocket.connection_timeout,
            )

            # 启动读写任务
            await self._handle_communication(websocket, resp, connection_status)

        except WebSocketDisconnect:
            ws_logger.info(
                f"WebSocket 与 pod：{podname} 在命名空间：{namespace} 中断开连接"
            )
        except Exception as e:
            error_msg = f"WebSocket 错误，针对 {podname}：{e}\r\n"
            ws_logger.error(error_msg)
            if websocket.client_state != WebSocketState.DISCONNECTED:
                try:
                    await websocket.send_text(error_msg)
                except Exception:
                    pass
                await websocket.close()
        finally:
            ws_logger.info(
                f"WebSocket 连接已关闭，对应 Pod：{podname}，所在命名空间：{namespace}"
            )
            # 记录连接关闭日志
            try:
                await self.db_service.log_terminal_connection(
                    effective_username, namespace, podname, "连接关闭"
                )
            except Exception as log_err:
                ws_logger.error(f"记录连接关闭日志时出错: {log_err}")

    async def _handle_communication(
        self, websocket: WebSocket, resp, connection_status: ConnectionStatus
    ) -> None:
        """处理WebSocket和Pod之间的通信"""
        # 创建读写任务
        read_task = asyncio.create_task(
            self._read_from_pod(websocket, resp, connection_status)
        )
        write_task = asyncio.create_task(
            self._write_to_pod(websocket, resp, connection_status)
        )

        try:
            # 等待任一任务完成或超时
            done, pending = await asyncio.wait(
                [read_task, write_task],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=connection_status.connection_timeout,
            )

            # 取消未完成的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as cancel_err:
                    ws_logger.error(f"取消任务时出错: {cancel_err}")

        except asyncio.TimeoutError:
            ws_logger.warning("WebSocket连接超时，强制关闭")
            read_task.cancel()
            write_task.cancel()
            try:
                await read_task
                await write_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _read_from_pod(
        self, websocket: WebSocket, resp, connection_status: ConnectionStatus
    ) -> None:
        """从Pod读取数据并发送到WebSocket"""
        try:
            timeout = 0.05  # 50毫秒的超时
            last_heartbeat_time = asyncio.get_event_loop().time()
            last_check_time = asyncio.get_event_loop().time()

            while resp.is_open() and connection_status.is_active:
                try:
                    resp.update(timeout=timeout)
                    data_received = False

                    # 处理标准输出
                    if resp.peek_stdout():
                        stdout_data = resp.read_stdout()
                        if stdout_data:
                            stdout_data = self._format_terminal_data(stdout_data)
                            if websocket.client_state == WebSocketState.CONNECTED:
                                await websocket.send_text(stdout_data)
                                data_received = True
                            else:
                                break

                    # 处理标准错误
                    if resp.peek_stderr():
                        stderr_data = resp.read_stderr()
                        if stderr_data:
                            stderr_data = self._format_terminal_data(stderr_data)
                            if websocket.client_state == WebSocketState.CONNECTED:
                                await websocket.send_text(stderr_data)
                                data_received = True
                            else:
                                break

                    # 定期检查连接状态
                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_check_time > 30:
                        last_check_time = current_time
                        if not self._check_connection_timeout(
                            connection_status, current_time
                        ):
                            connection_status.is_active = False
                            break

                    # 发送心跳
                    if current_time - last_heartbeat_time > 15:
                        await self._send_heartbeat(resp)
                        last_heartbeat_time = current_time

                    # 动态调整休眠时间
                    sleep_time = 0.01 if data_received else 0.05
                    await asyncio.sleep(sleep_time)

                except Exception as loop_err:
                    ws_logger.error(f"Pod读取循环中出错: {loop_err}")
                    if self._is_connection_error(loop_err):
                        break
                    await asyncio.sleep(0.1)

        except Exception as e:
            ws_logger.error(f"从 Pod 读取时出错: {e}")
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_text(f"从 Pod 读取时出错: {e}\r\n")
                except Exception:
                    pass
        finally:
            await self._cleanup_pod_stream(resp, websocket, "read_from_pod")

    async def _write_to_pod(
        self, websocket: WebSocket, resp, connection_status: ConnectionStatus
    ) -> None:
        """从WebSocket读取数据并发送到Pod"""
        try:
            while connection_status.is_active:
                try:
                    # 设置超时
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=30)

                    # 检查心跳包
                    if data == "\x00":
                        ws_logger.debug("收到心跳包，跳过处理")
                        continue

                    # 更新活动时间 - 修复：直接使用asyncio时间戳
                    connection_status.last_activity_time = (
                        asyncio.get_event_loop().time()
                    )

                    if not resp.is_open():
                        ws_logger.warning("Pod连接已关闭，无法写入数据")
                        break

                    # 处理消息
                    await self._process_websocket_message(data, resp)

                    # 检查超时
                    if not self._check_connection_timeout(connection_status):
                        break

                except asyncio.TimeoutError:
                    # 超时检查
                    if not self._check_connection_timeout(connection_status):
                        break
                    if (
                        not resp.is_open()
                        or websocket.client_state != WebSocketState.CONNECTED
                    ):
                        ws_logger.info("检测到连接已关闭，终止写入循环")
                        break
                    continue

                except WebSocketDisconnect:
                    ws_logger.info("WebSocket 被客户端断开连接")
                    break

                except Exception as e:
                    ws_logger.error(f"接收或处理WebSocket数据时出错: {e}")
                    if self._is_connection_error(e):
                        break
                    await asyncio.sleep(0.1)

        except Exception as outer_e:
            ws_logger.error(f"写入 Pod 的外层循环出错: {outer_e}")
        finally:
            await self._cleanup_pod_stream(resp, websocket, "write_to_pod")

    async def _process_websocket_message(self, data: str, resp) -> None:
        """处理WebSocket消息"""
        try:
            # 尝试解析为JSON消息
            message = json.loads(data)
            if isinstance(message, dict) and message.get("type") == "resize":
                await self._handle_resize_message(message, resp)
                return
        except json.JSONDecodeError:
            pass

        # 处理普通文本消息
        await self._handle_text_message(data, resp)

    async def _handle_resize_message(self, message: dict, resp) -> None:
        """处理终端大小调整消息"""
        cols = message.get("cols")
        rows = message.get("rows")

        if cols is not None and rows is not None:
            resize_payload = json.dumps({"Width": int(cols), "Height": int(rows)})
            if resp.is_open():
                from kubernetes.stream import ws_client

                resp.write_channel(ws_client.RESIZE_CHANNEL, resize_payload)
                ws_logger.info(f"已发送 PTY resize 请求: cols={cols}, rows={rows}")
            else:
                ws_logger.warning("Pod 连接已关闭，无法发送 PTY resize 请求")

    async def _handle_text_message(self, data: str, resp) -> None:
        """处理文本消息"""
        if len(data) > 100 or "\n" in data:
            # 处理长文本或多行文本
            lines = data.splitlines()
            for i, line in enumerate(lines):
                if resp.is_open():
                    resp.write_stdin(line)
                    if i < len(lines) - 1:
                        resp.write_stdin("\n")
                else:
                    ws_logger.warning("Pod 连接已关闭，无法写入粘贴数据")
                    break
        else:
            # 处理单行文本
            if resp.is_open():
                resp.write_stdin(data)
            else:
                ws_logger.warning("Pod 连接已关闭，无法写入单行数据")

    def _format_terminal_data(self, data: str) -> str:
        """格式化终端数据"""
        if not data.endswith("\r\n") and data.endswith("\n"):
            return data.replace("\n", "\r\n")
        return data

    def _check_connection_timeout(
        self, connection_status: ConnectionStatus, current_time: float = None
    ) -> bool:
        """检查连接是否超时 - 修复：直接比较浮点数时间戳"""
        if current_time is None:
            current_time = asyncio.get_event_loop().time()

        # 检查空闲超时 - 直接比较浮点数
        if (
            current_time - connection_status.last_activity_time
            > connection_status.idle_timeout
        ):
            ws_logger.warning("连接超时，无活动时间过长")
            return False

        # 检查总连接时间超时 - 直接比较浮点数
        if (
            current_time - connection_status.connection_start_time
            > connection_status.connection_timeout
        ):
            ws_logger.warning("连接总时间超时")
            return False

        return True

    async def _send_heartbeat(self, resp) -> None:
        """发送Kubernetes连接心跳"""
        try:
            if resp.is_open():
                resp.write_stdin("")
                ws_logger.debug("发送Kubernetes连接心跳")
        except Exception as heartbeat_err:
            ws_logger.error(f"发送Kubernetes心跳失败: {heartbeat_err}")

    def _is_connection_error(self, error: Exception) -> bool:
        """判断是否为连接错误"""
        error_str = str(error).lower()
        return "connection" in error_str or "closed" in error_str

    async def _cleanup_pod_stream(
        self, resp, websocket: WebSocket, source: str
    ) -> None:
        """清理Pod流资源"""
        ws_logger.info(f"正在清理Pod流资源... (来源: {source})")

        if resp.is_open():
            try:
                resp.close()
                ws_logger.info(f"Pod响应流已关闭 ({source})")
            except Exception as resp_close_err:
                ws_logger.error(f"关闭Pod响应流时出错: {resp_close_err}")

        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
                ws_logger.info(f"WebSocket连接已关闭 ({source})")
            except Exception as close_err:
                ws_logger.error(f"关闭WebSocket连接时出错: {close_err}")

        ws_logger.info(f"流资源清理完成 ({source})")


# 创建WebSocket处理器实例的工厂函数
def create_websocket_handler(
    k8s_service: KubernetesService, db_service: DatabaseService
) -> WebSocketHandler:
    """创建WebSocket处理器实例"""
    return WebSocketHandler(k8s_service, db_service)
