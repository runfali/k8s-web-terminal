from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState  # 导入 WebSocketState
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware  # 导入 CORS 中间件
import kubernetes
from kubernetes.client import Configuration as K8sConfiguration  # 导入 K8s 配置类
from kubernetes.stream import ws_client  # 导入 ws_client 以便使用 STDIN_CHANNEL 等常量
import os
import asyncio
import asyncpg  # 导入 asyncpg 用于异步 PostgreSQL 操作
from datetime import datetime  # 导入 datetime 用于时间戳

# import pty  # pty 在 k8s stream 中不再需要手动管理
import shutil
import tempfile
import io
import tarfile
import json  # 导入 json 模块用于解析 resize 消息
from fastapi import (
    UploadFile,
    File,
    Form,
    Query,
)  # 导入 UploadFile, File, Form 和 Query
from pydantic import BaseModel  # 导入 BaseModel 用于定义数据模型


app = FastAPI()

# 配置 CORS 中间件，允许所有跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,  # 允许携带凭证
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有请求头
)

# 配置文件路径，按需配置是否使用绝对路径
# CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
# KUBE_CONFIG_FILE = os.path.join(CONFIG_DIR, "config")
KUBE_CONFIG_FILE = "/data/k8s_web_terminal/config/config"  # 使用绝对路径

# 模板目录
templates = Jinja2Templates(directory="templates")

# 静态文件目录
app.mount(
    "/static", StaticFiles(directory="templates/static"), name="static"
)  # 确保这里的路径正确指向包含 xterm.js 等文件的目录


# 定义数据库配置的数据模型
class DatabaseConfig(BaseModel):
    host: str
    port: int
    user: str
    password: str
    database: str


# 数据库连接信息
postgres_config: DatabaseConfig = DatabaseConfig(
    host=os.getenv("POSTGRES_HOST", "10.200.1.171"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    user=os.getenv("POSTGRES_USER", "kube"),
    password=os.getenv("POSTGRES_PASSWORD", "kube"),
    database=os.getenv("POSTGRES_DB", "kube"),
)

# 全局数据库连接池
db_pool = None


# 应用启动事件：初始化数据库连接池并创建表
@app.on_event("startup")
async def startup_db_client():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            user=postgres_config.user,
            password=postgres_config.password,
            database=postgres_config.database,
            host=postgres_config.host,
            port=postgres_config.port,
        )
        async with db_pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS terminal_logs (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(255) NOT NULL,
                    namespace VARCHAR(255) NOT NULL,
                    podname VARCHAR(255) NOT NULL,
                    connection_time TIMESTAMP WITHOUT TIME ZONE DEFAULT (NOW() AT TIME ZONE 'UTC')
                );
            """
            )
        print("数据库连接池已创建，terminal_logs 表已准备就绪。")
    except Exception as e:
        print(f"数据库初始化失败: {e}")
        db_pool = None  # 确保在失败时 db_pool 为 None


# 应用关闭事件：关闭数据库连接池
@app.on_event("shutdown")
async def shutdown_db_client():
    global db_pool
    if db_pool:
        await db_pool.close()
        print("数据库连接池已关闭。")


# 异步函数：记录终端连接日志
async def log_terminal_connection(username: str, namespace: str, podname: str):
    global db_pool
    if not db_pool:
        print("错误：数据库连接池不可用，无法记录日志。")
        return
    try:
        async with db_pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO terminal_logs (username, namespace, podname, connection_time) VALUES ($1, $2, $3, $4)",
                username,
                namespace,
                podname,
                datetime.utcnow(),
            )
            print(
                f"日志记录成功: 用户名={username}, 命名空间={namespace}, Pod名称={podname}"
            )
    except Exception as e:
        print(f"记录终端连接日志时出错: {e}")


@app.get("/connect", response_class=HTMLResponse)
async def connect_to_pod(
    request: Request, chinesename: str, podname: str, namespace: str
):
    """
    处理连接到指定 POD 的请求，并返回终端页面。
    chinesename: 保留参数，暂不使用。
    podname: POD 名称。
    namespace: 命名空间。
    """
    print(
        f"收到请求：chinesename={chinesename}，podname={podname}，namespace={namespace}"
    )
    return templates.TemplateResponse(
        "terminal.html",
        {
            "request": request,
            "podname": podname,
            "namespace": namespace,
            "chinesename": chinesename,  # 虽然不用，但还是传递给模板，以备将来使用
        },
    )


@app.websocket("/ws/{namespace}/{podname}")  # 移除了路径中的 username
async def websocket_endpoint(
    websocket: WebSocket,
    namespace: str,
    podname: str,
    chinesename: str = Query(None),  # 从查询参数获取 chinesename
):  # 修改参数，使用 chinesename
    """
    处理到指定 POD 的 WebSocket 连接，用于终端交互。
    """
    await websocket.accept()
    # 如果 chinesename 为 None 或空字符串，则设置一个默认值或进行相应处理
    effective_username = chinesename if chinesename else "unknown_user"
    print(
        f"为命名空间 {namespace} 中的 {podname} (用户: {effective_username}) 建立了 WebSocket 连接"
    )
    # 记录连接日志
    await log_terminal_connection(effective_username, namespace, podname)

    try:
        # 加载 K8s 配置
        if not os.path.exists(KUBE_CONFIG_FILE):
            error_msg = f"错误：在 {KUBE_CONFIG_FILE} 处未找到 Kubernetes 配置文件\r\n"
            print(error_msg)
            await websocket.send_text(error_msg)
            await websocket.close()
            return

        try:
            # 加载 K8s 配置
            # 确保配置文件存在并且可以被正确加载
            if not os.path.isfile(KUBE_CONFIG_FILE):
                error_msg = f"错误：Kubernetes 配置文件 {KUBE_CONFIG_FILE} 不是一个有效的文件\r\n"
                print(error_msg)
                await websocket.send_text(error_msg)
                await websocket.close()
                return

            # 直接使用配置文件路径，不创建临时文件
            kubernetes.config.load_kube_config(config_file=KUBE_CONFIG_FILE)
            k8s_client_config = K8sConfiguration.get_default_copy()
            k8s_client_config.verify_ssl = False
            # 可选: 如果服务器证书的主机名与请求的主机名不匹配，禁用主机名断言
            # k8s_client_config.assert_hostname = False
            # 新增: 配置与 K8s API Server 的 WebSocket 连接的 ping/pong
            k8s_client_config.websocket_client_params = {
                "ping_interval": 30,  # 每30秒发送一次ping (增加间隔，减少频繁ping)
                "ping_timeout": 60,  # 60秒内未收到pong则超时 (大幅增加超时时间)
                "max_size": 10 * 1024 * 1024,  # 增加最大消息大小到10MB，处理大量数据
                "skip_utf8_validation": True,  # 跳过UTF-8验证以提高性能
            }
            K8sConfiguration.set_default(
                k8s_client_config
            )  # 将修改后的配置设置为默认配置

            # 使用修改后的默认配置创建 API 客户端实例
            core_v1 = kubernetes.client.CoreV1Api()
            print(
                "Kubernetes 配置已成功加载，忽略 SSL 证书验证，并已配置 WebSocket ping/pong。"
            )
        except Exception as e:
            error_msg = f"加载 Kubernetes 配置时出错: {e}\r\n"
            print(error_msg)
            await websocket.send_text(error_msg)
            await websocket.close()
            return

        # 定义在 Pod 中执行的命令，通常是 /bin/sh 或 /bin/bash
        command = ["/bin/bash"]

        # 创建到 Pod 的 exec 连接
        try:
            resp = kubernetes.stream.stream(
                core_v1.connect_get_namespaced_pod_exec,
                podname,
                namespace,
                command=command,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=True,
                _preload_content=False,
            )
            print(f"已成功连接到 {podname} 的 Pod 执行流")
        except kubernetes.client.exceptions.ApiException as e:
            error_msg = f"连接到 Pod 执行流时出错: {e}\r\n"
            print(error_msg)
            await websocket.send_text(error_msg)
            await websocket.close()
            return
        except Exception as e:
            error_msg = f"在与 Pod 建立连接时发生了意外错误：{e}\r\n"
            print(error_msg)
            await websocket.send_text(error_msg)
            await websocket.close()
            return

        async def read_from_pod():
            """从 Pod 读取数据并发送到 WebSocket"""
            try:
                # 设置更短的超时时间，以便更快地响应
                timeout = 0.05  # 50毫秒的超时
                last_activity_time = asyncio.get_event_loop().time()

                while resp.is_open():
                    resp.update(timeout=timeout)

                    data_received = False

                    if resp.peek_stdout():
                        stdout_data = resp.read_stdout()
                        if stdout_data:
                            # 确保数据以\r\n结尾，避免终端显示问题
                            if not stdout_data.endswith(
                                "\r\n"
                            ) and stdout_data.endswith("\n"):
                                stdout_data = stdout_data.replace("\n", "\r\n")
                            await websocket.send_text(stdout_data)
                            data_received = True
                            last_activity_time = asyncio.get_event_loop().time()

                    if resp.peek_stderr():
                        stderr_data = resp.read_stderr()
                        if stderr_data:
                            # 确保数据以\r\n结尾，避免终端显示问题
                            if not stderr_data.endswith(
                                "\r\n"
                            ) and stderr_data.endswith("\n"):
                                stderr_data = stderr_data.replace("\n", "\r\n")
                            await websocket.send_text(stderr_data)
                            data_received = True
                            last_activity_time = asyncio.get_event_loop().time()

                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_activity_time > 60:
                        try:
                            if websocket.client_state == WebSocketState.CONNECTED:
                                await websocket.send_text(
                                    "\x00"
                                )  # 发送一个不可见的字符作为心跳，比空字符串更可靠
                            last_activity_time = current_time
                        except Exception as heartbeat_err:
                            print(f"发送心跳消息失败: {heartbeat_err}")

                    # 动态调整休眠时间，有数据时更快响应，无数据时减少CPU使用
                    if data_received:
                        await asyncio.sleep(0.01)  # 有数据时快速响应
                    else:
                        await asyncio.sleep(0.05)  # 无数据时减少CPU使用

            except Exception as e:
                print(f"从 Pod 读取时出错: {e}")
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(f"从 Pod 读取时出错: {e}\r\n")
            finally:
                if resp.is_open():
                    resp.close()
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    try:
                        await websocket.close()
                    except Exception as close_err:
                        print(f"关闭WebSocket连接时出错: {close_err}")
                print("容器读取流已关闭。")

        async def write_to_pod():
            """从 WebSocket 读取数据并发送到 Pod"""
            try:
                while True:
                    try:
                        # 设置超时，避免长时间阻塞
                        data = await asyncio.wait_for(
                            websocket.receive_text(),
                            timeout=60,  # 60秒超时，可以根据需要调整
                        )

                        if not resp.is_open():
                            print("Pod连接已关闭，无法写入数据")
                            break

                        # 处理特殊的粘贴数据，避免格式错乱
                        # 尝试解析数据是否为 JSON，并检查 type 是否为 resize
                        try:
                            message = json.loads(data)
                            if (
                                isinstance(message, dict)
                                and message.get("type") == "resize"
                            ):
                                cols = message.get("cols")
                                rows = message.get("rows")
                                if cols is not None and rows is not None:
                                    resize_payload = json.dumps(
                                        {"Width": int(cols), "Height": int(rows)}
                                    )
                                    if resp.is_open():
                                        resp.write_channel(
                                            kubernetes.stream.ws_client.RESIZE_CHANNEL,
                                            resize_payload,
                                        )
                                        print(
                                            f"已发送 PTY resize 请求: cols={cols}, rows={rows}"
                                        )
                                    else:
                                        print(
                                            "Pod 连接已关闭，无法发送 PTY resize 请求"
                                        )
                                continue  # 处理完 resize 消息后，继续等待下一条消息
                        except json.JSONDecodeError:
                            # 如果不是 JSON 或者解析失败，则认为是普通输入数据
                            pass
                        except Exception as e:
                            print(f"处理 resize 消息时发生错误: {e}")

                        # 如果不是 resize 消息，则正常写入 STDIN
                        # 如果数据很长或包含多行，可能是粘贴的内容
                        if len(data) > 100 or "\n" in data:
                            # 确保每行都有正确的行尾
                            lines = data.splitlines()
                            for i, line in enumerate(lines):
                                # 写入每一行，并在最后一行之前添加换行符
                                if resp.is_open():
                                    resp.write_stdin(line)
                                    if i < len(lines) - 1:
                                        resp.write_stdin("\n")
                                else:
                                    print("Pod 连接已关闭，无法写入粘贴数据")
                                    break  # 如果连接关闭，则跳出循环
                        else:
                            # 正常写入单行数据
                            if resp.is_open():
                                resp.write_stdin(data)
                            else:
                                print("Pod 连接已关闭，无法写入单行数据")

                    except asyncio.TimeoutError:
                        # 超时只是意味着没有收到数据，继续循环
                        continue
                    except WebSocketDisconnect:
                        print("WebSocket 被客户端断开连接。")
                        break
                    except Exception as e:
                        print(f"接收或处理WebSocket数据时出错: {e}")
                        if str(e).startswith("Connection to remote host was lost"):
                            # 连接丢失，退出循环
                            break
            except Exception as outer_e:
                print(f"写入 Pod 的外层循环出错: {outer_e}")
            finally:
                if resp.is_open():
                    try:
                        resp.close()
                    except Exception as close_err:
                        print(f"关闭Pod响应流时出错: {close_err}")

                if websocket.client_state != WebSocketState.DISCONNECTED:
                    try:
                        await websocket.close()
                    except Exception as ws_close_err:
                        print(f"关闭WebSocket时出错: {ws_close_err}")

                print("由于 WebSocket 活动，Pod 写入流已关闭。")

        # 并发执行读写操作
        read_task = asyncio.create_task(read_from_pod())
        write_task = asyncio.create_task(write_to_pod())

        await asyncio.gather(read_task, write_task)

    except WebSocketDisconnect:
        print(f"WebSocket 与 pod：{podname} 在命名空间：{namespace} 中断开连接")
    except Exception as e:
        error_msg = f"WebSocket 错误，针对 {podname}：{e}\r\n"
        print(error_msg)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.send_text(error_msg)
            except Exception as send_e:
                print(f"向WebSocket 发送最终错误消息时出错: {send_e}")
            await websocket.close()
    finally:
        print(f"WebSocket 连接已关闭，对应 Pod：{podname}，所在命名空间：{namespace}")
        # 确保所有资源被清理，尽管在 read_from_pod 和 write_to_pod 的 finally 中已经处理
        if "resp" in locals() and resp.is_open():
            resp.close()


@app.post("/upload/{namespace}/{podname}")
async def upload_file_to_pod(
    namespace: str,
    podname: str,
    file: UploadFile = File(...),
    # destination_path: str = Form(...),  # 文件在 Pod 内的目标路径 - 用户要求移除
):
    """
    将文件上传到指定的 Pod。
    destination_path: 文件在 Pod 内的完整目标路径，例如 /app/data/myfile.txt
    """
    # 从上传的文件名中获取基本名称，以防止路径遍历
    original_filename = file.filename
    # 检查文件名是否包含路径分隔符或为空
    if not original_filename or "/" in original_filename or "\\" in original_filename:
        error_msg = f"错误：无效的文件名 '{original_filename}'。文件名不能为空且不能包含路径分隔符。"
        print(error_msg)
        return {"error": error_msg, "status_code": 400}

    # 使用安全的文件名
    safe_filename = os.path.basename(original_filename)
    if (
        not safe_filename
    ):  # 再次检查，以防 basename 返回空（例如，如果原始文件名是 '.' 或 '..')
        error_msg = (
            f"错误：处理后的文件名无效 '{original_filename}' -> '{safe_filename}'."
        )
        print(error_msg)
        return {"error": error_msg, "status_code": 400}

    print(
        f"接收到文件上传请求: namespace={namespace}, podname={podname}, original_filename={original_filename}, safe_filename={safe_filename}, destination=/tmp/{safe_filename}"
    )

    try:
        # 加载 K8s 配置 (与 websocket_endpoint 中类似)
        if not os.path.exists(KUBE_CONFIG_FILE):
            error_msg = f"在 {KUBE_CONFIG_FILE} 处未找到 Kubernetes 配置文件"
            print(error_msg)
            return {"error": error_msg}

        try:
            # 确保配置文件存在并且可以被正确加载
            if not os.path.isfile(KUBE_CONFIG_FILE):
                error_msg = (
                    f"错误：Kubernetes 配置文件 {KUBE_CONFIG_FILE} 不是一个有效的文件"
                )
                print(error_msg)
                return {"error": error_msg}

            # 直接使用配置文件路径，不创建临时文件
            kubernetes.config.load_kube_config(config_file=KUBE_CONFIG_FILE)
            k8s_client_config = K8sConfiguration.get_default_copy()
            k8s_client_config.verify_ssl = False
            # 新增: 配置与 K8s API Server 的 WebSocket 连接的 ping/pong
            k8s_client_config.websocket_client_params = {
                "ping_interval": 30,  # 每30秒发送一次ping (增加间隔，减少频繁ping)
                "ping_timeout": 60,  # 60秒内未收到pong则超时 (大幅增加超时时间)
                "max_size": 10 * 1024 * 1024,  # 增加最大消息大小到10MB，处理大量数据
                "skip_utf8_validation": True,  # 跳过UTF-8验证以提高性能
            }
            K8sConfiguration.set_default(k8s_client_config)
            core_v1 = kubernetes.client.CoreV1Api()
            print(
                "Kubernetes 配置已成功加载，忽略 SSL 证书验证，并已配置 WebSocket ping/pong (用于上传)。"
            )
        except Exception as e:
            error_msg = f"加载 Kubernetes 配置时出错 (用于上传): {e}"
            print(error_msg)
            return {"error": error_msg}

        # 用户要求将文件上传到 /tmp 目录，文件名保持不变 (使用安全的文件名)
        pod_target_dir = "/tmp"
        pod_file_path = os.path.join(
            pod_target_dir, safe_filename
        )  # 使用 safe_filename

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            shutil.copyfileobj(file.file, tmp_file)
            tmp_file_path = tmp_file.name
        print(f"文件已临时保存到: {tmp_file_path}")

        try:
            # 创建目标目录 (如果不存在)
            mkdir_command = ["mkdir", "-p", pod_target_dir]
            resp_mkdir = kubernetes.stream.stream(
                core_v1.connect_get_namespaced_pod_exec,
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
                    print(f"MKDIR STDOUT: {resp_mkdir.read_stdout()}")
                if resp_mkdir.peek_stderr():
                    print(f"MKDIR STDERR: {resp_mkdir.read_stderr()}")
            resp_mkdir.close()
            if resp_mkdir.returncode != 0:
                # 即使创建目录失败，也尝试复制，tar可能会创建目录
                print(
                    f"在 Pod 中创建目录 {pod_target_dir} 可能失败，返回码: {resp_mkdir.returncode}"
                )

            # 创建一个包含单个文件的 tar 归档流
            # 文件在 tar 包内的名称应该是相对于目标目录的名称，即 os.path.basename(pod_file_path)
            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                # arcname 是文件在 tar 包中的路径
                # 对于 kubectl cp /tmp/foo mypod:/app/bar，tar 包中是 bar，解压到 /app/ 下
                # arcname 应该是安全的文件名
                tar.add(tmp_file_path, arcname=safe_filename)  # 使用 safe_filename
            tar_stream.seek(0)

            # 在 Pod 中执行 tar 命令以提取文件
            # tar xf - : 从标准输入读取 tar 归档并提取
            # -C <directory> : 提取前切换到指定目录
            exec_command = ["tar", "xf", "-", "-C", pod_target_dir]

            resp_cp = kubernetes.stream.stream(
                core_v1.connect_get_namespaced_pod_exec,
                podname,
                namespace,
                command=exec_command,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=False,
                _preload_content=False,
            )

            print(
                f"向 Pod {podname} 的 {pod_target_dir} 目录传输文件 {safe_filename} (目标名: {safe_filename})"
            )

            # 将 tar 流写入到 Pod 的 stdin
            # 需要分块写入以避免阻塞或内存问题
            buffer_size = 4096  # 4KB 缓冲区
            while True:
                data_chunk = tar_stream.read(buffer_size)
                if not data_chunk:
                    break
                # 使用 ws_client.STDIN_CHANNEL (通常是 0) 来指定标准输入通道
                resp_cp.write_channel(ws_client.STDIN_CHANNEL, data_chunk)
            # WSClient.close() 会关闭所有通道，tar 命令应该能通过 stdin 的关闭来检测到输入结束。

            tar_stream.close()

            # 等待命令完成并检查输出/错误
            while resp_cp.is_open():
                resp_cp.update(timeout=1)
                if resp_cp.peek_stdout():
                    print(f"CP STDOUT: {resp_cp.read_stdout()}")
                if resp_cp.peek_stderr():
                    stderr_output = resp_cp.read_stderr()
                    print(f"CP STDERR: {stderr_output}")
                    # 如果 stderr 有内容，可能表示错误
                    # 但 tar 有时也会在 stderr 输出非错误信息，需要谨慎判断
            resp_cp.close()

            if resp_cp.returncode != 0:
                error_msg = (
                    f"在 Pod 中复制文件失败。Tar 命令返回码: {resp_cp.returncode}"
                )
                print(error_msg)
                return {"error": error_msg}

            print(f"文件 {safe_filename} 已成功上传到 Pod {podname} 的 {pod_file_path}")
            return {"message": f"文件 {safe_filename} 已成功上传到 {pod_file_path}"}

        except kubernetes.client.exceptions.ApiException as e:
            error_msg = f"Kubernetes API 错误: {e}"
            print(error_msg)
            return {"error": error_msg}
        except Exception as e:
            error_msg = f"上传文件到 Pod 时发生意外错误: {e}"
            print(error_msg)
            return {"error": error_msg}
        finally:
            # 5. 清理临时文件
            if os.path.exists(tmp_file_path):
                os.remove(tmp_file_path)
                print(f"临时文件 {tmp_file_path} 已删除")

    except Exception as e:
        # 捕获顶层异常，以防万一
        error_msg = f"处理文件上传时发生未知错误: {e}"
        print(error_msg)
        return {"error": error_msg, "status_code": 500}


if __name__ == "__main__":  # 添加了空行以符合 PEP8
    import uvicorn

    # 允许从任何主机访问，方便测试，生产环境应配置具体允许的域名
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8006,
        ws_ping_interval=30,  # 每30秒发送一次ping（增加间隔）
        ws_ping_timeout=60,  # 60秒内未收到pong则超时（增加超时）
        timeout_keep_alive=120,  # 保持连接活跃的超时时间（增加）
        limit_concurrency=100,  # 限制并发连接数
        limit_max_requests=10000,  # 限制最大请求数
    )
