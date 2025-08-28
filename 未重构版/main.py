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
import logging  # 导入 logging 模块用于日志记录
import logging.handlers  # 导入 handlers 用于配置日志处理器

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
# 配置日志记录
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)  # 确保日志目录存在
# 创建日志记录器
logger = logging.getLogger("k8s_web_terminal")
logger.setLevel(logging.INFO)
# 创建文件处理器，使用RotatingFileHandler实现日志轮转
file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(log_dir, "terminal.log"),
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5,  # 保留5个备份文件
    encoding="utf-8",
)
# 创建控制台处理器
console_handler = logging.StreamHandler()
# 设置日志格式
log_format = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler.setFormatter(log_format)
console_handler.setFormatter(log_format)
# 添加处理器到日志记录器
logger.addHandler(file_handler)
logger.addHandler(console_handler)
# 配置 CORS 中间件，允许所有跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,  # 允许携带凭证
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有请求头
)
# 配置文件路径
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
KUBE_CONFIG_FILE = os.path.join(CONFIG_DIR, "config")
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
global_api_client = None


# 应用启动事件：初始化数据库连接池并创建表
@app.on_event("startup")
async def startup_db_client():
    global db_pool
    global global_api_client
    try:
        # 配置数据库连接池，添加最大连接数和连接超时设置
        db_pool = await asyncpg.create_pool(
            user=postgres_config.user,
            password=postgres_config.password,
            database=postgres_config.database,
            host=postgres_config.host,
            port=postgres_config.port,
            min_size=5,  # 最小连接数
            max_size=20,  # 最大连接数
            max_inactive_connection_lifetime=300.0,  # 空闲连接最大生存时间(秒)
            timeout=10.0,  # 获取连接的超时时间
        )
        async with db_pool.acquire() as connection:
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
        logger.info("数据库连接池已创建，terminal_logs 表已准备就绪。")
        # 加载 Kubernetes 配置
        if not os.path.exists(KUBE_CONFIG_FILE):
            logger.error(f"错误：在 {KUBE_CONFIG_FILE} 处未找到 Kubernetes 配置文件")
            return
        kubernetes.config.load_kube_config(config_file=KUBE_CONFIG_FILE)
        k8s_client_config = K8sConfiguration.get_default_copy()
        k8s_client_config.verify_ssl = False
        # 优化WebSocket连接参数，减少ping间隔，更快检测连接问题
        k8s_client_config.websocket_client_params = {
            "ping_interval": 30,  # 保持较低的ping间隔
            "ping_timeout": 120,  # 增加ping超时时间
            "max_size": 10 * 1024 * 1024,
            "skip_utf8_validation": True,
            "close_timeout": 30,  # 增加关闭超时
        }
        global_api_client = kubernetes.client.ApiClient(configuration=k8s_client_config)
        logger.info("Kubernetes 配置已成功加载和自定义。")
    except Exception as e:
        logger.error(f"启动初始化失败: {e}")
        if db_pool:
            await db_pool.close()
        db_pool = None
        if global_api_client:
            global_api_client.close()
        global_api_client = None


# 应用关闭事件：关闭数据库连接池
@app.on_event("shutdown")
async def shutdown_db_client():
    global db_pool
    global global_api_client
    if db_pool:
        await db_pool.close()
        logger.info("数据库连接池已关闭。")
    if global_api_client:
        global_api_client.close()
        logger.info("Kubernetes API 客户端已关闭。")
    # 确保所有日志都被写入文件
    logging.shutdown()


# 异步函数：记录终端连接日志
async def log_terminal_connection(
    username: str, namespace: str, podname: str, action: str = "连接"
):
    global db_pool
    if not db_pool:
        logger.error("错误：数据库连接池不可用，无法记录日志。")
        return
    try:
        async with db_pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO terminal_logs (username, namespace, podname, connection_time, action) VALUES ($1, $2, $3, $4, $5)",
                username,
                namespace,
                podname,
                datetime.utcnow(),
                action,
            )
            logger.info(
                f"日志记录成功: 用户名={username}, 命名空间={namespace}, Pod名称={podname}, 操作={action}"
            )
            # 立即提交事务，确保日志实时记录
            await connection.execute("COMMIT")
    except Exception as e:
        logger.error(f"记录终端连接日志时出错: {e}")
        # 不需要额外的文件记录，因为logger已经配置了文件处理器


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
    logger.info(
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
    chinesename: str = Query(None),
):
    await websocket.accept()
    effective_username = chinesename if chinesename else "unknown_user"
    logger.info(
        f"为命名空间 {namespace} 中的 {podname} (用户: {effective_username}) 建立了 WebSocket 连接"
    )
    # 记录连接建立日志
    await log_terminal_connection(effective_username, namespace, podname, "连接建立")
    try:
        if global_api_client is None:
            await websocket.send_text("错误：Kubernetes 配置未加载\r\n")
            await websocket.close()
            return
        core_v1 = kubernetes.client.CoreV1Api(api_client=global_api_client)
        logger.info("使用全局 Kubernetes API 客户端。")
        command = ["/bin/bash"]
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
            logger.info(f"已成功连接到 {podname} 的 Pod 执行流")
        except kubernetes.client.exceptions.ApiException as e:
            error_msg = f"连接到 Pod 执行流时出错: {e}\r\n"
            logger.error(error_msg)
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_text(error_msg)
            await websocket.close()
            return
        except Exception as e:
            error_msg = f"在与 Pod 建立连接时发生了意外错误：{e}\r\n"
            logger.error(error_msg)
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_text(error_msg)
            await websocket.close()
            return
        # 共享状态变量
        connection_active = True
        last_activity_time = asyncio.get_event_loop().time()  # 用户最后活动时间
        connection_start_time = asyncio.get_event_loop().time()  # 连接开始时间
        IDLE_TIMEOUT = 300  # 5分钟无活动超时
        CONNECTION_TIMEOUT = 3600  # 1小时总连接超时

        async def read_from_pod():
            """从 Pod 读取数据并发送到 WebSocket"""
            nonlocal connection_active  # 不再更新 last_activity_time，只在用户输入时更新
            try:
                # 设置更短的超时时间，以便更快地响应
                timeout = 0.05  # 50毫秒的超时
                last_heartbeat_time = asyncio.get_event_loop().time()
                last_check_time = asyncio.get_event_loop().time()
                while resp.is_open() and connection_active:
                    try:
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
                                if websocket.client_state == WebSocketState.CONNECTED:
                                    await websocket.send_text(stdout_data)
                                    data_received = True
                                    # 不再更新最后活动时间，只在用户输入时更新
                                else:
                                    break
                        if resp.peek_stderr():
                            stderr_data = resp.read_stderr()
                            if stderr_data:
                                # 确保数据以\r\n结尾，避免终端显示问题
                                if not stderr_data.endswith(
                                    "\r\n"
                                ) and stderr_data.endswith("\n"):
                                    stderr_data = stderr_data.replace("\n", "\r\n")
                                if websocket.client_state == WebSocketState.CONNECTED:
                                    await websocket.send_text(stderr_data)
                                    data_received = True
                                    # 不再更新最后活动时间，只在用户输入时更新
                                else:
                                    break
                        current_time = asyncio.get_event_loop().time()
                        # 每30秒强制检查一次连接状态，即使没有数据交互
                        if current_time - last_check_time > 30:
                            last_check_time = current_time
                            # 检查连接是否超时
                            if current_time - last_activity_time > IDLE_TIMEOUT:
                                logger.warning("定期检查发现连接超时，无活动时间过长")
                                connection_active = False
                                break
                            # 检查总连接时间是否超时
                            if (
                                current_time - connection_start_time
                                > CONNECTION_TIMEOUT
                            ):
                                logger.warning("定期检查发现连接总时间超时")
                                connection_active = False
                                break
                        # 每15秒发送一次心跳到Kubernetes连接，但不更新用户活动时间
                        if current_time - last_heartbeat_time > 15:
                            try:
                                # 发送空字符到Pod，保持连接活跃
                                if resp.is_open():
                                    resp.write_stdin("")
                                    last_heartbeat_time = current_time
                                    logger.debug("发送Kubernetes连接心跳")
                            except Exception as heartbeat_err:
                                logger.error(f"发送Kubernetes心跳失败: {heartbeat_err}")
                                # 不中断连接，继续尝试
                        # 动态调整休眠时间，有数据时更快响应，无数据时减少CPU使用
                        if data_received:
                            await asyncio.sleep(0.01)  # 有数据时快速响应
                        else:
                            await asyncio.sleep(0.05)  # 无数据时减少CPU使用
                    except Exception as loop_err:
                        logger.error(f"Pod读取循环中出错: {loop_err}")
                        if (
                            str(loop_err).lower().find("connection") >= 0
                            or str(loop_err).lower().find("closed") >= 0
                        ):
                            break
                        await asyncio.sleep(0.1)  # 出错时短暂暂停，避免CPU占用过高
            except Exception as e:
                logger.error(f"从 Pod 读取时出错: {e}")
                if websocket.client_state == WebSocketState.CONNECTED:
                    try:
                        await websocket.send_text(f"从 Pod 读取时出错: {e}\r\n")
                    except Exception as send_err:
                        logger.error(f"发送错误消息时出错: {send_err}")
            finally:
                logger.info("正在清理Pod读取流资源...")
                if resp.is_open():
                    try:
                        resp.close()
                        logger.info("Pod响应流已关闭")
                    except Exception as resp_close_err:
                        logger.error(f"关闭Pod响应流时出错: {resp_close_err}")
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    try:
                        await websocket.close()
                        logger.info("WebSocket连接已关闭(read_from_pod)")
                    except Exception as close_err:
                        logger.error(f"关闭WebSocket连接时出错: {close_err}")
                logger.info("容器读取流资源清理完成。")

        async def write_to_pod():
            """从 WebSocket 读取数据并发送到 Pod"""
            nonlocal connection_active, last_activity_time
            try:
                while connection_active:
                    try:
                        # 设置超时，避免长时间阻塞，减少超时时间以便更快检测连接问题
                        data = await asyncio.wait_for(
                            websocket.receive_text(),
                            timeout=30,  # 从60秒减少到30秒，更频繁地检查超时
                        )
                        # 检查是否是心跳包（\x00），如果是则跳过处理，不更新活动时间
                        if data == "\x00":
                            logger.debug("收到心跳包，跳过处理")
                            continue
                        # 更新最后活动时间 - 只在用户输入时更新
                        last_activity_time = asyncio.get_event_loop().time()
                        if not resp.is_open():
                            logger.warning("Pod连接已关闭，无法写入数据")
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
                                        logger.info(
                                            f"已发送 PTY resize 请求: cols={cols}, rows={rows}"
                                        )
                                    else:
                                        logger.warning(
                                            "Pod 连接已关闭，无法发送 PTY resize 请求"
                                        )
                                        break
                                continue  # 处理完 resize 消息后，继续等待下一条消息
                        except json.JSONDecodeError:
                            # 如果不是 JSON 或者解析失败，则认为是普通输入数据
                            pass
                        except Exception as e:
                            logger.error(f"处理 resize 消息时发生错误: {e}")
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
                                    logger.warning("Pod 连接已关闭，无法写入粘贴数据")
                                    break  # 如果连接关闭，则跳出循环
                        else:
                            # 正常写入单行数据
                            if resp.is_open():
                                resp.write_stdin(data)
                            else:
                                logger.warning("Pod 连接已关闭，无法写入单行数据")
                                break
                        # 检查连接是否超时
                        current_time = asyncio.get_event_loop().time()
                        if (
                            current_time - last_activity_time > IDLE_TIMEOUT
                        ):  # 5分钟无活动则断开
                            logger.warning("写入端检测到连接超时，无活动时间过长")
                            break
                        # 检查总连接时间是否超时
                        if current_time - connection_start_time > CONNECTION_TIMEOUT:
                            logger.warning("写入端检测到连接总时间超时")
                            break
                    except asyncio.TimeoutError:
                        # 超时检查连接状态
                        current_time = asyncio.get_event_loop().time()
                        if (
                            current_time - last_activity_time > IDLE_TIMEOUT
                        ):  # 5分钟无活动则断开
                            logger.warning("写入端检测到连接超时，无活动时间过长")
                            break
                        # 检查总连接时间是否超时
                        if current_time - connection_start_time > CONNECTION_TIMEOUT:
                            logger.warning("写入端检测到连接总时间超时")
                            break
                        if (
                            not resp.is_open()
                            or websocket.client_state != WebSocketState.CONNECTED
                        ):
                            logger.info("检测到连接已关闭，终止写入循环")
                            break
                        # 移除了手动ping WebSocket的代码，因为FastAPI的WebSocket对象没有ping方法
                        # 心跳由框架自动处理
                        continue
                    except WebSocketDisconnect:
                        logger.info("WebSocket 被客户端断开连接。")
                        break
                    except Exception as e:
                        logger.error(f"接收或处理WebSocket数据时出错: {e}")
                        if (
                            str(e).lower().find("connection") >= 0
                            or str(e).lower().find("closed") >= 0
                        ):
                            logger.warning("检测到连接相关错误，终止写入循环")
                            break
                        # 其他错误短暂暂停后继续尝试
                        await asyncio.sleep(0.1)
            except Exception as outer_e:
                logger.error(f"写入 Pod 的外层循环出错: {outer_e}")
            finally:
                logger.info("正在清理Pod写入流资源...")
                if resp.is_open():
                    try:
                        resp.close()
                        logger.info("Pod响应流已关闭(write_to_pod)")
                    except Exception as close_err:
                        logger.error(f"关闭Pod响应流时出错: {close_err}")
                # 检查WebSocket状态后再尝试关闭
                if websocket.client_state != WebSocketState.DISCONNECTED:
                    try:
                        await websocket.close()
                        logger.info("WebSocket连接已关闭(write_to_pod)")
                    except Exception as ws_close_err:
                        logger.error(f"关闭WebSocket时出错: {ws_close_err}")
                logger.info("Pod写入流资源清理完成。")

        # 并发执行读写操作
        read_task = asyncio.create_task(read_from_pod())
        write_task = asyncio.create_task(write_to_pod())
        # 使用wait_for代替gather，设置总体超时时间
        try:
            # 等待任一任务完成或超时
            done, pending = await asyncio.wait(
                [read_task, write_task],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=CONNECTION_TIMEOUT,  # 使用总连接超时时间
            )
            # 取消未完成的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as cancel_err:
                    logger.error(f"取消任务时出错: {cancel_err}")
        except asyncio.TimeoutError:
            logger.warning("WebSocket连接超时，强制关闭")
            # 取消所有任务
            read_task.cancel()
            write_task.cancel()
            try:
                await read_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await write_task
            except (asyncio.CancelledError, Exception):
                pass
    except WebSocketDisconnect:
        logger.info(f"WebSocket 与 pod：{podname} 在命名空间：{namespace} 中断开连接")
    except Exception as e:
        error_msg = f"WebSocket 错误，针对 {podname}：{e}\r\n"
        logger.error(error_msg)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.send_text(error_msg)
            except Exception as send_e:
                logger.error(f"向WebSocket 发送最终错误消息时出错: {send_e}")
            await websocket.close()
    finally:
        logger.info(
            f"WebSocket 连接已关闭，对应 Pod：{podname}，所在命名空间：{namespace}"
        )
        # 记录连接关闭日志
        try:
            await log_terminal_connection(
                effective_username, namespace, podname, "连接关闭"
            )
        except Exception as log_err:
            logger.error(f"记录连接关闭日志时出错: {log_err}")
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
        logger.error(error_msg)
        return {"error": error_msg, "status_code": 400}
    # 使用安全的文件名
    safe_filename = os.path.basename(original_filename)
    if (
        not safe_filename
    ):  # 再次检查，以防 basename 返回空（例如，如果原始文件名是 '.' 或 '..'）
        error_msg = (
            f"错误：处理后的文件名无效 '{original_filename}' -> '{safe_filename}'。"
        )
        logger.error(error_msg)
        return {"error": error_msg, "status_code": 400}
    logger.info(
        f"接收到文件上传请求: namespace={namespace}, podname={podname}, original_filename={original_filename}, safe_filename={safe_filename}, destination=/tmp/{safe_filename}"
    )
    try:
        # 加载 K8s 配置 (与 websocket_endpoint 中类似)
        if not os.path.exists(KUBE_CONFIG_FILE):
            return {"error": f"在 {KUBE_CONFIG_FILE} 处未找到 Kubernetes 配置文件"}
        try:
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
            # Create ApiClient with this specific configuration instance
            api_client = kubernetes.client.ApiClient(configuration=k8s_client_config)
            # Pass the custom ApiClient to CoreV1Api
            core_v1 = kubernetes.client.CoreV1Api(api_client=api_client)
            logger.info(
                "Kubernetes 配置已成功加载和自定义，忽略 SSL 证书验证，并已配置 WebSocket ping/pong (用于上传)。"
            )
        except Exception as e:
            error_msg = f"加载 Kubernetes 配置时出错 (用于上传): {e}"
            logger.error(error_msg)
            return {"error": error_msg}
        # 用户要求将文件上传到 /tmp 目录，文件名保持不变 (使用安全的文件名)
        pod_target_dir = "/tmp"
        pod_file_path = os.path.join(
            pod_target_dir, safe_filename
        )  # 使用 safe_filename
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            shutil.copyfileobj(file.file, tmp_file)
            tmp_file_path = tmp_file.name
        logger.info(f"文件已临时保存到: {tmp_file_path}")
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
                    logger.info(f"MKDIR STDOUT: {resp_mkdir.read_stdout()}")
                if resp_mkdir.peek_stderr():
                    logger.warning(f"MKDIR STDERR: {resp_mkdir.read_stderr()}")
            resp_mkdir.close()
            if resp_mkdir.returncode != 0:
                # 即使创建目录失败，也尝试复制，tar可能会创建目录
                logger.warning(
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
            logger.info(
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
                    logger.info(f"CP STDOUT: {resp_cp.read_stdout()}")
                if resp_cp.peek_stderr():
                    stderr_output = resp_cp.read_stderr()
                    logger.warning(f"CP STDERR: {stderr_output}")
                    # 如果 stderr 有内容，可能表示错误
                    # 但 tar 有时也会在 stderr 输出非错误信息，需要谨慎判断
            resp_cp.close()
            if resp_cp.returncode != 0:
                error_msg = (
                    f"在 Pod 中复制文件失败。Tar 命令返回码: {resp_cp.returncode}"
                )
                logger.error(error_msg)
                return {"error": error_msg}
            logger.info(
                f"文件 {safe_filename} 已成功上传到 Pod {podname} 的 {pod_file_path}"
            )
            return {"message": f"文件 {safe_filename} 已成功上传到 {pod_file_path}"}
        except kubernetes.client.exceptions.ApiException as e:
            error_msg = f"Kubernetes API 错误: {e}"
            logger.error(error_msg)
            return {"error": error_msg}
        except Exception as e:
            error_msg = f"上传文件到 Pod 时发生意外错误: {e}"
            logger.error(error_msg)
            return {"error": error_msg}
        finally:
            # 5. 清理临时文件
            if os.path.exists(tmp_file_path):
                os.remove(tmp_file_path)
                logger.info(f"临时文件 {tmp_file_path} 已删除")
    except Exception as e:
        # 捕获顶层异常，以防万一
        error_msg = f"处理文件上传时发生未知错误: {e}"
        logger.error(error_msg)
        return {"error": error_msg, "status_code": 500}


if __name__ == "__main__":  # 添加了空行以符合 PEP8
    import uvicorn

    # 允许从任何主机访问，方便测试，生产环境应配置具体允许的域名
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8006,
        ws_ping_interval=30,  # 每30秒发送一次ping
        ws_ping_timeout=60,  # 60秒内未收到pong则超时
        timeout_keep_alive=300,  # 5分钟保持连接活跃
        limit_concurrency=50,
        limit_max_requests=5000,
        workers=4,
    )
