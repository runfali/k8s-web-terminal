"""
Kubernetes服务模块
管理与Kubernetes集群的连接和操作
"""

import os
import kubernetes
from kubernetes.client import Configuration as K8sConfiguration
from kubernetes.stream import ws_client
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
        """检查Pod是否存在"""
        if not self.core_v1:
            raise K8sConnectionError("Kubernetes客户端未初始化")

        try:
            self.core_v1.read_namespaced_pod(name=podname, namespace=namespace)
            return True
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
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

    @log_function_call(k8s_logger)
    def create_exec_stream(self, connection_info: K8sConnectionInfo):
        """创建Pod执行流"""
        if not self.core_v1:
            raise K8sConnectionError("Kubernetes客户端未初始化")

        try:
            # 检查Pod是否存在
            if not self.check_pod_exists(
                connection_info.namespace, connection_info.podname
            ):
                raise PodNotFoundError(
                    connection_info.podname, connection_info.namespace
                )

            # 创建执行流
            resp = kubernetes.stream.stream(
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

            k8s_logger.info(f"已成功连接到 {connection_info.podname} 的 Pod 执行流")
            return resp

        except kubernetes.client.exceptions.ApiException as e:
            error_msg = f"连接到 Pod 执行流时出错: {e}"
            k8s_logger.error(error_msg)
            raise PodConnectionError(
                connection_info.podname, connection_info.namespace, str(e)
            )
        except Exception as e:
            error_msg = f"在与 Pod 建立连接时发生意外错误: {e}"
            k8s_logger.error(error_msg)
            raise PodConnectionError(
                connection_info.podname, connection_info.namespace, str(e)
            )

    def is_connected(self) -> bool:
        """检查Kubernetes是否连接"""
        return self.api_client is not None and self.core_v1 is not None


# 全局Kubernetes服务实例
k8s_service = KubernetesService()
