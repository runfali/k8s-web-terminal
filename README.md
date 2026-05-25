# K8S Web Terminal

通过浏览器 WebSocket 连接 Kubernetes Pod 的在线终端工具，支持文件上传和操作日志记录。

## 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.8+ | 运行环境 |
| FastAPI | 0.104.1 | HTTP + WebSocket 框架 |
| Uvicorn | 0.23.2 | ASGI 服务器 |
| kubernetes | 17.17.0 | K8s API 客户端 |
| websockets | 14.1 | WebSocket 协议支持 |
| asyncpg | >=0.27.0 | PostgreSQL 异步驱动 |
| Jinja2 | >=3.1.0 | HTML 模板渲染 |
| python-multipart | 0.0.6 | 文件上传解析 |
| pydantic | >=2.0.0 | 数据校验 |

前端：Xterm.js + xterm-addon-fit（静态文件内置于 `templates/static/`）

## 项目结构

```
k8s-web-terminal/
├── main.py                          # 应用入口，FastAPI 实例化 + 中间件 + 路由注册
├── app/
│   ├── config.py                    # 配置管理（环境变量覆盖所有参数）
│   ├── models.py                    # Pydantic 数据模型
│   ├── api/
│   │   └── terminal.py              # HTTP / WebSocket 路由（/connect, /ws, /upload 等）
│   ├── handlers/
│   │   └── websocket_handler.py     # WebSocket ↔ Pod 双向通信 + 心跳 + 超时管理
│   ├── services/
│   │   ├── database.py              # PostgreSQL 连接池 + terminal_logs 表操作
│   │   ├── k8s_service.py           # K8s 客户端 + 证书持久化 + Pod 缓存
│   │   └── upload_service.py        # 文件上传（tar 流写入 Pod /tmp）
│   └── utils/
│       ├── exceptions.py            # 自定义异常层级（DB/K8s/WS/文件/认证）
│       └── logger.py                # 统一日志 + 装饰器
├── templates/
│   ├── terminal.html                # Web 终端 UI
│   └── static/                      # Xterm.js 及其 addon 静态文件
├── config/
│   └── config                       # kubeconfig 文件（gitignore 外）
├── requirements.txt                 # 生产依赖
└── requirements-dev.txt             # 开发/测试/代码质量工具
```

## 快速开始

### 环境依赖

- Python 3.8+
- Kubernetes 集群访问权限（kubeconfig）
- PostgreSQL 12+（可选，不启用则跳过日志记录）

### 安装

```bash
# 创建虚拟环境
python -m venv venv && source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 配置

**1. kubeconfig**

将集群的 kubeconfig 文件放到 `config/config`，权限设为 `600`：

```bash
mkdir -p config
cp ~/.kube/config config/config
chmod 600 config/config
```

启动时会自动将 kubeconfig 中的 Base64 证书数据解码写入 `config/certs/` 目录（持久化），避免依赖 `/tmp` 临时文件。

**2. 环境变量（可选，均有默认值）**

```bash
# 数据库
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=your_password
export POSTGRES_DB=k8s_terminal

# 服务
export SERVER_HOST=0.0.0.0
export SERVER_PORT=8006

# K8s
export K8S_VERIFY_SSL=false   # 生产环境建议设为 true

# 日志
export LOG_LEVEL=INFO
export LOG_DIR=logs
```

完整配置项见 `app/config.py`。

**3. 数据库初始化（使用日志功能时需要）**

```sql
CREATE DATABASE k8s_terminal;
-- 应用启动时会自动创建 terminal_logs 表，无需手动建表
```

### 启动

```bash
# 开发模式
python main.py

# 生产模式
uvicorn main:app --host 0.0.0.0 --port 8006 --workers 4
```

多 worker 安全：每个 WebSocket 连接都是独立的 K8s exec stream（连到不同 Pod 的独立 bash 会话），不存在多终端共享同一会话的场景，无需 sticky session。`workers` 数量可通过 `SERVER_WORKERS` 环境变量控制。

## 使用指南

### 访问终端

浏览器打开：

```
http://<host>:8006/connect?chinesename=<用户名>&podname=<Pod名称>&namespace=<命名空间>
```

| 参数 | 必选 | 说明 |
|------|------|------|
| `chinesename` | 是 | 用户名，记入操作日志 |
| `podname` | 是 | Pod 完整名称 |
| `namespace` | 是 | Pod 所在命名空间 |

页面加载后会自动建立 WebSocket 连接到 Pod 的 `/bin/bash`，终端断开时自动重连（最多 10 次，指数退避）。

### WebSocket 端点

```
ws://<host>:8006/ws/{namespace}/{podname}?chinesename=<用户名>
```

支持 PTY resize 消息（终端窗口大小变化自动同步到 Pod）。

### 文件上传

**拖拽上传**：将文件拖到终端区域，确认后上传到 Pod 的 `/tmp/` 目录。

**API 上传**：

```bash
curl -X POST "http://<host>:8006/upload/{namespace}/{podname}" \
  -F "file=@/local/path/file.txt"
```

文件大小限制：100MB。上传通过 tar 流写入 Pod，自动创建目标目录。

### 健康检查

```bash
curl http://<host>:8006/health
# {"status":"healthy","database":"connected","kubernetes":"connected"}
```

### API 文档

- Swagger UI: `http://<host>:8006/docs`
- ReDoc: `http://<host>:8006/redoc`

### 查看日志

```bash
tail -f logs/terminal.log
```

或查询数据库：

```sql
SELECT * FROM terminal_logs ORDER BY connection_time DESC LIMIT 10;
```

## 近期优化摘要

- **SSL 证书持久化**：启动时将 kubeconfig 中 Base64 证书写入 `config/certs/`，避免 `/tmp` 清理导致 `SSLError(FileNotFoundError)`
- **Pod 存在性缓存**：带 TTL 的内存缓存（命中 5 分钟 / 未命中 30 秒），减少 K8s API 调用
- **前端内存管理**：终端写入队列限长 500~1000 条，防止长时间运行内存泄漏
- **数据传输优化**：后端读缓冲区 8192 字节 + 消息批量合并刷新（~60fps），减少系统调用
- **输入防抖**：用户输入 50ms 窗口内批量发送，粘贴长文本自动分块投递
- **重连机制**：指数退避重连，最多 10 次，避免惊群

## 安全注意事项

- kubeconfig 文件权限应为 `600`，防止被其他用户读取
- 生产环境建议 `K8S_VERIFY_SSL=true` 启用证书校验
- 服务不包含认证机制，请部署在内网并通过防火墙限制访问来源
- CORS 默认允许所有来源（`*`），如有需要设置 `CORS_ORIGINS` 环境变量
- 文件上传大小限制 100MB，可配合 `BodySizeLimitMiddleware` 在 ASGI 层二次拦截
