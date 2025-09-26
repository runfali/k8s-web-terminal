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

            # 加载Kubernetes配置
            kubernetes.config.load_kube_config(config_file=config.k8s.config_file)

            # 获取配置并自定义
            k8s_client_config = K8sConfiguration.get_default_copy()
            k8s_client_config.verify_ssl = config.k8s.verify_ssl

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

            k8s_logger.info("Kubernetes配置已成功加载和自定义")

        except Exception as e:
            k8s_logger.error(f"Kubernetes初始化失败: {e}")
            raise K8sConnectionError(f"Kubernetes初始化失败: {e}")

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

        try:
            # 首先检查Pod是否存在
            if not self.check_pod_exists(connection_info.namespace, connection_info.podname):
                raise PodNotFoundError(connection_info.podname, connection_info.namespace)

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
                )
            )

            k8s_logger.info(
                f"成功创建到Pod {connection_info.podname} 的执行流，命名空间：{connection_info.namespace}"
            )
            return resp

        except PodNotFoundError:
            raise
        except Exception as e:
            k8s_logger.error(f"创建Pod执行流失败: {e}")
            raise PodConnectionError(connection_info.podname, connection_info.namespace, f"创建Pod执行流失败: {e}")

    def is_connected(self) -> bool:
        """检查Kubernetes是否连接"""
        return self.api_client is not None and self.core_v1 is not None


# 全局Kubernetes服务实例
k8s_service = KubernetesService()
