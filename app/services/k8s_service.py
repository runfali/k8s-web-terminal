"""
Kubernetes服务模块
管理与Kubernetes集群的连接和操作
"""

import os
import time
import asyncio
import kubernetes
from kubernetes.client import Configuration as K8sConfiguration
from typing import Optional
import base64
import re
from ..config import config
from ..models import K8sConnectionInfo
from ..utils.exceptions import (
    K8sConnectionError,
    K8sConfigError,
    PodNotFoundError,
    PodConnectionError,
)
from ..utils.logger import k8s_logger, log_async_function_call, log_function_call


class KubernetesService:
    """Kubernetes服务类"""

    def __init__(self):
        self.api_client: Optional[kubernetes.client.ApiClient] = None
        self.core_v1: Optional[kubernetes.client.CoreV1Api] = None
        self._pod_cache = {}  # Pod信息缓存
        self._cache_ttl = 300  # 缓存有效期5分钟

    @log_function_call(k8s_logger)
    def initialize(self) -> None:
        """初始化Kubernetes客户端"""
        try:
            if not os.path.exists(config.k8s.config_file):
                raise K8sConfigError(
                    f"Kubernetes配置文件未找到: {config.k8s.config_file}"
                )

            # 1) 优先确保证书持久化，避免临时文件被系统清理
            cert_paths = self._ensure_persistent_cert_files()

            # 2) 尝试常规方式加载Kube配置
            try:
                kubernetes.config.load_kube_config(config_file=config.k8s.config_file)
            except Exception as e:
                # 如果常规加载失败（通常是临时证书文件问题），记录并继续使用手动配置
                k8s_logger.warning(f"常规load_kube_config失败，尝试手动配置: {e}")

            # 获取配置并自定义
            try:
                k8s_client_config = K8sConfiguration.get_default_copy()
            except Exception:
                # 如果默认配置不可用，创建一个新的配置实例
                k8s_client_config = K8sConfiguration()

            # 基本SSL行为
            k8s_client_config.verify_ssl = config.k8s.verify_ssl

            # 如果存在持久化证书，强制使用这些文件，避免/tmp下的临时文件
            if cert_paths.get("ssl_ca_cert"):
                k8s_client_config.ssl_ca_cert = cert_paths["ssl_ca_cert"]
            if cert_paths.get("cert_file"):
                k8s_client_config.cert_file = cert_paths["cert_file"]
            if cert_paths.get("key_file"):
                k8s_client_config.key_file = cert_paths["key_file"]

            # 如果未设置host（手动配置场景），从配置文件中解析server
            if not getattr(k8s_client_config, "host", None):
                server = self._extract_value_from_kubeconfig("server")
                if server:
                    k8s_client_config.host = server.strip()

            # 配置WebSocket参数
            k8s_client_config.websocket_client_params = {
                "ping_interval": config.k8s.ping_interval,
                "ping_timeout": config.k8s.ping_timeout,
                "max_size": config.k8s.max_size,
                "skip_utf8_validation": config.k8s.skip_utf8_validation,
                "close_timeout": config.k8s.close_timeout,
            }

            self.api_client = kubernetes.client.ApiClient(
                configuration=k8s_client_config
            )
            self.core_v1 = kubernetes.client.CoreV1Api(api_client=self.api_client)

            k8s_logger.info("Kubernetes配置已成功加载和自定义（使用持久化证书文件）")

        except Exception as e:
            k8s_logger.error(f"Kubernetes初始化失败: {e}")
            raise K8sConnectionError(f"Kubernetes初始化失败: {e}")

    @log_function_call(k8s_logger)
    def _ensure_persistent_cert_files(self) -> dict:
        """确保证书数据持久化到稳定路径，避免/tmp临时文件被清理
        返回持久化后的文件路径字典，例如：{"ssl_ca_cert": path, "cert_file": path, "key_file": path}
        """
        cert_paths: dict = {}
        try:
            config_text = ""
            with open(config.k8s.config_file, "r", encoding="utf-8") as f:
                config_text = f.read()

            def extract_value(key: str) -> Optional[str]:
                # 简单的YAML行级解析，适用于当前单集群/单用户结构
                m = re.search(rf"{key}:\s*(.+)", config_text)
                if not m:
                    return None
                val = m.group(1).strip()
                # 去除可能的引号
                return val.strip('"').strip("'")

            ca_data_b64 = extract_value("certificate-authority-data")
            client_cert_b64 = extract_value("client-certificate-data")
            client_key_b64 = extract_value("client-key-data")

            cert_dir = os.path.join(os.path.dirname(config.k8s.config_file), "certs")
            os.makedirs(cert_dir, exist_ok=True)

            if ca_data_b64 and not ca_data_b64.startswith("/"):
                try:
                    ca_bytes = base64.b64decode(ca_data_b64)
                    ca_path = os.path.join(cert_dir, "ca.crt")
                    with open(ca_path, "wb") as f:
                        f.write(ca_bytes)
                    cert_paths["ssl_ca_cert"] = ca_path
                except Exception as e:
                    k8s_logger.warning(f"写入CA证书失败，将继续尝试使用默认行为: {e}")

            if client_cert_b64 and not client_cert_b64.startswith("/"):
                try:
                    crt_bytes = base64.b64decode(client_cert_b64)
                    crt_path = os.path.join(cert_dir, "client.crt")
                    with open(crt_path, "wb") as f:
                        f.write(crt_bytes)
                    cert_paths["cert_file"] = crt_path
                except Exception as e:
                    k8s_logger.warning(f"写入客户端证书失败，继续默认行为: {e}")

            if client_key_b64 and not client_key_b64.startswith("/"):
                try:
                    key_bytes = base64.b64decode(client_key_b64)
                    key_path = os.path.join(cert_dir, "client.key")
                    with open(key_path, "wb") as f:
                        f.write(key_bytes)
                    cert_paths["key_file"] = key_path
                except Exception as e:
                    k8s_logger.warning(f"写入客户端私钥失败，继续默认行为: {e}")
        except Exception as e:
            k8s_logger.warning(f"读取或解析Kubernetes配置证书数据失败: {e}")
        return cert_paths

    def _extract_value_from_kubeconfig(self, key: str) -> Optional[str]:
        """从kubeconfig文本中提取简单的键值（行级），用于server等字段"""
        try:
            with open(config.k8s.config_file, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(rf"{key}:\s*(.+)", text)
            if m:
                return m.group(1).strip().strip('"').strip("'")
        except Exception:
            pass
        return None

    @log_function_call(k8s_logger)
    def reinitialize(self) -> None:
        """重新初始化Kubernetes客户端

        当SSL证书临时文件被清理导致连接失败时，调用此方法重新初始化连接
        """
        k8s_logger.info("正在重新初始化Kubernetes客户端...")

        # 先关闭现有连接
        if self.api_client:
            try:
                self.api_client.close()
            except Exception as e:
                k8s_logger.warning(f"关闭旧连接时出错: {e}")

        # 重新初始化前也确保证书持久化，避免再次出现/tmp临时文件问题
        self._ensure_persistent_cert_files()
        self.api_client = None
        self.core_v1 = None
        self.initialize()
        k8s_logger.info("Kubernetes客户端已成功重新初始化")

    @log_function_call(k8s_logger)
    def close(self) -> None:
        """关闭Kubernetes客户端"""
        if self.api_client:
            self.api_client.close()
            k8s_logger.info("Kubernetes API客户端已关闭")

    @log_function_call(k8s_logger)
    def check_pod_exists(self, namespace: str, podname: str) -> bool:
        """检查Pod是否存在（带缓存）"""
        if not self.core_v1:
            raise K8sConnectionError("Kubernetes客户端未初始化")

        # 检查缓存
        cache_key = f"{namespace}:{podname}"
        current_time = time.time()

        if cache_key in self._pod_cache:
            cached_result, cache_time = self._pod_cache[cache_key]
            if current_time - cache_time < self._cache_ttl:
                k8s_logger.debug(f"Pod存在性检查命中缓存: {namespace}/{podname}")
                return cached_result

        # 最多尝试2次（初始尝试 + 1次重试）
        max_retries = 1
        retry_count = 0

        while True:
            try:
                self.core_v1.read_namespaced_pod(name=podname, namespace=namespace)
                # 缓存结果
                self._pod_cache[cache_key] = (True, current_time)
                return True
            except kubernetes.client.exceptions.ApiException as e:
                if e.status == 404:
                    # 缓存结果
                    self._pod_cache[cache_key] = (False, current_time)
                    return False
                else:
                    k8s_logger.error(f"检查Pod存在性时出错: {e}")
                    raise K8sConnectionError(f"检查Pod时出错: {e}")
            except Exception as e:
                # 检查是否为SSL错误
                error_str = str(e)
                if (
                    "SSLError" in error_str
                    and "FileNotFoundError" in error_str
                    and retry_count < max_retries
                ):
                    k8s_logger.warning(
                        f"检测到SSL证书文件错误，尝试重新初始化连接: {e}"
                    )
                    retry_count += 1
                    try:
                        # 重新初始化连接
                        self.reinitialize()
                        # 继续循环，重新尝试
                        continue
                    except Exception as reinit_error:
                        k8s_logger.error(f"重新初始化连接失败: {reinit_error}")
                        raise K8sConnectionError(
                            f"SSL错误且重新初始化失败: {reinit_error}"
                        )
                else:
                    # 其他错误或已达到最大重试次数
                    k8s_logger.error(f"检查Pod存在性时发生未处理错误: {e}")
                    raise K8sConnectionError(f"检查Pod时发生未处理错误: {e}")

    @log_function_call(k8s_logger)
    def get_pod_info(self, namespace: str, podname: str) -> dict:
        """获取Pod信息"""
        if not self.core_v1:
            raise K8sConnectionError("Kubernetes客户端未初始化")

        try:
            pod = self.core_v1.read_namespaced_pod(name=podname, namespace=namespace)
            return {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase,
                "node": pod.spec.node_name,
                "creation_time": pod.metadata.creation_timestamp,
                "labels": pod.metadata.labels or {},
                "containers": [container.name for container in pod.spec.containers],
            }
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                raise PodNotFoundError(podname, namespace)
            else:
                k8s_logger.error(f"获取Pod信息时出错: {e}")
                raise K8sConnectionError(f"获取Pod信息时出错: {e}")

    @log_async_function_call(k8s_logger)
    async def create_exec_stream(self, connection_info: K8sConnectionInfo):
        """创建Pod执行流"""
        if not self.core_v1:
            raise K8sConnectionError("Kubernetes客户端未初始化")

        # 最多尝试2次（初始尝试 + 1次重试）
        max_retries = 1
        retry_count = 0

        while True:
            try:
                # 首先检查Pod是否存在
                if not self.check_pod_exists(
                    connection_info.namespace, connection_info.podname
                ):
                    raise PodNotFoundError(
                        connection_info.podname, connection_info.namespace
                    )

                # 创建执行流 - 使用正确的stream函数
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: kubernetes.stream.stream(
                        self.core_v1.connect_get_namespaced_pod_exec,
                        connection_info.podname,
                        connection_info.namespace,
                        command=connection_info.command,
                        stderr=True,
                        stdin=True,
                        stdout=True,
                        tty=True,
                        _preload_content=False,
                    ),
                )

                k8s_logger.info(
                    f"成功创建到Pod {connection_info.podname} 的执行流，命名空间：{connection_info.namespace}"
                )
                return resp

            except PodNotFoundError:
                raise
            except Exception as e:
                # 检查是否为SSL错误
                error_str = str(e)
                if (
                    "SSLError" in error_str
                    and "FileNotFoundError" in error_str
                    and retry_count < max_retries
                ):
                    k8s_logger.warning(
                        f"创建执行流时检测到SSL证书文件错误，尝试重新初始化连接: {e}"
                    )
                    retry_count += 1
                    try:
                        # 重新初始化连接
                        self.reinitialize()
                        # 继续循环，重新尝试
                        continue
                    except Exception as reinit_error:
                        k8s_logger.error(f"重新初始化连接失败: {reinit_error}")
                        raise K8sConnectionError(
                            f"SSL错误且重新初始化失败: {reinit_error}"
                        )
                else:
                    # 其他错误或已达到最大重试次数
                    k8s_logger.error(f"创建Pod执行流失败: {e}")
                    raise PodConnectionError(
                        connection_info.podname,
                        connection_info.namespace,
                        f"创建Pod执行流失败: {e}",
                    )

    def is_connected(self) -> bool:
        """检查Kubernetes是否连接"""
        return self.api_client is not None and self.core_v1 is not None


# 全局Kubernetes服务实例
k8s_service = KubernetesService()
