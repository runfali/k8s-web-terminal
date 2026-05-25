"""
文件上传服务模块
处理文件上传到Kubernetes Pod的逻辑 - 完全按照原版main.py实现
"""

import os
import shutil
import tempfile
import io
import tarfile
from fastapi import UploadFile
import kubernetes
from kubernetes.stream import ws_client
from ..models import FileUploadResponse
from ..utils.exceptions import (
    FileValidationError,
    FileTransferError,
    K8sConnectionError,
    PodConnectionError,
)
from ..utils.logger import upload_logger, log_async_function_call


class FileUploadService:
    """文件上传服务类"""

    def __init__(self, k8s_service, max_upload_size: int = 100 * 1024 * 1024):
        self.k8s_service = k8s_service
        self.max_upload_size = max_upload_size  # 上传文件大小限制，默认100MB
        self.target_dir = "/tmp"  # 目标目录

    @log_async_function_call(upload_logger)
    async def validate_file(self, file: UploadFile) -> str:
        """验证上传的文件"""
        original_filename = file.filename

        # 检查文件名
        if (
            not original_filename
            or "/" in original_filename
            or "\\" in original_filename
        ):
            error_msg = f"无效的文件名 '{original_filename}'。文件名不能为空且不能包含路径分隔符。"
            upload_logger.error(error_msg)
            raise FileValidationError(error_msg)

        # 使用安全的文件名
        safe_filename = os.path.basename(original_filename)
        if not safe_filename:
            error_msg = f"处理后的文件名无效 '{original_filename}' -> '{safe_filename}'"
            upload_logger.error(error_msg)
            raise FileValidationError(error_msg)

        # 检查文件大小
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)
        if file_size > self.max_upload_size:
            error_msg = (
                f"文件 '{original_filename}' 大小为 {file_size} 字节，超过限制 "
                f"{self.max_upload_size} 字节"
            )
            upload_logger.error(error_msg)
            raise FileValidationError(error_msg)
        if file_size == 0:
            error_msg = f"文件 '{original_filename}' 为空，不允许上传空文件"
            upload_logger.error(error_msg)
            raise FileValidationError(error_msg)

        upload_logger.info(f"文件验证通过: {safe_filename}, 大小: {file_size} 字节")
        return safe_filename

    @log_async_function_call(upload_logger)
    async def save_temp_file(self, file: UploadFile) -> str:
        """保存临时文件"""
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                shutil.copyfileobj(file.file, tmp_file)
                tmp_file_path = tmp_file.name

            upload_logger.info(f"文件已临时保存到: {tmp_file_path}")
            return tmp_file_path

        except Exception as e:
            error_msg = f"保存临时文件失败: {e}"
            upload_logger.error(error_msg)
            raise FileTransferError(error_msg)

    @log_async_function_call(upload_logger)
    async def cleanup_temp_file(self, tmp_file_path: str) -> None:
        """清理临时文件"""
        try:
            if os.path.exists(tmp_file_path):
                os.remove(tmp_file_path)
                upload_logger.info(f"临时文件 {tmp_file_path} 已删除")
        except Exception as e:
            upload_logger.error(f"删除临时文件失败: {e}")

    @log_async_function_call(upload_logger)
    async def upload_file(
        self, namespace: str, podname: str, file: UploadFile
    ) -> FileUploadResponse:
        """上传文件到Pod - 完全按照原版本main.py的实现"""
        tmp_file_path = None

        if not self.k8s_service.core_v1:
            error_msg = "Kubernetes 客户端未初始化，无法上传文件"
            upload_logger.error(error_msg)
            return FileUploadResponse(error=error_msg, status_code=500)

        try:
            # 验证文件
            safe_filename = await self.validate_file(file)

            upload_logger.info(
                f"接收到文件上传请求: namespace={namespace}, podname={podname}, "
                f"original_filename={file.filename}, safe_filename={safe_filename}, "
                f"destination={self.target_dir}/{safe_filename}"
            )

            # 保存临时文件
            tmp_file_path = await self.save_temp_file(file)

            pod_file_path = os.path.join(self.target_dir, safe_filename)

            try:
                # 创建目标目录 (如果不存在)
                mkdir_command = ["mkdir", "-p", self.target_dir]
                resp_mkdir = kubernetes.stream.stream(
                    self.k8s_service.core_v1.connect_get_namespaced_pod_exec,
                    podname,
                    namespace,
                    command=mkdir_command,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                    _preload_content=False,
                )
                while resp_mkdir.is_open():
                    resp_mkdir.update(timeout=1)
                    if resp_mkdir.peek_stdout():
                        upload_logger.info(f"MKDIR STDOUT: {resp_mkdir.read_stdout()}")
                    if resp_mkdir.peek_stderr():
                        upload_logger.warning(
                            f"MKDIR STDERR: {resp_mkdir.read_stderr()}"
                        )
                resp_mkdir.close()
                if resp_mkdir.returncode != 0:
                    # 即使创建目录失败，也尝试复制，tar可能会创建目录
                    upload_logger.warning(
                        f"在 Pod 中创建目录 {self.target_dir} 可能失败，返回码: {resp_mkdir.returncode}"
                    )

                # 创建一个包含单个文件的 tar 归档流
                tar_stream = io.BytesIO()
                with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                    tar.add(tmp_file_path, arcname=safe_filename)
                tar_stream.seek(0)

                # 在 Pod 中执行 tar 命令以提取文件
                exec_command = ["tar", "xf", "-", "-C", self.target_dir]

                resp_cp = kubernetes.stream.stream(
                    self.k8s_service.core_v1.connect_get_namespaced_pod_exec,
                    podname,
                    namespace,
                    command=exec_command,
                    stderr=True,
                    stdin=True,
                    stdout=True,
                    tty=False,
                    _preload_content=False,
                )

                upload_logger.info(
                    f"向 Pod {podname} 的 {self.target_dir} 目录传输文件 {safe_filename} (目标名: {safe_filename})"
                )

                # 将 tar 流写入到 Pod 的 stdin
                buffer_size = 4096  # 4KB 缓冲区
                while True:
                    data_chunk = tar_stream.read(buffer_size)
                    if not data_chunk:
                        break
                    resp_cp.write_channel(ws_client.STDIN_CHANNEL, data_chunk)

                tar_stream.close()

                # 等待命令完成并检查输出/错误
                while resp_cp.is_open():
                    resp_cp.update(timeout=1)
                    if resp_cp.peek_stdout():
                        upload_logger.info(f"CP STDOUT: {resp_cp.read_stdout()}")
                    if resp_cp.peek_stderr():
                        stderr_output = resp_cp.read_stderr()
                        upload_logger.warning(f"CP STDERR: {stderr_output}")
                resp_cp.close()

                if resp_cp.returncode != 0:
                    error_msg = (
                        f"在 Pod 中复制文件失败。Tar 命令返回码: {resp_cp.returncode}"
                    )
                    upload_logger.error(error_msg)
                    return FileUploadResponse(error=error_msg, status_code=500)

                upload_logger.info(
                    f"文件 {safe_filename} 已成功上传到 Pod {podname} 的 {pod_file_path}"
                )
                return FileUploadResponse(
                    message=f"文件 {safe_filename} 已成功上传到 {pod_file_path}"
                )

            except kubernetes.client.exceptions.ApiException as e:
                error_msg = f"Kubernetes API 错误: {e}"
                upload_logger.error(error_msg)
                return FileUploadResponse(error=error_msg, status_code=500)
            except Exception as e:
                error_msg = f"上传文件到 Pod 时发生意外错误: {e}"
                upload_logger.error(error_msg)
                return FileUploadResponse(error=error_msg, status_code=500)

        except (
            FileValidationError,
            FileTransferError,
            K8sConnectionError,
            PodConnectionError,
        ) as e:
            upload_logger.error(f"文件上传失败: {e}")
            return FileUploadResponse(error=str(e), status_code=400)

        except Exception as e:
            error_msg = f"处理文件上传时发生未知错误: {e}"
            upload_logger.error(error_msg)
            return FileUploadResponse(error=error_msg, status_code=500)

        finally:
            # 清理临时文件
            if tmp_file_path:
                await self.cleanup_temp_file(tmp_file_path)


# 创建文件上传服务实例的工厂函数
def create_upload_service(k8s_service) -> FileUploadService:
    """创建文件上传服务实例"""
    return FileUploadService(k8s_service)
