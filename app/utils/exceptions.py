"""
自定义异常模块
定义项目中使用的所有自定义异常类
"""


class BaseAppException(Exception):
    """应用基础异常类"""

    def __init__(self, message: str, error_code: str = None):
        self.message = message
        self.error_code = error_code
        super().__init__(self.message)


class DatabaseException(BaseAppException):
    """数据库相关异常"""

    pass


class DatabaseConnectionError(DatabaseException):
    """数据库连接错误"""

    def __init__(self, message: str = "数据库连接失败"):
        super().__init__(message, "DB_CONNECTION_ERROR")


class DatabaseOperationError(DatabaseException):
    """数据库操作错误"""

    def __init__(self, message: str = "数据库操作失败"):
        super().__init__(message, "DB_OPERATION_ERROR")


class KubernetesException(BaseAppException):
    """Kubernetes相关异常"""

    pass


class K8sConnectionError(KubernetesException):
    """Kubernetes连接错误"""

    def __init__(self, message: str = "Kubernetes连接失败"):
        super().__init__(message, "K8S_CONNECTION_ERROR")


class K8sConfigError(KubernetesException):
    """Kubernetes配置错误"""

    def __init__(self, message: str = "Kubernetes配置错误"):
        super().__init__(message, "K8S_CONFIG_ERROR")


class PodNotFoundError(KubernetesException):
    """Pod未找到错误"""

    def __init__(self, podname: str, namespace: str):
        message = f"Pod '{podname}' 在命名空间 '{namespace}' 中未找到"
        super().__init__(message, "POD_NOT_FOUND")


class PodConnectionError(KubernetesException):
    """Pod连接错误"""

    def __init__(self, podname: str, namespace: str, reason: str = ""):
        message = f"无法连接到Pod '{podname}' (命名空间: '{namespace}')"
        if reason:
            message += f": {reason}"
        super().__init__(message, "POD_CONNECTION_ERROR")


class WebSocketException(BaseAppException):
    """WebSocket相关异常"""

    pass


class WebSocketConnectionError(WebSocketException):
    """WebSocket连接错误"""

    def __init__(self, message: str = "WebSocket连接失败"):
        super().__init__(message, "WS_CONNECTION_ERROR")


class WebSocketTimeoutError(WebSocketException):
    """WebSocket超时错误"""

    def __init__(self, message: str = "WebSocket连接超时"):
        super().__init__(message, "WS_TIMEOUT_ERROR")


class FileUploadException(BaseAppException):
    """文件上传相关异常"""

    pass


class FileValidationError(FileUploadException):
    """文件验证错误"""

    def __init__(self, message: str = "文件验证失败"):
        super().__init__(message, "FILE_VALIDATION_ERROR")


class FileTransferError(FileUploadException):
    """文件传输错误"""

    def __init__(self, message: str = "文件传输失败"):
        super().__init__(message, "FILE_TRANSFER_ERROR")


class ConfigurationException(BaseAppException):
    """配置相关异常"""

    pass


class ConfigNotFoundError(ConfigurationException):
    """配置文件未找到错误"""

    def __init__(self, config_path: str):
        message = f"配置文件未找到: {config_path}"
        super().__init__(message, "CONFIG_NOT_FOUND")


class InvalidConfigError(ConfigurationException):
    """无效配置错误"""

    def __init__(self, message: str = "配置无效"):
        super().__init__(message, "INVALID_CONFIG")


class ValidationException(BaseAppException):
    """验证异常"""

    pass


class ParameterValidationError(ValidationException):
    """参数验证错误"""

    def __init__(self, parameter: str, message: str = ""):
        full_message = f"参数 '{parameter}' 验证失败"
        if message:
            full_message += f": {message}"
        super().__init__(full_message, "PARAMETER_VALIDATION_ERROR")


class AuthenticationException(BaseAppException):
    """认证相关异常"""

    pass


class UnauthorizedError(AuthenticationException):
    """未授权错误"""

    def __init__(self, message: str = "未授权访问"):
        super().__init__(message, "UNAUTHORIZED")


class ForbiddenError(AuthenticationException):
    """禁止访问错误"""

    def __init__(self, message: str = "访问被禁止"):
        super().__init__(message, "FORBIDDEN")


class ServiceException(BaseAppException):
    """服务相关异常"""

    pass


class ServiceUnavailableError(ServiceException):
    """服务不可用错误"""

    def __init__(self, service_name: str):
        message = f"服务 '{service_name}' 不可用"
        super().__init__(message, "SERVICE_UNAVAILABLE")


class InternalServerError(ServiceException):
    """内部服务器错误"""

    def __init__(self, message: str = "内部服务器错误"):
        super().__init__(message, "INTERNAL_SERVER_ERROR")
