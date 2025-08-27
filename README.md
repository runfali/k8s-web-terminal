# K8S Web Terminal

一个通过 Web 浏览器访问 Kubernetes Pod 终端的工具。

## 主要功能

- 通过 WebSocket 安全连接到 Kubernetes Pod。
- 在浏览器中提供一个功能齐全的交互式终端界面 (基于 Xterm.js)。
- 支持从用户本地上传文件到正在连接的 Pod 内的指定路径。
- 支持终端大小自适应浏览器窗口。
- 记录用户连接终端的日志到 PostgreSQL 数据库 (可选功能，可配置)。
- 使用 FastAPI 构建的异步后端，性能高效。
- 简洁的前端界面，易于使用。

## 技术栈

- **后端**:
  - Python 3.x
  - FastAPI: 高性能的 Python Web 框架。
  - Uvicorn: ASGI 服务器，用于运行 FastAPI 应用。
  - Kubernetes Python Client: 用于与 Kubernetes API 交互。
  - Websockets: 实现浏览器与后端之间的实时双向通信。
  - Asyncpg: 异步 PostgreSQL 驱动程序，用于数据库操作。
- **前端**:
  - HTML5
  - CSS3
  - JavaScript
  - Xterm.js: 功能强大的 Web 终端模拟器。
  - Xterm-addon-fit: Xterm.js 插件，用于终端大小自适应。
- **数据库** (可选，用于日志记录):
  - PostgreSQL

## 项目结构

```
.k8s_web_terminal/
├── config/
│   └── config        # Kubernetes 集群配置文件
├── main.py           # FastAPI 应用主文件
├── requirements.txt  # Python 依赖列表
├── templates/
│   ├── static/
│   │   ├── xterm-addon-fit.js
│   │   ├── xterm.css
│   │   └── xterm.js
│   └── terminal.html # 终端前端页面模板
└── README.md         # 项目说明文件
```

## 安装与运行

1.  **克隆仓库** (如果您是从 git 仓库获取):

    ```bash
    git clone <repository_url>
    cd k8s_web_terminal
    ```

2.  **创建并激活虚拟环境** (推荐):

    ```bash
    python -m venv venv
    # Windows
    .\venv\Scripts\activate
    # macOS/Linux
    source venv/bin/activate
    ```

3.  **安装依赖**:
    确保您的 `pip` 是最新版本，然后运行：

    ```bash
    pip install -r requirements.txt
    ```

4.  **配置 Kubernetes**:

    - 将您的 Kubernetes 集群的 `config` 文件放置在项目根目录下的 `config/` 文件夹内。
    - 确保该配置文件具有访问目标 Pod 的必要权限。

5.  **配置数据库环境变量** (可选，如果需要日志记录功能):
    应用会尝试连接到 PostgreSQL 数据库来记录终端连接。请设置以下环境变量：

    - `POSTGRES_HOST`: PostgreSQL 服务器地址 (默认: `10.200.1.171`)
    - `POSTGRES_PORT`: PostgreSQL 服务器端口 (默认: `5432`)
    - `POSTGRES_USER`: PostgreSQL 用户名 (默认: `kube`)
    - `POSTGRES_PASSWORD`: PostgreSQL 密码 (默认: `kube`)
    - `POSTGRES_DB`: PostgreSQL 数据库名称 (默认: `kube`)
      如果环境变量未设置，将使用代码中定义的默认值。如果数据库连接失败，日志功能将不可用，但核心终端功能仍可正常工作。

6.  **运行应用**:
    在项目根目录下运行以下命令启动 FastAPI 应用：
    ```bash
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
    ```
    - `--reload`: 开发模式下，代码更改后自动重启服务。
    - `--host 0.0.0.0`: 使服务可以从本地网络访问。
    - `--port 8000`: 指定服务监听的端口。

## 使用方法

应用启动后，通过浏览器访问以下 URL 格式连接到指定的 Pod：

`http://<your_server_address>:8000/connect?chinesename=<username>&podname=<pod_name>&namespace=<namespace>`

例如:
`http://localhost:8000/connect?chinesename=testuser&podname=my-nginx-pod-12345&namespace=default`

参数说明:

- `chinesename`: 用户名 (用于日志记录，可以是任意字符串)。
- `podname`: 您要连接的 Pod 的名称。
- `namespace`: Pod 所在的 Kubernetes 命名空间。

成功连接后，您将在浏览器中看到一个交互式终端。

### 文件上传

在终端界面，您可以将文件拖拽到终端区域以上传文件。上传的文件将被放置在 Pod 内的 `/tmp/` 目录下。

## 注意事项

- **安全**: 请确保您的 Kubernetes `config` 文件得到妥善保管，并且不要将此服务暴露在不受信任的网络中，除非您已采取适当的安全措施。
- **SSL/TLS**: 当前 `main.py` 中的 Kubernetes Python Client 配置禁用了 SSL 验证 (`k8s_client_config.verify_ssl = False`)。在生产环境中，强烈建议启用 SSL 验证并正确配置证书。
- **错误处理**: 应用包含基本的错误处理，例如 K8s 配置未找到或数据库连接问题，但可以根据需要进一步增强。

## 贡献指南

欢迎提交 Pull Requests 或 Issues 来改进此项目。
