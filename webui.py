# webui.py
# ========================================================================
# Nascence 辉夜 Web 控制面板（替代原 PyQt5 控制面板）
#
# 本文件提供完整的 Web 服务：
#   - 静态前端（webui/ 目录下的 index.html / style.css / app.js）
#   - REST API（/api/*）：启动/停止 QQ、对话测试、伪造发送/思考、记忆注入、
#                          模型开关切换、配置读写、自训练控制等
#   - WebSocket（/ws）：实时推送日志、状态、对话消息到浏览器
#   - 三个模型后端（text/embed/multimodal）由 core.model_backend 托管，
#     启动时按开关加载 llama.cpp 本地模型，运行中可在「模型」页切换
#
# 运行方式：
#   python webui.py                 # 启动 Web 面板（默认 127.0.0.1:8787）
#   python webui.py --port 9000     # 自定义端口
#
# 浏览器访问 http://127.0.0.1:8787
# ========================================================================

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import posixpath
import re
import secrets
import signal
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from pathlib import Path

from gateway import social_manager, lottery_manager

from aiohttp import web

from config.api_config import config, DEFAULT_CONFIG, reload_config, save_config
from config.constants import BOT_NAME
from utils.event_bus import BUS
from core import runtime_paths
from core.user_worker import UserWorkerPool

PROJECT_DIR = Path(__file__).resolve().parent
RUN_DIR = PROJECT_DIR / "run"
LOG_DIR = RUN_DIR / "logs"
WEBUI_DIR = PROJECT_DIR / "webui"
GATEWAY_DIR = PROJECT_DIR / "gateway"
CLIENT_DIR = PROJECT_DIR / "client"  # 客户端独立存放（可脱离服务端单独打开）
USERS_FILE = GATEWAY_DIR / "users.json"
SESSIONS_FILE = GATEWAY_DIR / "sessions.json"

# 全站广播（管理员在「工具」页发布，持久化保存；客户端打开后弹窗展示）
BROADCAST_FILE = GATEWAY_DIR / "broadcast.json"
# IP 访问控制黑白名单（管理员在「工具」页配置，持久化保存）
IP_RULES_FILE = GATEWAY_DIR / "ip_rules.json"

USER_WORKERS = UserWorkerPool(str(PROJECT_DIR))
GLOBAL_COGNITION_ENABLED = False

# 心跳与空闲自动启停状态（按用户独立计算：每个用户 30 秒无心跳则仅暂停该用户认知循环）
_USER_HEARTBEATS = {}    # username -> last heartbeat timestamp
_USER_PAUSED = {}        # username -> bool (该用户是否因空闲被自动暂停)
_PENDING_EXIT_USERS = {} # username -> deadline_timestamp (用户下线后等待1分钟退出的计划截止时间)
_heartbeat_watchdog_task = None

# 在线判定窗口（秒）：客户端每 15 秒上报一次心跳，留出 3 个周期的容错。
# 只要在这个窗口内收到过心跳（或存在活跃 WebSocket），即视为在线——与认知循环是否启动无关。
_ONLINE_HEARTBEAT_WINDOW = 45
_OFFLINE_EXIT_DELAY_SECONDS = 60.0  # 用户判定下线后，等待1分钟保存数据并退出进程释放内存

# 认知循环启动控制（安全优先：启动后必须等待前端心跳才真正开始运转）
_COGNITION_DESIRED = False    # 管理员是否期望认知循环开启（系统/控制面板启动时置 True）
# 已授权启动但尚未收到前端心跳的用户 -> (memory_user_id, role)
_PENDING_COGNITION_USERS = {}

# 各用户 worker 主动上报的热数据缓存：username -> {"mem","links","words","ts"}
# （供控制面板总览汇总所有在线用户之和；子进程空闲时每约 2 秒上报，见 user_worker.py）
_WORKER_STATS = {}
_WORKER_STATS_LOCK = threading.Lock()

# 默认端口（可通过 --port 覆盖；或读配置中保存的 server_port）
DEFAULT_WEB_PORT = 8787
# 网页版客户端独立端口（随服务器运行；读配置中保存的 client_port）
DEFAULT_CLIENT_PORT = 8987

# 运行中实时切换监听端口所需的状态（复用同一 AppRunner，停旧 site 起新 site）
_RUNNER = None          # 当前 AppRunner 引用（_serve 内赋值）
_SITES = []             # 当前活跃的 TCPSite 列表
_CURRENT_PORT = None    # 当前监听端口
_CURRENT_HOST = "0.0.0.0"  # 当前监听主机地址

# 网页版客户端服务状态（独立端口，随服务器启动）
_CLIENT_RUNNER = None
_CLIENT_SITES = []
_CLIENT_PORT = None

# 日志分类（与前端 tab 对应）
LOG_CAT_RUNTIME = "runtime"
LOG_CAT_THINKING = "thinking"
LOG_CAT_MODEL = "model"
LOG_CAT_CONNECTION = "connection"  # 连接类：Web 轮询(ping) 等

# 历史日志环形缓冲（供 WebSocket 连接建立时补发）
# 各分类日志行数上限（统一 2000）
# 每条日志为结构化 dict：{"cat", "line", "level", "user"}
_MAX_LOG_LINES = 2000
_log_buffer = {cat: [] for cat in (LOG_CAT_RUNTIME, LOG_CAT_THINKING, LOG_CAT_MODEL, LOG_CAT_CONNECTION)}

# 从日志行/logger 名中提取用户名（用于把日志归属到具体用户）
_USER_IN_LOGGER = re.compile(r"^UserWorker\[([^\]]+)\]")
_USER_IN_MSG = re.compile(r"用户\s+([^，。:：\s]+)")


def _extract_log_user(record):
    """尽量判断一条日志属于哪个用户：优先 logger 名 UserWorker[<username>]，其次消息文本。"""
    try:
        m = _USER_IN_LOGGER.match(record.name or "")
        if m:
            return m.group(1)
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)
        m2 = _USER_IN_MSG.search(msg)
        if m2:
            return m2.group(1).strip()
    except Exception:
        pass
    return ""


# ------------------------------------------------------------------------
# 日志处理
# ------------------------------------------------------------------------
class SignalLogHandler(logging.Handler):
    """把日志写入环形缓冲，并广播给所有 WebSocket 客户端。"""

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self._clients = set()      # 在线 WebSocket
        self._clients_lock = threading.Lock()

    def add_client(self, ws):
        with self._clients_lock:
            self._clients.add(ws)

    def remove_client(self, ws):
        with self._clients_lock:
            self._clients.discard(ws)

    def emit(self, record):
        try:
            cat = self._categorize(record)
            line = self.format(record)
            level = record.levelname  # INFO / WARNING / ERROR ...
            user = _extract_log_user(record)
            entry = {"cat": cat, "line": line, "level": level, "user": user}
            # 写入环形缓冲（线程安全由 GIL 保证），统一限制行数
            buf = _log_buffer.setdefault(cat, [])
            buf.append(entry)
            if len(buf) > _MAX_LOG_LINES:
                del buf[:len(buf) - _MAX_LOG_LINES]
            # 入队广播（不直接碰 asyncio，避免跨线程事件循环问题）
            if _ws_broadcast_queue is not None:
                _ws_broadcast_queue.put_nowait(entry)
        except Exception:
            pass

    @staticmethod
    def _categorize(record):
        name = record.name
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)

        # 1. 连接类日志（含心跳、Web 轮询、WebSocket 连接等） → 「连接」页
        if (
            name == "aiohttp.access"
            or name.startswith("UserWorker.Connection")
            or "[心跳]" in msg
            or "用户子进程正在运行" in msg
            or any(k in msg for k in ("WebSocket", "连接", "发送失败", "连接已断开", "[心跳]"))
        ):
            return LOG_CAT_CONNECTION

        # 2. 思考类日志（含认知循环、思考、意图拆解、用户对话处理等） → 「思考」页
        if (
            name in ("Nascence.Processing", "CognitionRunner", "CognitiveLoop", "UserWorker.Thinking")
            or any(k in msg for k in (
                "[用户对话]", "[认知循环]", "[主动发言]", "[思考]", "[认知]",
                "[内心独白]", "[意图拆解]", "[联想扩散]", "[思维回想]", "思考", "thinking"
            ))
        ):
            return LOG_CAT_THINKING

        # 3. 模型加载/推理相关日志 → 「模型」页
        if name == "ModelBackend":
            return LOG_CAT_MODEL
        model_keywords = (
            "[ModelBackend]", "[embed]", "[text]", "[multimodal]", "[模型]",
            "正在启动语义模型", "语义模型启动完成", "llama-server", "llama.cpp",
            "模型", "GGUF", "mmproj", "embedding"
        )
        if any(k in msg for k in model_keywords):
            return LOG_CAT_MODEL

        # 4. 其他常规系统日志 → 「运行」页
        return LOG_CAT_RUNTIME


# 全局日志处理器（web 服务器运行时由 configure_logging 注册）
# 使用 asyncio.Queue 桥接：日志可能来自任意线程，统一入队，
# 由主事件循环中的 task 消费并推送给 WebSocket 客户端，避免跨线程直接调用 asyncio API。
signal_handler = SignalLogHandler()
_ws_broadcast_queue = None      # asyncio.Queue；由 start() 初始化
_ws_broadcast_task = None       # 消费队列的后台 task
_main_loop = None               # 主事件循环引用


# ------------------------------------------------------------------------
# 运行环境（Runtime）
# ------------------------------------------------------------------------
class Runtime:
    """核心业务逻辑（与原控制面板一致，去掉 PyQt 依赖）。"""

    def __init__(self):
        self.initialized = False
        self._shutdown_done = False
        self._lock = threading.RLock()  # 可重入：initialize 与认知控制会嵌套加锁

    # ---------------- 初始化 ----------------
    def initialize(self):
        with self._lock:
            if self.initialized:
                return
            logging.info("开始初始化 Nascence 运行环境")
            # 1. 启动本地模型后端（按开关加载三个默认模型）
            from core.model_backend import start_default_backends, check_models_ready
            start_default_backends()
            # 2. 门槛检查：至少每种能力有本地模型或外部 API，否则明确报错阻止启动
            check_models_ready()
            # 3. 预热 embedding + 加载记忆
            from core.memory_engine import get_model, memories, _init_metrics_counters
            from core.virtual_clock import clock
            from main import cold_start_batch_injection
            from utils.persistence import load_all_data, load_state, save_all_data, save_state
            from core.llm_interface import load_dialogue_history

            clock.enable_qq_mode()
            get_model()
            load_all_data()
            load_state()
            load_dialogue_history()
            _init_metrics_counters()
            # 概念层：按当前数据目录重建概念索引（失败不阻断启动，概念路自动降级为不启用）
            try:
                from core.concept_store import reload_from_db as _concept_reload, stats as _concept_stats
                _concept_reload()
                logging.info("概念层已加载：%s", _concept_stats())
            except Exception:
                logging.exception("概念层初始化失败，本次运行将不启用概念检索")
            if not memories:
                try:
                    cold_start_batch_injection()
                    save_all_data()
                    save_state()
                except Exception as e:
                    # embed 模型未就绪/未配置 API 时冷启动注入会失败：
                    # 记录警告并跳过注入，不阻塞服务与认知循环启动
                    logging.warning("冷启动记忆注入跳过（embed 不可用）: %s", e)
            self.initialized = True
            logging.info("运行环境初始化完成，记忆数=%d", len(memories))

    # ---------------- 认知循环控制 ----------------
    def start_cognition(self):
        """启动永续认知循环（独立常驻）。"""
        self.initialize()
        from core.cognition_runner import COGNITION
        COGNITION.start()
        BUS.emit_status("认知循环运行中")
        logging.info("认知循环已启动")

    def stop_cognition(self):
        """优雅停止认知循环（当前轮完成后退出，不影响主程序/面板）。"""
        from core.cognition_runner import COGNITION
        COGNITION.stop()
        BUS.emit_status("认知循环已停止")
        logging.info("认知循环停止信号已发送")

    def cognition_status(self) -> dict:
        """返回认知循环当前状态（供 WebUI 展示）。"""
        from core.cognition_runner import COGNITION
        return {"running": COGNITION.is_running()}

    # ---------------- 伪造思考 ----------------
    def fake_think(self, text):
        self.initialize()
        from core.cognition import generate_response
        from utils.message_history import add_message
        from utils.persistence import save_all_data, save_state

        augmented = f"我（{BOT_NAME}）自己想着：{text}"
        reply, _ = generate_response(augmented)
        reply = str(reply or "（静默）").strip()
        memory_text = f"我想：{reply}"
        add_message(BOT_NAME, memory_text, "伪造思考")
        save_all_data()
        save_state()
        logging.info("伪造思考完成：输入=%s 想法=%s", text, reply)
        return reply

    # ---------------- 记忆注入 ----------------
    def inject_memory(self, content):
        self.initialize()
        from core.memory_engine import create_memory
        from utils.persistence import save_all_data

        memory_id = create_memory(content)
        save_all_data()
        logging.info("管理员注入记忆成功，ID=%s，内容=%s", memory_id, content)
        return memory_id

    # ---------------- 保存 / 关闭 ----------------
    def save(self):
        if not self.initialized:
            return
        from utils.persistence import save_all_data, save_state
        save_all_data()
        save_state()
        logging.info("记忆和对话状态已完整保存")

    def shutdown(self):
        """完整关停：停止认知循环 → 最终保存 → 停止模型后端。

        幂等：可被按钮退出、信号、atexit、_serve finally 多路径重复调用，
        内部通过 _shutdown_done 保证只执行一次完整关停。
        """
        with self._lock:
            if getattr(self, "_shutdown_done", False):
                return
            self._shutdown_done = True
        logging.info("控制面板正在停止全部服务")
        try:
            from core.cognition_runner import COGNITION
            COGNITION.shutdown()
        except Exception:
            logging.exception("停止认知循环失败")
        try:
            self.save()
        except Exception:
            logging.exception("退出保存失败")
        try:
            from core.model_backend import stop_all_backends
            stop_all_backends()
        except Exception:
            logging.exception("停止模型后端失败")
        logging.info("全部服务已停止")


RUNTIME = Runtime()


# ------------------------------------------------------------------------
# API 辅助：模型开关切换
# ------------------------------------------------------------------------
def _switch_backend(name: str, use_default: bool, api_base_url: str, api_key: str, api_model: str) -> dict:
    """切换指定后端的模式（默认模型 / 外部 API），返回 (ok, needs_restart, message)。

    说明：
      - 切到 API：本地模型保持运行不关闭，仅路由变更（可实时生效）
      - 切到默认：若本地模型本次已加载 → 立即生效；否则提示重启
    """
    from core.model_backend import BACKENDS
    backend = BACKENDS.get(name)
    if backend is None:
        return {"ok": False, "needs_restart": False, "message": f"未知后端: {name}"}

    # 先更新配置（写回磁盘 + 热刷新全局 config）
    cfg = config.get("backends", {})
    cfg.setdefault(name, {})
    cfg[name]["use_default"] = bool(use_default)
    cfg[name]["api_base_url"] = (api_base_url or "").strip()
    # 前端不回显 API Key；当提交的 key 为空时保留原有 key，避免误清
    submitted_key = (api_key or "").strip()
    if submitted_key:
        cfg[name]["api_key"] = submitted_key
    cfg[name]["api_model"] = (api_model or "").strip()
    save_config(config)
    reload_config()

    # 执行切换语义判断
    ok, needs_restart, message = backend.switch_use_default(bool(use_default))
    if not ok:
        # 配置不合法：回滚 use_default（保留用户填的 API）
        cfg[name]["use_default"] = not bool(use_default)
        save_config(config)
        reload_config()
    logging.info("[模型] %s 切换 use_default=%s -> %s", name, use_default, message)
    return {"ok": ok, "needs_restart": needs_restart, "message": message}


# ------------------------------------------------------------------------
# WebSocket 消息推送（消息 / 状态 / 私聊与群聊定向推送）
# ------------------------------------------------------------------------
_admin_ws_clients = set()    # 管理员控制面板 WebSocket（接收运行日志、系统指标监控与全局事件）
_client_ws_clients = set()   # 客户端用户 WebSocket（仅接收 instant_broadcast、状态与用户专属消息，严禁下发日志）
_ws_user_clients = defaultdict(set)  # username -> set of ws
_ws_clients_lock = threading.Lock()


async def _send_to_user_ws(username: str, payload: dict):
    """向指定用户的所有在线 WebSocket 客户端推送消息。"""
    if not username:
        return
    with _ws_clients_lock:
        targets = list(_ws_user_clients.get(username, set()))
    if not targets:
        return
    text = json.dumps(payload, ensure_ascii=False)
    for ws in targets:
        await _ws_safe_send(ws, text)


async def _send_to_users_ws(usernames: list, payload: dict):
    """向多个用户推送消息。"""
    for u in usernames or []:
        await _send_to_user_ws(u, payload)


def _broadcast(payload: dict):
    """把消息入队，由主事件循环统一推送给所有 WebSocket 客户端。"""
    if _ws_broadcast_queue is not None:
        try:
            _ws_broadcast_queue.put_nowait(payload)
        except Exception:
            pass


def _bus_forwarder(topic, *args):
    """把事件总线消息转发为 WebSocket 消息。"""
    if topic == "message":
        sender, content, source = args
        _broadcast({"type": "message", "sender": sender, "content": content, "source": source})
    elif topic == "status":
        _broadcast({"type": "status", "text": args[0]})


async def _ws_broadcast_worker():
    """消费广播队列并分流推送（在主事件循环内运行）。"""
    global _ws_broadcast_queue, _ws_broadcast_task
    while True:
        try:
            item = await _ws_broadcast_queue.get()
            # 日志条目：dict 且含 cat/line；其余 dict 为结构消息（message/status 等）
            if (isinstance(item, dict) and "cat" in item and "line" in item) or isinstance(item, tuple):
                if isinstance(item, dict):
                    payload = {
                        "type": "log",
                        "cat": item.get("cat"),
                        "line": item.get("line"),
                        "level": item.get("level", "INFO"),
                        "user": item.get("user", ""),
                    }
                else:
                    cat, line = item
                    payload = {"type": "log", "cat": cat, "line": line, "level": "INFO", "user": ""}
                text = json.dumps(payload, ensure_ascii=False)
                # 安全隔离：运行日志属于内部敏感信息，严格仅推送给控制面板管理员 WebSocket
                with _ws_clients_lock:
                    targets = list(_admin_ws_clients)
                for ws in targets:
                    await _ws_safe_send(ws, text)
            else:
                # 全局广播消息（如 instant_broadcast / status / message 等）：推给管理员与所有在线客户端
                payload = item
                text = json.dumps(payload, ensure_ascii=False)
                with _ws_clients_lock:
                    targets = list(_admin_ws_clients | _client_ws_clients)
                for ws in targets:
                    await _ws_safe_send(ws, text)
        except asyncio.CancelledError:
            break
        except Exception:
            continue


# ------------------------------------------------------------------------
# 用户鉴权与会话管理（PBKDF2 加盐哈希）
# ------------------------------------------------------------------------
def _load_users() -> dict:
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    # 自动迁移：为历史存量用户补齐 role、user_id，并创建学生记忆体目录
    modified = False
    used_ids = {u.get("user_id") for u in users.values() if isinstance(u, dict) and u.get("user_id")}
    for uname, udata in list(users.items()):
        if not isinstance(udata, dict):
            continue
        if "role" not in udata or not udata["role"]:
            udata["role"] = ROLE_STUDENT
            modified = True
        if "user_id" not in udata or not udata["user_id"]:
            new_uid = _generate_user_id()
            while new_uid in used_ids:
                new_uid = _generate_user_id()
            udata["user_id"] = new_uid
            used_ids.add(new_uid)
            modified = True
        if udata.get("role") == ROLE_STUDENT and udata.get("user_id"):
            os.makedirs(f"data/_memory/{udata['user_id']}", exist_ok=True)
        # 封禁相关字段：存量用户补齐默认值（未封禁），旧数据无需人工迁移
        if "banned" not in udata:
            udata["banned"] = False
            modified = True
        if "ban_reason" not in udata:
            udata["ban_reason"] = ""
            modified = True
        if "banned_at" not in udata:
            udata["banned_at"] = None
            modified = True

    if modified:
        try:
            _save_users(users)
        except Exception as e:
            logging.warning("[用户数据迁移] 保存迁移后的 users 失败: %s", e)
    return users


def _save_users(users: dict):
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()


def _sanitize_username(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum() or ch in "_-")


ROLE_STUDENT = "student"
ROLE_PARENT = "parent"
ROLE_TEACHER = "teacher"
VALID_ROLES = (ROLE_STUDENT, ROLE_PARENT, ROLE_TEACHER)


def _generate_user_id(prefix: str = "U") -> str:
    """生成用户短码 ID（用于记忆体目录归属与绑定）。"""
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def register_user(username: str, password: str, role: str = ROLE_STUDENT,
                  age: int = None, guardian_user_id: str = "") -> dict:
    username = _sanitize_username(username)
    if not (2 <= len(username) <= 32):
        return {"ok": False, "message": "用户名需为 2-32 位字母/数字/下划线。"}
    if len(password) < 4:
        return {"ok": False, "message": "密码至少 4 位。"}
    if role not in VALID_ROLES:
        role = ROLE_STUDENT
    # 年龄校验：学生注册必须填写年龄；不满14周岁必须提供家长/教师 user_id
    if role == ROLE_STUDENT:
        if age is None:
            return {"ok": False, "message": "请填写您的年龄。"}
        try:
            age = int(age)
        except (TypeError, ValueError):
            return {"ok": False, "message": "年龄格式不正确。"}
        guardian_user_id = (guardian_user_id or "").strip().upper()
        if age < 14:
            if not guardian_user_id:
                return {"ok": False, "message": "未满14周岁，需填写家长或教师的 user_id。"}
            users_check = _load_users()
            guardian = next((u for u in users_check.values()
                             if u.get("role") in (ROLE_PARENT, ROLE_TEACHER)
                             and (u.get("user_id") or "").upper() == guardian_user_id), None)
            if not guardian:
                return {"ok": False, "message": "未找到该家长/教师账号（请确认 user_id 正确）。"}
    users = _load_users()
    if username in users:
        return {"ok": False, "message": "用户名已存在。"}
    salt = secrets.token_hex(16)
    user_id = _generate_user_id()
    # 防止短码碰撞：重试直到唯一
    used_ids = {u.get("user_id") for u in users.values() if u.get("user_id")}
    while user_id in used_ids:
        user_id = _generate_user_id()
    record = {
        "salt": salt,
        "hash": _hash_password(password, salt),
        "role": role,
        "user_id": user_id,
        "linked_students": [],
        "created_at": time.time(),
        "age": age if role == ROLE_STUDENT else None,
        "guardian_user_id": guardian_user_id if role == ROLE_STUDENT else "",
        "minor_mode": (age is not None and age < 14) if role == ROLE_STUDENT else False,
    }
    users[username] = record
    _save_users(users)
    os.makedirs(f"data/{username}", exist_ok=True)
    if role == ROLE_STUDENT:
        # 学生注册即创建独立记忆体目录 data/_memory/<user_id>
        os.makedirs(f"data/_memory/{user_id}", exist_ok=True)
    logging.info("[注册] 新用户 %s 已创建（role=%s, user_id=%s, age=%s, minor_mode=%s）",
                 username, role, user_id, age, record["minor_mode"])
    return {"ok": True, "username": username, "role": role, "user_id": user_id,
            "minor_mode": record["minor_mode"]}


def _user_bound_by(username: str, user_id: str, guardian_user_id: str, users: dict) -> list:
    """查找所有绑定该用户的用户（家长/教师绑定学生、监护人）。

    返回列表: [{"username": str, "user_id": str, "role": str, "relation": str}]
    """
    bound_by = []
    seen = set()
    sname = _sanitize_username(username)
    target_uid = (user_id or "").strip().upper()
    target_guardian = (guardian_user_id or "").strip().upper()

    for other_name, other_rec in users.items():
        if not isinstance(other_rec, dict) or _sanitize_username(other_name) == sname:
            continue
        other_uid = (other_rec.get("user_id") or "").strip()
        other_role = other_rec.get("role", "")
        # 1. 检查 other_rec 的 linked_students 是否包含 target_uid
        linked = _normalize_linked(other_rec.get("linked_students") or [])
        found_entry = None
        if target_uid:
            found_entry = next((e for e in linked if (e.get("user_id") or "").strip().upper() == target_uid), None)

        if found_entry:
            rel = (found_entry.get("relation") or "").strip()
            bound_by.append({
                "username": other_name,
                "user_id": other_uid,
                "role": other_role,
                "relation": rel,
            })
            seen.add(other_name)
        elif target_guardian and other_uid and other_uid.upper() == target_guardian:
            # 2. 检查是否为未成年人的登记监护人
            bound_by.append({
                "username": other_name,
                "user_id": other_uid,
                "role": other_role,
                "relation": "监护人",
            })
            seen.add(other_name)

    bound_by.sort(key=lambda x: x["username"])
    return bound_by


def _enrich_linked_students(linked: list, users: dict) -> list:
    """丰富 linked_students，确保包含 student_name 与对应的 username。"""
    uid_to_uname = {}
    for uname, urec in users.items():
        if isinstance(urec, dict):
            uid = (urec.get("user_id") or "").strip().upper()
            if uid:
                uid_to_uname[uid] = uname

    result = []
    for item in _normalize_linked(linked):
        uid = (item.get("user_id") or "").strip()
        matched_uname = uid_to_uname.get(uid.upper(), "")
        sname = item.get("student_name") or matched_uname or uid
        result.append({
            "user_id": uid,
            "student_name": sname,
            "username": matched_uname,
            "relation": item.get("relation", ""),
        })
    return result


def _user_profile(username: str, users: dict = None) -> dict:
    """返回用户资料（含角色/绑定/被绑定信息），不存在返回 None。"""
    sname = _sanitize_username(username)
    if users is None:
        users = _load_users()
    rec = users.get(sname)
    if not rec:
        return None
    uid = rec.get("user_id", "")
    guid = rec.get("guardian_user_id", "")
    return {
        "username": sname,
        "role": rec.get("role", ROLE_STUDENT),
        "user_id": uid,
        "linked_students": _enrich_linked_students(rec.get("linked_students") or [], users),
        "bound_by": _user_bound_by(sname, uid, guid, users),
        "created_at": rec.get("created_at"),
        "age": rec.get("age"),
        "guardian_user_id": guid,
        "minor_mode": bool(rec.get("minor_mode", False)),
        # 账号封禁状态（控制面板据此显示徽章与切换按钮文案）
        "banned": bool(rec.get("banned", False)),
        "ban_reason": str(rec.get("ban_reason") or ""),
        "banned_at": rec.get("banned_at"),
    }


def _normalize_linked(linked: list) -> list:
    """把 linked_students 规范化为对象数组 [{user_id, student_name, relation}]。

    兼容旧格式（纯字符串数组：仅 user_id），迁移为无关系对象。
    """
    result = []
    for item in linked or []:
        if isinstance(item, str):
            result.append({"user_id": item, "student_name": "", "relation": ""})
        elif isinstance(item, dict):
            result.append({
                "user_id": item.get("user_id", ""),
                "student_name": item.get("student_name", ""),
                "relation": item.get("relation", ""),
            })
    return result


def bind_student(username: str, student_user_id: str, relation: str = "") -> dict:
    """家长/老师绑定学生。返回 (ok, message)。

    关系规则：
      - 家长：关系原样保存（如"妈妈"、"爸爸"）。
      - 老师：关系自动补"老师"后缀（如"数学"→"数学老师"；已含"老师"则不重复）。
    """
    profile = _user_profile(username)
    if not profile:
        return False, "用户不存在。"
    if profile["role"] == ROLE_STUDENT:
        return False, "学生端无需绑定学生。"
    student_user_id = (student_user_id or "").strip().upper()
    if not student_user_id:
        return False, "请输入学生的 user_id。"
    # 不能绑定自己
    if (profile.get("user_id") or "").upper() == student_user_id:
        return False, "不能绑定自己，请绑定学生的 user_id。"
    # 校验该 user_id 对应真实学生账号（家长/老师只能绑定学生，不能绑定其它家长/老师）
    users = _load_users()
    target = next((u for u in users.values()
                   if u.get("role") == ROLE_STUDENT and (u.get("user_id") or "").upper() == student_user_id), None)
    if not target:
        return False, f"未找到 user_id 为 {student_user_id} 的学生（只能绑定学生账号）。"
    canonical = target["user_id"]
    # 学生用户名（用于显示名"xxx（学生）的xxx（关系）"）
    student_name = next((name for name, u in users.items()
                         if u.get("user_id") == canonical), canonical)
    # 关系规则：老师自动补"老师"后缀
    relation = (relation or "").strip()
    if profile["role"] == ROLE_TEACHER and relation and not relation.endswith("老师"):
        relation = relation + "老师"
    linked = _normalize_linked(profile.get("linked_students") or [])
    entry = {"user_id": canonical, "student_name": student_name, "relation": relation}
    found = next((e for e in linked if e["user_id"] == canonical), None)
    if found:
        # 已绑定：更新关系
        found["relation"] = relation
        found["student_name"] = student_name
    else:
        linked.append(entry)
    users[_sanitize_username(username)]["linked_students"] = linked
    _save_users(users)
    display = f"{student_name}的{relation}" if relation else canonical
    return True, f"已连接学生 {display}"


def verify_user(username: str, password: str) -> bool:
    username = _sanitize_username(username)
    users = _load_users()
    rec = users.get(username)
    if not rec:
        return False
    return _hash_password(password, rec["salt"]) == rec["hash"]


SESSION_TTL_SECONDS = 86400 * 7  # 登录状态在服务端保留 7 天，活跃时自动滑动续期


def _load_sessions(cleanup: bool = False) -> dict:
    sessions = {}
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    sessions = raw
        except (json.JSONDecodeError, OSError):
            sessions = {}

    if cleanup:
        now = time.time()
        valid = {}
        changed = False
        for token, val in sessions.items():
            if isinstance(val, dict):
                exp = val.get("expires_at", 0)
                if exp > now:
                    valid[token] = val
                else:
                    changed = True
            elif isinstance(val, str):
                # 兼容旧格式，赋予 7 天有效期
                valid[token] = {
                    "username": val,
                    "created_at": now,
                    "expires_at": now + SESSION_TTL_SECONDS
                }
                changed = True
        if changed:
            _save_sessions(valid)
        return valid
    return sessions


def _save_sessions(sessions: dict):
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)


def create_session(username: str) -> str:
    token = secrets.token_hex(24)
    now = time.time()
    sessions = _load_sessions(cleanup=True)
    sessions[token] = {
        "username": username,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS
    }
    _save_sessions(sessions)
    return token


def resolve_session(token: str):
    if not token:
        return None
    sessions = _load_sessions(cleanup=True)
    val = sessions.get(token)
    if not val:
        return None
    if isinstance(val, dict):
        now = time.time()
        if now > val.get("expires_at", 0):
            sessions.pop(token, None)
            _save_sessions(sessions)
            return None
        # 活跃会话自动滑动续期
        val["expires_at"] = now + SESSION_TTL_SECONDS
        _save_sessions(sessions)
        owner = val.get("username")
        # 封禁兜底：即使会话未被及时吊销（竞态/多进程），被封禁账号一律视同未登录。
        # 这里统一拦截，覆盖全部 30 处 resolve_session 调用点，不必逐个改。
        if owner and _is_user_banned_cached(owner):
            logging.info("[鉴权] 已封禁账号 %s 的会话被拒绝", owner)
            return None
        return owner
    if isinstance(val, str):
        if _is_user_banned_cached(val):
            return None
        return val
    return None


# 封禁状态缓存：users.json 每次请求都读盘代价偏高，这里做短 TTL 缓存，
# 封禁/解封/改密时用 _invalidate_ban_cache() 立即失效。
_BAN_CACHE = {}          # username -> (banned: bool, reason: str)
_BAN_CACHE_TS = 0.0
_BAN_CACHE_TTL = 3.0


def _invalidate_ban_cache():
    global _BAN_CACHE_TS
    _BAN_CACHE.clear()
    _BAN_CACHE_TS = 0.0


def _is_user_banned_cached(username: str) -> bool:
    """带短 TTL 缓存的封禁判断（热路径用，避免每次请求解析 users.json）。"""
    global _BAN_CACHE_TS
    if not username:
        return False
    now = time.time()
    if now - _BAN_CACHE_TS > _BAN_CACHE_TTL:
        try:
            users = _load_users()
            _BAN_CACHE.clear()
            for uname, rec in users.items():
                if isinstance(rec, dict) and rec.get("banned"):
                    _BAN_CACHE[uname] = str(rec.get("ban_reason") or "")
            _BAN_CACHE_TS = now
        except Exception:
            # 读盘异常时保守放行（不因缓存问题误锁用户），由登录接口严格拦截
            return False
    return username in _BAN_CACHE


# ---------------------------------------------------------------------------
# 账号封禁：生效链（登录拦截 / 会话语失效 / WebSocket 断开 / 鉴权兜底）
#   只标记 banned 是不够的——已登录客户端的 token 仍在有效期内，若不主动吊销，
#   被封禁用户可以继续访问，封禁就形同虚设。
# ---------------------------------------------------------------------------
def is_user_banned(username: str):
    """返回 (banned, reason)。用户不存在时返回 (False, "")。"""
    if not username:
        return False, ""
    try:
        rec = _load_users().get(_sanitize_username(username))
    except Exception:
        return False, ""
    if not isinstance(rec, dict):
        return False, ""
    if rec.get("banned"):
        return True, str(rec.get("ban_reason") or "")
    return False, ""


def _revoke_user_sessions(username: str) -> int:
    """吊销某用户的全部登录会话（改密/封禁后调用）。返回吊销数量。"""
    uname = _sanitize_username(username)
    if not uname:
        return 0
    try:
        sessions = _load_sessions(cleanup=False)
    except Exception:
        return 0
    removed = []
    for token, val in list(sessions.items()):
        owner = val.get("username") if isinstance(val, dict) else (val if isinstance(val, str) else None)
        if owner == uname:
            sessions.pop(token, None)
            removed.append(token)
    if removed:
        try:
            _save_sessions(sessions)
        except Exception:
            logging.exception("[账号封禁] 保存会话清理结果失败")
        logging.info("[账号封禁] 已吊销用户 %s 的 %d 个登录会话", uname, len(removed))
    return len(removed)


def _disconnect_user_ws(username: str) -> int:
    """断开某用户全部活跃客户端 WebSocket（封禁时调用），返回断开数量。"""
    uname = _sanitize_username(username)
    if not uname:
        return 0
    closed = 0
    try:
        with _ws_clients_lock:
            conns = list(_ws_user_clients.get(uname) or [])
            _ws_user_clients.pop(uname, None)
        for ws in conns:
            try:
                asyncio.create_task(ws.close(code=1008, message=b"account banned"))
                closed += 1
            except Exception:
                pass
    except Exception:
        logging.exception("[账号封禁] 断开用户 %s 的 WebSocket 失败", uname)
    return closed


def _purge_user_runtime_state(username: str):
    """清掉该用户的心跳/暂停/退出计划等运行态，避免残留标记影响后续登录。"""
    uname = _sanitize_username(username)
    if not uname:
        return
    try:
        _USER_HEARTBEATS.pop(uname, None)
        _USER_PAUSED.pop(uname, None)
        _PENDING_EXIT_USERS.pop(uname, None)
        _PENDING_COGNITION_USERS.pop(uname, None)
    except Exception:
        pass


def get_token_from_request(request, data=None):
    token = None
    if data and isinstance(data, dict):
        token = data.get("token")
    if not token:
        token = request.query.get("token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    if not token:
        token = request.cookies.get("nhy_user_token")
    return token or ""


# ------------------------------------------------------------------------
# 网络安全：IP/用户双重限流 + IP 黑白名单控制 + 时间戳签名防重放
# ------------------------------------------------------------------------
_RATE_LIMIT_IP = defaultdict(deque)          # ip -> deque[timestamps]
_RATE_LIMIT_USER = defaultdict(deque)         # username -> deque[timestamps]
_RATE_IP_PER_SEC = 10                         # 用户需求：IP 访问限制严格设定为每秒最多 10 次
_RATE_USER_PER_SEC = 20
_RATE_WINDOW = 1.0
_REPLAY_CACHE = set()                        # 已使用的 (token, ts, nonce)
_REPLAY_CACHE_LOCK = threading.Lock()

# IP 访问控制黑白名单持久化与内存缓存
_IP_RULES_LOCK = threading.Lock()
_IP_RULES_CACHE = {"whitelist": set(), "blacklist": set()}


def _normalize_ip_str(raw: str) -> str:
    return str(raw or "").strip()


def _load_ip_rules() -> dict:
    """读取已保存的 IP 黑白名单，并同步刷新内存 set 缓存。"""
    res = {"whitelist": [], "blacklist": []}
    try:
        if IP_RULES_FILE.exists():
            data = json.loads(IP_RULES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                wl = [ip for ip in (_normalize_ip_str(x) for x in data.get("whitelist", [])) if ip]
                bl = [ip for ip in (_normalize_ip_str(x) for x in data.get("blacklist", [])) if ip]
                res = {"whitelist": sorted(list(set(wl))), "blacklist": sorted(list(set(bl)))}
    except Exception as e:
        logging.warning("[IP规则] 读取 IP 规则文件失败: %s", e)
    with _IP_RULES_LOCK:
        _IP_RULES_CACHE["whitelist"] = set(res["whitelist"])
        _IP_RULES_CACHE["blacklist"] = set(res["blacklist"])
    return res


def _save_ip_rules(rules: dict):
    """原子写入 IP 规则（临时文件 + os.replace）。"""
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    wl = sorted(list(set([ip for ip in (_normalize_ip_str(x) for x in rules.get("whitelist", [])) if ip])))
    bl = sorted(list(set([ip for ip in (_normalize_ip_str(x) for x in rules.get("blacklist", [])) if ip])))
    clean_data = {"whitelist": wl, "blacklist": bl, "updated_at": time.time()}
    tmp = IP_RULES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean_data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, IP_RULES_FILE)
    with _IP_RULES_LOCK:
        _IP_RULES_CACHE["whitelist"] = set(wl)
        _IP_RULES_CACHE["blacklist"] = set(bl)
    logging.info("[IP规则] IP 控制规则已更新：白名单 %d 个，黑名单 %d 个", len(wl), len(bl))


def is_ip_whitelisted(ip: str) -> bool:
    """判断 IP 是否在白名单中（白名单 IP 豁免每秒 10 次访问限制及密码风控）。"""
    if not ip:
        return False
    with _IP_RULES_LOCK:
        return ip in _IP_RULES_CACHE["whitelist"]


def is_ip_blacklisted(ip: str) -> bool:
    """判断 IP 是否在黑名单中（黑名单 IP 直接阻断，禁止一切访问）。"""
    if not ip:
        return False
    with _IP_RULES_LOCK:
        return ip in _IP_RULES_CACHE["blacklist"]


def _rate_allow(bucket: dict, key: str, limit: int, window: float = _RATE_WINDOW) -> bool:
    now = time.time()
    q = bucket[key]
    # 移除窗口外的时间戳
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _get_request_ip(request) -> str:
    """提取客户端请求的真实 IP 地址。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    x_real = request.headers.get("X-Real-IP", "")
    if x_real:
        return x_real.strip()
    return (request.remote or "unknown").strip()


def check_ip_rate_limit(request) -> bool:
    """单 IP 每秒最多 10 次请求，超出则拒绝（白名单 IP 直接放行）。"""
    ip = _get_request_ip(request)
    if is_ip_whitelisted(ip):
        return True
    return _rate_allow(_RATE_LIMIT_IP, ip, _RATE_IP_PER_SEC)


# ------------------------------------------------------------------------
# 登录密码防爆破风控系统：
# 规则：
# 1. 单 IP 累计连续 3 次密码错误触发风控封禁。
# 2. 封禁时长呈指数级递增：
#    - 首次封禁：2^1 = 2 分钟 (120 秒)
#    - 若密码正确则重置清零（归零重置）
#    - 解封后若再连错 3 次，封禁 2^2 = 4 分钟 (240 秒)
#    - 若密码正确则重置清零
#    - 解封后若再连错 3 次，封禁 2^3 = 8 分钟 (480 秒)，以此类推 2^k 分钟。
# 3. 封禁期间内该 IP 尝试任何登录直接拦截并返回剩余解封时间。
# ------------------------------------------------------------------------
_IP_AUTH_RISK_LOCK = threading.Lock()
_IP_AUTH_RISK_DATA = {}  # ip -> {"failed_count": int, "strike_level": int, "blocked_until": float}


def check_ip_login_blocked(ip: str) -> tuple:
    """检查指定 IP 是否正处于风控封禁状态（白名单 IP 始终豁免）。

    返回 (is_blocked: bool, remaining_seconds: int, block_minutes: int)
    """
    if is_ip_whitelisted(ip):
        return False, 0, 0
    now = time.time()
    with _IP_AUTH_RISK_LOCK:
        rec = _IP_AUTH_RISK_DATA.get(ip)
        if not rec:
            return False, 0, 0
        blocked_until = rec.get("blocked_until", 0)
        if now < blocked_until:
            rem = max(1, int(blocked_until - now))
            level = max(1, rec.get("strike_level", 1))
            return True, rem, int(2 ** level)
    return False, 0, 0


def record_login_failure(ip: str) -> tuple:
    """记录一次登录密码错误（白名单 IP 豁免封禁）。

    返回 (is_blocked_now: bool, block_minutes: int, remaining_seconds: int, failed_count: int)
    """
    if is_ip_whitelisted(ip):
        return False, 0, 0, 0
    now = time.time()
    with _IP_AUTH_RISK_LOCK:
        rec = _IP_AUTH_RISK_DATA.setdefault(ip, {"failed_count": 0, "strike_level": 0, "blocked_until": 0})
        # 若之前封禁时间已过，重置本次错误计数（保留 strike_level 供递增）
        if now >= rec.get("blocked_until", 0) and rec["blocked_until"] > 0:
            rec["blocked_until"] = 0
            rec["failed_count"] = 0

        rec["failed_count"] += 1
        if rec["failed_count"] >= 3:
            # 达到 3 次错误，触发/递增风控等级
            rec["strike_level"] += 1
            # 封禁分钟数 = 2 ^ strike_level (如 level 1 为 2 分钟，level 2 为 4 分钟，level 3 为 8 分钟)
            block_mins = int(2 ** rec["strike_level"])
            block_secs = block_mins * 60
            rec["blocked_until"] = now + block_secs
            rec["failed_count"] = 0  # 触发封禁后清零本轮失败计数
            logging.warning("[风控拦截] IP %s 连续 3 次密码错误，已触发第 %d 级风控封禁（封禁 %d 分钟）",
                            ip, rec["strike_level"], block_mins)
            return True, block_mins, block_secs, 3
        return False, 0, 0, rec["failed_count"]


def record_login_success(ip: str):
    """登录成功时彻底清零该 IP 的所有错误记录与风控等级。"""
    with _IP_AUTH_RISK_LOCK:
        if ip in _IP_AUTH_RISK_DATA:
            _IP_AUTH_RISK_DATA.pop(ip, None)
            logging.info("[风控恢复] IP %s 密码验证成功，已清零风控状态", ip)


def check_user_rate_limit(username: str) -> bool:
    """单用户每秒最多 3 次请求，超出则拒绝。"""
    if not username:
        return True
    return _rate_allow(_RATE_LIMIT_USER, username, _RATE_USER_PER_SEC)


def _session_sign_key(token: str) -> str:
    """由服务端随机 sign_secret 与会话 token 派生每会话签名密钥。

    密钥在登录/注册时随会话下发，仅对该 token 有效；源码与客户端静态文件里
    不保存任何硬编码密钥，避免公开仓库中出现可复用凭据。
    """
    secret = str(config.get("sign_secret") or "").strip()
    if not secret or not token:
        return ""
    return hmac.new(
        secret.encode("utf-8"),
        f"sign|{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_request_signature(request, username: str, token: str) -> bool:
    """时间戳 + nonce + 签名校验，防止同一 Token 被无限重放。

    要求请求头携带：
      X-Timestamp: 秒级时间戳
      X-Nonce:     随机串（每次请求唯一）
      X-Sign:      hmac_sha256(session_sign_key, f"{username}|{timestamp}|{nonce}")

    其中 session_sign_key 由服务端在登录/注册时按会话下发（见 _session_sign_key）。

    向后兼容：客户端在非 HTTPS 安全上下文（如 http://IP:8987）下无法调用
    WebCrypto 生成签名时不会携带这三个头，此时直接放行（仅依赖 Bearer 会话认证），
    只有提供了签名头才进行严格校验。
    """
    try:
        ts = request.headers.get("X-Timestamp", "")
        nonce = request.headers.get("X-Nonce", "")
        sign = request.headers.get("X-Sign", "")
        # 签名头缺失：非安全上下文兼容放行（仅 Bearer 认证）
        if not ts or not nonce or not sign:
            return True
        ts_f = float(ts)
        # 时间窗：允许 ±300 秒时钟偏差
        if abs(time.time() - ts_f) > 300:
            return False
        sign_key = _session_sign_key(token)
        if not sign_key:
            # 未配置服务端签名密钥（理论上不会发生）：退化为仅 Bearer 认证
            return True
        expected = hmac.new(
            sign_key.encode("utf-8"),
            f"{username}|{ts}|{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sign):
            return False
        # 防重放：同一 (token, ts, nonce) 只能使用一次
        replay_key = f"{token}|{ts}|{nonce}"
        with _REPLAY_CACHE_LOCK:
            if replay_key in _REPLAY_CACHE:
                return False
            _REPLAY_CACHE.add(replay_key)
            if len(_REPLAY_CACHE) > 50000:
                _REPLAY_CACHE.clear()
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------
# 按用户切换数据目录并加载其记忆沙箱
# ------------------------------------------------------------------------
_current_loaded_user = None


def load_user_context(username: str, force: bool = False):
    """切换数据目录到 data/<username>/，重置并加载该用户独立数据。"""
    global _current_loaded_user
    if not force and _current_loaded_user == username:
        from core.memory_engine import memories
        return memories

    user_dir = f"data/{username}"
    os.makedirs(user_dir, exist_ok=True)
    # 单用户兼容：记忆体与历史对话同目录 data/<username>
    runtime_paths.set_user(username)

    from core.memory_engine import reset_engine_state, memories, _init_metrics_counters
    from utils.persistence import load_all_data, load_state
    from utils.dialogue_state import reset_state
    from utils import message_history
    from core.llm_interface import load_dialogue_history
    from core import concept_store

    reset_engine_state()
    reset_state()
    message_history._message_history.clear()
    message_history._flushed_count = 0
    load_all_data()
    load_state()
    load_dialogue_history()
    _init_metrics_counters()
    # 概念层：随用户切换重建（失败只记日志，不阻断该用户的加载）
    try:
        concept_store.reset_concept_state()
        concept_store.reload_from_db()
        logging.info("概念层已加载（用户 %s）：%s", username, concept_store.stats())
    except Exception:
        logging.exception("概念层初始化失败（用户 %s），本次将不启用概念检索", username)
    _current_loaded_user = username
    return memories


# ------------------------------------------------------------------------
# 用户网关 HTTP 处理器（/gateway/* 与客户端静态资源）
# ------------------------------------------------------------------------
async def client_index_handler(request):
    """用户客户端主入口（动态注入静态资源版本号，避免浏览器缓存旧 JS/CSS）。"""
    index_file = CLIENT_DIR / "index.html"
    try:
        html = index_file.read_text(encoding="utf-8")
        def _v(relpath):
            f = CLIENT_DIR / relpath
            return int(f.stat().st_mtime) if f.exists() else 0
        # 把固定的 ?v=xxx 替换为基于文件修改时间的版本号
        import re as _re
        html = _re.sub(r'(style\.css)\?v=[^"]*', lambda m: f"{m.group(1)}?v={_v('style.css')}", html)
        html = _re.sub(r'(nav/nav_style\.css)\?v=[^"]*', lambda m: f"{m.group(1)}?v={_v('nav/nav_style.css')}", html)
        html = _re.sub(r'(app\.js)\?v=[^"]*', lambda m: f"{m.group(1)}?v={_v('app.js')}", html)
        html = _re.sub(r'(nav/nav_script\.js)\?v=[^"]*', lambda m: f"{m.group(1)}?v={_v('nav/nav_script.js')}", html)
    except Exception:
        return web.FileResponse(index_file)
    resp = web.Response(text=html, content_type="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


async def client_config_handler(request):
    """客户端配置文件。"""
    resp = web.FileResponse(CLIENT_DIR / "client_config.json")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


async def client_asset_handler(request):
    """客户端静态资源（index.html 使用相对路径，服务端下发模式需按名映射到 client/）。"""
    name = request.match_info["name"]
    # 按请求路径前缀确定子目录（/nav/* → client/nav/，/images/* → client/images/）
    prefix = ""
    if request.path.startswith("/nav/"):
        prefix = "nav"
    elif request.path.startswith("/images/"):
        prefix = "images"
    target = (CLIENT_DIR / prefix / name).resolve() if prefix else (CLIENT_DIR / name).resolve()
    # 防目录穿越：仅允许 client/ 内的文件
    try:
        target.relative_to(CLIENT_DIR.resolve())
    except ValueError:
        return web.Response(status=404, text="Not Found")
    if target.is_file():
        resp = web.FileResponse(target)
        # 图片资源（头像/广告/logo）：改用条件缓存（ETag/Last-Modified → 304 协商），
        # 避免每次进列表都重新下载头像（此前 no-store 会导致反复拉取，加载明显变慢）。
        # 文件被覆盖时 ETag 随 mtime/size 变化，浏览器会自动拿到新图，不会读到旧缓存。
        if prefix == "images" or target.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico"):
            resp.headers["Cache-Control"] = "no-cache"   # 允许缓存但每次向服务端校验
        else:
            # 代码资源（style.css/app.js 等）禁用缓存：确保刷新即加载最新代码
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp
    return web.Response(status=404, text="Not Found")


async def chat_image_handler(request):
    """访问聊天图片：GET /chat_images/{name}，指向 data/chat_images/<name>。"""
    name = request.match_info.get("name", "")
    if not name or ".." in name or "/" in name:
        return web.Response(status=404, text="Not Found")
    target = (PROJECT_DIR / "data" / "chat_images" / name).resolve()
    chat_root = (PROJECT_DIR / "data" / "chat_images").resolve()
    try:
        target.relative_to(chat_root)
    except ValueError:
        return web.Response(status=404, text="Not Found")
    if target.is_file():
        return web.FileResponse(target)
    return web.Response(status=404, text="Not Found")


async def user_avatar_handler(request):
    """访问用户头像：GET /images/user_avatars/{name}，指向 client/images/user_avatars/<name>。

    头像文件按固定路径覆盖写入，若浏览器缓存了旧图，换头像后仍会显示旧头像，
    因此这里强制 no-cache 并每次向服务端校验（配合前端 ?v= 时间戳彻底避免陈旧）。
    """
    name = request.match_info.get("name", "")
    if not name or ".." in name or "/" in name:
        return web.Response(status=404, text="Not Found")
    target = (CLIENT_DIR / "images" / "user_avatars" / name).resolve()
    av_root = (CLIENT_DIR / "images" / "user_avatars").resolve()
    try:
        target.relative_to(av_root)
    except ValueError:
        return web.Response(status=404, text="Not Found")
    if target.is_file():
        resp = web.FileResponse(target)
        # 条件缓存：允许浏览器缓存并携带 ETag 回服务端校验（命中返回 304，不再传图），
        # 兼顾"换头像后能看到新图"（文件 mtime/size 变化 → ETag 变化）与"加载速度"。
        # 前端上传后还会给 URL 加 ?v=<时间戳>，进一步确保自己立刻看到新头像。
        resp.headers["Cache-Control"] = "no-cache"
        return resp
    return web.Response(status=404, text="Not Found")


async def client_ads_handler(request):
    """访问服务赞助广告图：GET /images/ads/{name}，指向 client/images/ads/<name>。"""
    name = request.match_info.get("name", "")
    if not name or ".." in name or "/" in name:
        return web.Response(status=404, text="Not Found")
    target = (CLIENT_DIR / "images" / "ads" / name).resolve()
    ads_root = (CLIENT_DIR / "images" / "ads").resolve()
    try:
        target.relative_to(ads_root)
    except ValueError:
        return web.Response(status=404, text="Not Found")
    if target.is_file():
        return web.FileResponse(target)
    return web.Response(status=404, text="Not Found")


async def admin_index_handler(request):
    """管理员控制面板主入口（动态给静态资源注入版本号，避免浏览器缓存旧 JS/CSS）。"""
    index_file = WEBUI_DIR / "index.html"
    try:
        html = index_file.read_text(encoding="utf-8")
        def _v(name):
            f = WEBUI_DIR / name
            return int(f.stat().st_mtime) if f.exists() else 0
        html = html.replace('/admin_static/style.css', f'/admin_static/style.css?v={_v("style.css")}')
        html = html.replace('/admin_static/app.js', f'/admin_static/app.js?v={_v("app.js")}')
    except Exception:
        return web.FileResponse(index_file)
    resp = web.Response(text=html, content_type="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


async def gateway_api_broadcast(request):
    """客户端读取全站广播：GET /gateway/broadcast

    未启用或内容为空时返回 enabled=False，客户端据此不弹窗。
    客户端本地用 localStorage 记录"已读内容指纹"与"不再提醒"，
    内容被管理员更新后指纹变化，会再次弹窗提醒。
    """
    try:
        rec = await asyncio.to_thread(_load_broadcast)
    except Exception:
        rec = {"enabled": False, "content": "", "updated_at": None}
    return web.json_response({
        "ok": True,
        "enabled": bool(rec.get("enabled")) and bool((rec.get("content") or "").strip()),
        "content": rec.get("content", ""),
        "updated_at": rec.get("updated_at"),
    })


async def gateway_api_register(request):
    data = await request.json()
    result = register_user(data.get("username", ""), data.get("password", ""),
                           data.get("role", ROLE_STUDENT),
                           age=data.get("age"),
                           guardian_user_id=data.get("guardian_user_id", ""))
    if result["ok"]:
        token = create_session(result["username"])
        result["token"] = token
        result["sign_key"] = _session_sign_key(token)
        result["avatar"] = ""   # 新注册用户必然无头像，显式下发避免前端沿用上一位用户
        resp = web.json_response(result)
        resp.set_cookie("nhy_user_token", token, max_age=86400 * 7, httponly=True, samesite="Lax", path="/", secure=request.url.scheme == "https")
        return resp
    return web.json_response(result)


async def gateway_api_login(request):
    ip = _get_request_ip(request)
    # 风控检查：若当前 IP 处于封禁期，直接拦截并告知剩余等待时间
    is_blocked, rem_sec, block_mins = check_ip_login_blocked(ip)
    if is_blocked:
        rem_min = max(1, (rem_sec + 59) // 60)
        return web.json_response({
            "ok": False,
            "blocked": True,
            "remaining_seconds": rem_sec,
            "message": f"密码错误次数过多，该IP已被风控限制登录，请于 {rem_min} 分钟后重试。"
        }, status=429)

    data = await request.json()
    username = _sanitize_username(data.get("username", ""))
    password = data.get("password", "")
    # 账号封禁拦截：即使密码正确也拒绝登录，且不签发任何 token
    banned, ban_reason = is_user_banned(username)
    if banned:
        logging.warning("[登录] 已封禁账号 %s 尝试登录 (IP=%s)，已拒绝", username, ip)
        return web.json_response({
            "ok": False,
            "banned": True,
            "message": f"该账号已被管理员封禁，无法登录。{('原因：' + ban_reason) if ban_reason else ''}".strip(),
        }, status=403)
    if verify_user(username, password):
        # 密码正确：清零该 IP 的所有风控与错误记录
        record_login_success(ip)
        token = create_session(username)
        logging.info("[登录] 用户 %s 登录成功 (IP=%s)", username, ip)
        profile = _user_profile(username) or {}
        # 头像：存在上传文件才返回 URL（否则空，前端回退字母）
        # 必须随登录一并下发，否则前端沿用上一位登录者内存中的 state.userAvatar，
        # 造成"未清缓存时显示上一个人的头像"。
        av_file = CLIENT_DIR / "images" / "user_avatars" / f"{username}.png"
        resp = web.json_response({
            "ok": True, "username": username, "token": token,
            "sign_key": _session_sign_key(token),
            "role": profile.get("role", ROLE_STUDENT),
            "user_id": profile.get("user_id", ""),
            "linked_students": profile.get("linked_students", []),
            "age": profile.get("age"),
            "guardian_user_id": profile.get("guardian_user_id", ""),
            "minor_mode": bool(profile.get("minor_mode", False)),
            "avatar": (f"/images/user_avatars/{username}.png" if av_file.exists() else ""),
        })
        # 同步下发 HttpOnly + Secure Cookie（防止 XSS 窃取 Token；HTTPS 下标记 Secure）
        resp.set_cookie("nhy_user_token", token, max_age=86400 * 7, httponly=True,
                        samesite="Lax", path="/", secure=request.url.scheme == "https")
        return resp

    # 密码错误：记录失败次数并在达到 3 次时触发指数级风控封禁 (2^k 分钟)
    is_blocked_now, block_mins, block_secs, failed_count = record_login_failure(ip)
    if is_blocked_now:
        return web.json_response({
            "ok": False,
            "blocked": True,
            "remaining_seconds": block_secs,
            "message": f"密码连续错误 3 次，该IP已被风控限制登录 {block_mins} 分钟，请稍后再试。"
        }, status=429)

    remain_tries = max(0, 3 - failed_count)
    return web.json_response({
        "ok": False,
        "message": f"用户名或密码错误（连续错误 3 次将风控该IP，还剩 {remain_tries} 次机会）。"
    }, status=401)


async def gateway_api_bind(request):
    """家长/老师绑定学生：POST /gateway/bind  body: {token, student_user_id, relation}"""
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    ok, msg = bind_student(username, data.get("student_user_id", ""),
                           data.get("relation", ""))
    if not ok:
        return web.json_response({"ok": False, "message": msg})
    profile = _user_profile(username) or {}
    return web.json_response({"ok": True, "message": msg,
                              "linked_students": profile.get("linked_students", [])})


async def gateway_api_bot_info(request):
    # 机器人头像：优先返回已上传的固定文件；否则返回默认图，避免前端加载不存在的图 404
    av_file = CLIENT_DIR / "images" / "bot_avatar.jpg"
    default_file = CLIENT_DIR / "images" / "bot_avatar_default.jpg"
    bot_avatar = "/images/bot_avatar.jpg" if av_file.exists() else None
    bot_avatar_default = "/images/bot_avatar_default.jpg" if default_file.exists() else None
    return web.json_response({
        "ok": True,
        "bot_name": BOT_NAME,
        "bot_avatar": bot_avatar,
        "bot_avatar_default": bot_avatar_default,
    })


async def gateway_api_profile(request):
    """获取当前登录用户资料（角色 / user_id / 绑定学生），供页面刷新后恢复。"""
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    record_heartbeat(username)  # 已登录用户的任何活跃请求都刷新其心跳
    profile = _user_profile(username) or {}
    # 用户头像：存在上传文件则返回 URL，否则空（前端回退字母）
    av_file = CLIENT_DIR / "images" / "user_avatars" / f"{username}.png"
    avatar = f"/images/user_avatars/{username}.png" if av_file.exists() else ""
    return web.json_response({
        "ok": True,
        "username": username,
        "role": profile.get("role", ROLE_STUDENT),
        "user_id": profile.get("user_id", ""),
        "linked_students": profile.get("linked_students", []),
        "age": profile.get("age"),
        "guardian_user_id": profile.get("guardian_user_id", ""),
        "minor_mode": bool(profile.get("minor_mode", False)),
        "avatar": avatar,
    })


async def gateway_api_delete_account(request):
    """注销账号：POST /gateway/delete_account  body: {token}

    学生：删除用户记录 + data/<username>/ + data/_memory/<user_id>/（彻底删除记忆体），
         并从其它用户 linked_students 中移除对该学生的引用。
    家长/老师：仅删除用户记录 + data/<username>/；保留学生记忆体（以学生为中心），
         并在其绑定学生记录中清空该家长/老师自己的引用。
    """
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    profile = _user_profile(username) or {}
    role = profile.get("role", ROLE_STUDENT)
    user_id = profile.get("user_id", "")

    users = _load_users()
    uname = _sanitize_username(username)

    # 从所有用户的 linked_students 中移除该用户引用（家长/教师 → 学生记录）
    if role in (ROLE_PARENT, ROLE_TEACHER):
        for other, urec in users.items():
            linked = _normalize_linked(urec.get("linked_students") or [])
            new_linked = [e for e in linked if e.get("user_id") != user_id]
            if len(new_linked) != len(linked):
                urec["linked_students"] = new_linked
    # 学生：从所有家长/教师 linked_students 移除对该学生引用
    elif role == ROLE_STUDENT:
        for other, urec in users.items():
            linked = _normalize_linked(urec.get("linked_students") or [])
            new_linked = [e for e in linked if e.get("user_id") != user_id]
            if len(new_linked) != len(linked):
                urec["linked_students"] = new_linked

    # 删除该用户自己的记录
    if uname in users:
        del users[uname]
    _save_users(users)

    # 清理目录
    import shutil
    try:
        if os.path.isdir(f"data/{uname}"):
            shutil.rmtree(f"data/{uname}")
    except Exception:
        pass
    if role == ROLE_STUDENT and user_id:
        # 学生注销：彻底删除记忆体目录（含记忆/FAISS/SQLite）
        try:
            mem_dir = f"data/_memory/{user_id}"
            if os.path.isdir(mem_dir):
                shutil.rmtree(mem_dir)
        except Exception:
            pass

    # 清空会话
    sessions = _load_sessions()
    sessions.pop(token, None)
    _save_sessions(sessions)

    logging.info("[注销] 用户 %s（role=%s, user_id=%s）已注销并清理数据", uname, role, user_id)
    return web.json_response({"ok": True, "message": "账号已注销"})


async def gateway_api_minor_mode(request):
    """家长/老师为其绑定学生设置未成年人模式：POST /gateway/minor_mode  body: {token, student_user_id, enabled}"""
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    profile = _user_profile(username) or {}
    if profile.get("role") in (ROLE_STUDENT,):
        return web.json_response({"ok": False, "message": "学生端无权设置未成年人模式。"}, status=403)
    # 校验：该学生必须在该家长/老师 linked_students 中
    own_uid = profile.get("user_id", "")
    linked = [e.get("user_id") for e in profile.get("linked_students") or [] if e.get("user_id")]
    stu_uid = (data.get("student_user_id") or "").strip().upper()
    if not stu_uid or stu_uid not in linked:
        return web.json_response({"ok": False, "message": "未绑定该学生，无法设置。"}, status=403)
    enabled = bool(data.get("enabled", False))

    users = _load_users()
    # 找到该学生用户名
    stu_name = next((n for n, u in users.items() if (u.get("user_id") or "").upper() == stu_uid), None)
    if not stu_name:
        return web.json_response({"ok": False, "message": "未找到该学生账号。"}, status=404)
    users[stu_name]["minor_mode"] = enabled
    _save_users(users)
    logging.info("[未成年模式] 用户 %s 将学生 %s 的未成年模式设为 %s", username, stu_name, enabled)
    return web.json_response({"ok": True, "student_user_id": stu_uid,
                              "minor_mode": enabled})


def _resolve_memory_scope(username: str, memory_user_id: str = None):
    """解析当前对话的记忆体归属与角色。

    返回 (memory_user_id, role) 或 None（无权限）。
    - 学生：记忆体 = 自己的 user_id
    - 家长/老师：memory_user_id 必须在其 linked_students 内（未指定则取第一个绑定学生）
    """
    profile = _user_profile(username) or {}
    role = profile.get("role", ROLE_STUDENT)
    own_uid = profile.get("user_id", "") or username
    if role == ROLE_STUDENT:
        return own_uid, role
    # 家长/老师：必须在绑定学生列表中（linked_students 为对象数组）
    linked = [e["user_id"] for e in profile.get("linked_students") or [] if e.get("user_id")]
    muid = (memory_user_id or "").strip()
    if not muid:
        if not linked:
            return None  # 未绑定学生
        muid = linked[0]
    else:
        if muid not in linked:
            return None  # 无权访问该学生记忆体
    return muid, role


def _history_msg_id(username: str, sender: str, t, content: str) -> str:
    """为历史消息生成**稳定**的唯一 id（供前端引用跳转按 id 找回原文）。

    注意：绝不能用「页内下标」参与 id —— 分页是多页切片的，新增一条消息就会让
    页内下标整体位移，导致引用卡片里存的旧 id 失配（表现为点引用跳不过去）。
    新写入的历史记录已自带 id（见 utils/message_history._with_id），此函数只用于
    兜底旧数据：用 时间+发送者+内容 的哈希构造，内容不变则 id 永远不变。
    """
    raw = f"{username}|{sender}|{t}|{content}"
    return "h_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


async def gateway_api_history(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    record_heartbeat(username)  # 拉取历史也是用户活跃信号

    scope = _resolve_memory_scope(username, request.query.get("memory_user_id"))
    if scope is None:
        return web.json_response({"ok": False, "message": "未绑定学生，无法获取对话。"}, status=403)
    muid, role = scope

    # 分页：before 为时间游标（更早的消息），缺省返回最新一页；limit 默认 30
    # 注意：历史记录里存的是"虚拟时钟时间"（clock.now()），而前端一律使用真实
    # Unix 时间戳。因此在出口把 time/cognition_start 转成真实时间，入参 before
    # 反向还原成虚拟时间，保证两边时间域一致（否则前端会显示成 1970 年）。
    from core.virtual_clock import clock as _clock
    before_raw = request.query.get("before")
    before = None
    if before_raw:
        try:
            before = _clock.to_virtual_time(float(before_raw))
        except (TypeError, ValueError):
            before = None
    limit = 30

    def _get():
        from utils.message_history import read_history_page_for
        page, has_more = read_history_page_for(str(PROJECT_DIR), username, before=before, limit=limit)
        # 规整字段（与旧逻辑一致，额外带 mtype/image 供前端渲染图片）
        # 并注入稳定唯一 id，供前端做增量消息比对和去重
        res = []
        for i, m in enumerate(page):
            text = m.get("content") if "content" in m else m.get("text", "")
            t = m.get("time")
            cs = m.get("cognition_start")
            sender = m.get("sender", "未知")
            real_t = _clock.to_real_time(t) if t is not None else None
            msg_id = m.get("id") or _history_msg_id(username, sender, t, text)
            res.append({
                "id": msg_id,
                "sender": sender,
                "content": text,
                "source": m.get("source", "客户端"),
                "time": real_t,
                "cognition_start": (_clock.to_real_time(cs) if cs is not None else None),
                "mtype": m.get("mtype"),
                "image": m.get("image"),
                "image_desc": m.get("image_desc"),
                "quote": m.get("quote"),
            })
        return res, has_more

    msgs, has_more = await asyncio.to_thread(_get)
    return web.json_response({"ok": True, "messages": msgs, "has_more": has_more,
                              "memory_user_id": muid, "role": role})


# ---------------------------------------------------------------------------
# 图片上传（聊天图片 + 用户头像）：URL 指针存储，大小 ≤10MB
# ---------------------------------------------------------------------------
_UPLOAD_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_UPLOAD_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_CHAT_IMAGES_DIR = PROJECT_DIR / "data" / "chat_images"
_USER_AVATAR_DIR = CLIENT_DIR / "images" / "user_avatars"

# 头像统一落盘规格：最长边像素与 JPEG 质量。
# 头像在界面上最大也只显示 ~42px，保留原始分辨率（常见 512~2000px）纯属浪费：
# 经服务端压缩后单张通常从数百 KB 降到 5~20KB，显著加快首屏/列表加载。
_AVATAR_MAX_EDGE = 256
_AVATAR_JPEG_QUALITY = 82


def _normalize_avatar_bytes(data: bytes) -> tuple:
    """把任意上传图片统一压缩为正方形 JPEG 字节，返回 (bytes, err)。

    - 等比缩放到最长边 _AVATAR_MAX_EDGE，居中裁剪为正方形（头像展示为方形/圆形）
    - 转 RGB 后用 JPEG 编码（透明区域填白底）
    - Pillow 不可用或解码失败时返回 (None, err)，由调用方决定回退原图
    """
    try:
        from PIL import Image
        import io as _io
    except Exception as e:
        return None, f"未安装 Pillow（{e}）"
    try:
        im = Image.open(_io.BytesIO(data))
        im.load()
    except Exception as e:
        return None, f"无法解析图片（{e}）"
    try:
        # 统一为 RGB：PNG 透明/调色板/灰度都要转，否则 JPEG 保存报错
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        w, h = im.size
        if not w or not h:
            return None, "图片尺寸无效"
        # 先缩放到"最短边 = MAX_EDGE"再中心裁剪，保证正方形且不过度放大
        scale = _AVATAR_MAX_EDGE / min(w, h)
        if scale < 1:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        w, h = im.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        if (w, h) != (side, side):
            im = im.crop((left, top, left + side, top + side))
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=_AVATAR_JPEG_QUALITY, optimize=True)
        return out.getvalue(), ""
    except Exception as e:
        return None, f"压缩失败（{e}）"


def _is_image_upload_ok(reader):
    """校验上传图片的类型与大小，返回 (ok, ext, data, err)。"""
    ctype = (reader.content_type or "").lower()
    ext = _UPLOAD_IMAGE_TYPES.get(ctype)
    if not ext:
        return False, "", None, f"不支持的图片类型 {ctype or '未知'}（仅支持 jpg / png / webp / gif）"
    data = reader.file.read()
    if len(data) > _UPLOAD_IMAGE_MAX_BYTES:
        return False, "", None, "图片过大（限制 10MB）"
    if not data:
        return False, "", None, "图片内容为空"
    return True, ext, data, ""


async def _describe_chat_image(local_path):
    """生成聊天图片的内容描述（多模态），失败/未配置时降级返回空字符串。"""
    try:
        from core.llm_interface import describe_image_from_path
        desc = await describe_image_from_path(str(local_path))
        if desc and not desc.startswith("[图片"):
            return str(desc).strip()
    except Exception as e:
        logging.debug("[图片] 生成图片描述失败: %s", e)
    return ""


# ---------------------------------------------------------------------------
# 表情包（贴纸）目录约定与分发
#   stickers/<包名>/logo.*       → 该包封面（在包选择区展示）
#   stickers/<包名>/<其余图片>   → 包内表情包，文件名（去扩展名）即表情包名
# 图片由前端按需下载到本地缓存（IndexedDB）后再发送，发送时以站内相对路径引用。
# ---------------------------------------------------------------------------
_STICKERS_DIR = PROJECT_DIR / "stickers"
_STICKER_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# 默认表情包组目录名：始终排在包列表最前面（前端也据此优先选中）
_DEFAULT_STICKER_PACK = "默认表情包"
# 表情包落盘规格：气泡内展示尺寸较小，256px 足够清晰（高清屏 2x 亦不糊），
# 单张通常压到 10~40KB，既省磁盘也显著加快前端下载缓存速度。
_STICKER_MAX_EDGE = 256
_STICKER_JPEG_QUALITY = 85
# 磁盘压缩缓存目录（首次请求时生成，后续直接命中，避免每次重新解码压缩）
_STICKERS_CACHE_DIR = PROJECT_DIR / "data" / "_sticker_cache"


def _safe_child_dir(base, name: str):
    """把目录名安全拼到 base 下；含路径分隔符或越界时返回 None。"""
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    try:
        target = (base / name).resolve()
        target.relative_to(base.resolve())
    except Exception:
        return None
    return target


def _safe_sticker_name(name: str):
    """校验单个表情包文件名：仅允许纯文件名（不含目录分隔符）与白名单扩展名。"""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in _STICKER_IMAGE_EXTS:
        return None
    return name


def _is_sticker_logo(filename: str) -> bool:
    """文件名（不含扩展名）为 logo 的图片视为该表情包组的封面。"""
    return os.path.splitext(str(filename))[0].lower() == "logo"


# 「随机表情」命名约定：同一前缀 + 连续序号，如 骰子_1.png ~ 骰子_6.png。
# 这类组在前端只显示一个格子，点击时随机抽其中一个发出去（对标 QQ 骰子）。
_RANDOM_SUFFIX_RE = re.compile(r"^(?P<base>.+?)_(?P<idx>\d+)$")


def _parse_random_series(filenames: list) -> dict:
    """从一组文件名里找出「随机表情」系列。

    返回 {系列基名: {"files": [按序号排序的文件名], "count": N}}；
    要求某基名至少有 2 个序号文件才算随机系列（单个的按普通表情处理）。
    """
    series = {}
    for fn in filenames:
        stem = os.path.splitext(fn)[0]
        m = _RANDOM_SUFFIX_RE.match(stem)
        if not m:
            continue
        base = m.group("base").strip()
        if not base:
            continue
        series.setdefault(base, []).append((int(m.group("idx")), fn))
    out = {}
    for base, pairs in series.items():
        if len(pairs) < 2:
            continue   # 只有一个序号文件：当作普通表情
        pairs.sort(key=lambda x: x[0])
        out[base] = {"files": [fn for _, fn in pairs], "count": len(pairs)}
    return out


def _compress_sticker_bytes(data: bytes) -> tuple:
    """把表情包图片压缩到 _STICKER_MAX_EDGE 内，返回 (bytes, ext, err)。

    - 等比缩放到最长边不超过 _STICKER_MAX_EDGE（小图保持原尺寸，不放大）
    - GIF 动图保留原始字节（动图是常见表情包形式，重编码会丢失动画）
    - 带透明的 PNG 保留 alpha（表情包常用透明背景，填白会很难看）
    - Pillow 不可用或解码失败时返回 err，由调用方回退直出原图
    """
    try:
        from PIL import Image
        import io as _io
    except Exception as e:
        return None, "", f"未安装 Pillow（{e}）"
    try:
        im = Image.open(_io.BytesIO(data))
        im.load()
    except Exception as e:
        return None, "", f"无法解析图片（{e}）"
    try:
        animated = bool(getattr(im, "is_animated", False))
        fmt = (im.format or "").upper()
        if animated or fmt == "GIF":
            # 动图：不重编码，保持动画与原始体积（前端下载后浏览器直接播放）
            return data, ".gif", ""

        src_mode = im.mode
        has_alpha = src_mode in ("RGBA", "LA") or (src_mode == "P" and "transparency" in im.info)
        w, h = im.size
        if not w or not h:
            return None, "", "图片尺寸无效"
        longest = max(w, h)
        if longest > _STICKER_MAX_EDGE:
            scale = _STICKER_MAX_EDGE / longest
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

        out = _io.BytesIO()
        if has_alpha:
            # 保留透明通道：输出 PNG（表情包透明底较多，避免填白破坏观感）
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            im.save(out, format="PNG", optimize=True)
            return out.getvalue(), ".png", ""
        # 无透明：转 RGB 用 JPEG 压缩，体积更小
        im = im.convert("RGB")
        im.save(out, format="JPEG", quality=_STICKER_JPEG_QUALITY, optimize=True)
        return out.getvalue(), ".jpg", ""
    except Exception as e:
        return None, "", f"压缩失败（{e}）"


def _sticker_cache_name(pack: str, filename: str, ext: str = None):
    """压缩缓存的统一命名（同一原文件无论被压成 jpg 还是 png 都指向同一 key）。"""
    stem = os.path.splitext(filename)[0]
    safe = "".join(ch for ch in f"{pack}_{stem}" if ch.isalnum() or ch in "._-")
    return _STICKERS_CACHE_DIR / f"{safe}{(ext or '.bin')}"


def _sticker_cache_lookup(pack: str, filename: str):
    """查找已压缩的磁盘缓存（扩展名可能因压缩由 png 变为 jpg），命中返回路径。"""
    stem = os.path.splitext(filename)[0]
    safe = "".join(ch for ch in f"{pack}_{stem}" if ch.isalnum() or ch in "._-")
    for ext in (".jpg", ".png", ".gif", ".webp"):
        p = _STICKERS_CACHE_DIR / f"{safe}{ext}"
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def _resolve_sticker_asset(pack: str, filename: str):
    """定位表情包原图，返回 (path, err)。严格防目录穿越。"""
    pack_dir = _safe_child_dir(_STICKERS_DIR, pack)
    if pack_dir is None:
        return None, "表情包组名非法"
    safe_name = _safe_sticker_name(filename)
    if safe_name is None:
        return None, "表情包文件名非法"
    path = pack_dir / safe_name
    try:
        path.resolve().relative_to(_STICKERS_DIR.resolve())
    except Exception:
        return None, "表情包路径非法"
    if not path.is_file():
        return None, "表情包不存在"
    return path, ""


def _prepare_sticker_bytes(path, pack: str, filename: str):
    """读取表情包图片并按需压缩（优先命中磁盘缓存），返回 (bytes, err)。"""
    cached = _sticker_cache_lookup(pack, filename)
    if cached is not None:
        try:
            return cached.read_bytes(), ""
        except Exception:
            pass
    try:
        raw = path.read_bytes()
    except Exception as e:
        return None, f"读取失败（{e}）"
    out, new_ext, err = _compress_sticker_bytes(raw)
    if out is None:
        # 无法压缩（Pillow 缺失等）：直出原图，保证功能可用
        return raw, ""
    final_cache = _sticker_cache_name(pack, filename, new_ext)
    try:
        _STICKERS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        final_cache.write_bytes(out)
    except Exception:
        pass
    return out, ""


def _list_sticker_packs():
    """扫描 stickers/ 目录，返回表情包组清单。

    每个子目录为一个表情包组：logo.* 为封面，其余图片为其表情包（名字取文件名去扩展名）。
    返回 [{id, name, logo, count}]，按目录名排序；无 logo 的组也会列出（logo 为空）。
    """
    packs = []
    try:
        if not _STICKERS_DIR.is_dir():
            return packs
        for entry in sorted(_STICKERS_DIR.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            logo_name = ""
            stickers = []
            try:
                files = sorted(entry.iterdir(), key=lambda p: p.name)
            except Exception:
                continue
            for f in files:
                if not f.is_file():
                    continue
                if os.path.splitext(f.name)[1].lower() not in _STICKER_IMAGE_EXTS:
                    continue
                if _is_sticker_logo(f.name):
                    logo_name = f.name
                else:
                    stickers.append(f.name)
            # 识别「随机表情」系列（如 骰子_1..6）：前端显示为单格，点击随机抽一个
            rand = _parse_random_series(stickers)
            rand_files = {fn for s in rand.values() for fn in s["files"]}
            packs.append({
                "id": entry.name,
                "name": entry.name,
                "logo": logo_name,
                "count": len(stickers) - len(rand_files) + len(rand),
                # 随机系列元信息：[{base, files, count}]，供前端渲染单格与随机发送
                "random_series": [
                    {"base": base, "files": s["files"], "count": s["count"]}
                    for base, s in sorted(rand.items())
                ],
            })
    except Exception as e:
        logging.warning("[表情包] 扫描目录失败: %s", e)
    # 默认表情包组固定排在最前（前端包选择区最左侧），其余按目录名排序。
    # 否则按 Unicode 序「默认表情包」会被排到「永雏塔菲…」之后，用户不易找到。
    packs.sort(key=lambda p: (p["id"] != _DEFAULT_STICKER_PACK, p["id"]))
    return packs


async def gateway_api_stickers_manifest(request):
    """表情包清单：GET /gateway/stickers/manifest?pack=<包名>

    - 不带 pack：返回全部表情包组（含 logo 文件名与数量）
    - 带 pack：额外返回该组内表情包文件列表（文件名即表情包名）
    """
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)

    packs = await asyncio.to_thread(_list_sticker_packs)
    pack_id = (request.query.get("pack") or "").strip()
    payload = {"ok": True, "packs": packs, "pack": None, "stickers": []}
    if pack_id:
        pack_dir = _safe_child_dir(_STICKERS_DIR, pack_id)
        if pack_dir is None or not pack_dir.is_dir():
            return web.json_response({"ok": False, "message": "表情包组不存在"}, status=404)

        def _collect():
            items = []
            try:
                for f in sorted(pack_dir.iterdir(), key=lambda p: p.name):
                    if not f.is_file():
                        continue
                    if os.path.splitext(f.name)[1].lower() not in _STICKER_IMAGE_EXTS:
                        continue
                    if _is_sticker_logo(f.name):
                        continue
                    items.append({"file": f.name, "name": os.path.splitext(f.name)[0]})
            except Exception:
                pass
            return items

        items = await asyncio.to_thread(_collect)
        payload["pack"] = pack_id
        payload["stickers"] = items
        # 随机系列标记：前端据此把同系列的多个文件折叠成一个"随机"格子。
        # 同时把每张图归入其系列，便于点击时随机抽取。
        series = _parse_random_series([it["file"] for it in items])
        file_to_base = {fn: base for base, s in series.items() for fn in s["files"]}
        for it in items:
            base = file_to_base.get(it["file"])
            if base:
                it["random_base"] = base
                it["series_count"] = series[base]["count"]
        payload["random_series"] = [
            {"base": base, "files": s["files"], "count": s["count"]}
            for base, s in sorted(series.items())
        ]
    return web.json_response(payload)


async def sticker_asset_handler(request):
    """表情包图片服务：GET /stickers/{pack}/{filename}（按需压缩后下发）。"""
    pack = request.match_info.get("pack", "")
    filename = request.match_info.get("filename", "")
    path, err = _resolve_sticker_asset(pack, filename)
    if path is None:
        return web.Response(status=404, text=err or "not found")
    data, cerr = await asyncio.to_thread(_prepare_sticker_bytes, path, pack, filename)
    if data is None:
        return web.Response(status=500, text=cerr or "read error")
    # 压缩可能改变实际格式（如 PNG 无透明则压成 JPEG），需按压缩后字节推断 MIME，
    # 否则响应头与实际内容不符会导致部分浏览器拒绝渲染。
    ctype = _sniff_image_mime(data) or {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    }.get(os.path.splitext(filename)[1].lower(), "application/octet-stream")
    resp = web.Response(body=data, content_type=ctype)
    # 表情包为静态素材：允许浏览器长时间缓存，减少重复拉取
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def _sniff_image_mime(data: bytes) -> str:
    """按魔数推断图片 MIME（压缩后格式可能与原文件名扩展名不一致）。"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:2] == b"BM":
            return "image/bmp"
    except Exception:
        pass
    return ""


async def gateway_api_chat_image_upload(request):
    """上传聊天图片：POST /gateway/chat_image  (multipart file 字段)。

    返回图片 URL 指针（存储于 data/chat_images/<username>_<ts>.<ext>），
    由客户端作为图片消息发送；历史记录存 URL 指针而非 base64。
    """
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效，请重新登录。"}, status=401)

    try:
        post = await request.post()
    except Exception as e:
        return web.json_response({"ok": False, "message": f"解析上传失败: {e}"})
    reader = post.get("file")
    if reader is None:
        return web.json_response({"ok": False, "message": "未找到 file 字段"})
    ok, ext, data, err = _is_image_upload_ok(reader)
    if not ok:
        return web.json_response({"ok": False, "message": err})

    import time as _time
    fname = f"{username}_{int(_time.time() * 1000)}.{ext}"
    _CHAT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    local_path = _CHAT_IMAGES_DIR / fname
    with open(local_path, "wb") as f:
        f.write(data)
    url = f"/chat_images/{fname}"

    # 上传时同步生成图片内容描述（供 LLM 历史/认知分析使用");
    image_desc = await _describe_chat_image(local_path)
    logging.info("[图片] 用户 %s 上传聊天图片 %s（%d 字节, %s, desc=%s）",
                 username, fname, len(data), ext, (image_desc or "")[:40])
    return web.json_response({"ok": True, "url": url, "image_desc": image_desc})


async def gateway_api_avatar_upload(request):
    """上传自己的头像：POST /gateway/avatar  (multipart file 字段)。

    存至 client/images/user_avatars/<username>.png（覆盖），客户端通过
    /images/user_avatars/<username>.png 访问；未上传时前端回退字母。
    """
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效，请重新登录。"}, status=401)

    try:
        post = await request.post()
    except Exception as e:
        return web.json_response({"ok": False, "message": f"解析上传失败: {e}"})
    reader = post.get("file")
    if reader is None:
        return web.json_response({"ok": False, "message": "未找到 file 字段"})
    ok, ext, data, err = _is_image_upload_ok(reader)
    if not ok:
        return web.json_response({"ok": False, "message": err})

    _USER_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    # 服务端统一压缩为小尺寸 JPEG（显著减小体积、加快列表加载）；
    # 压缩失败（如未装 Pillow）时回退保存原始字节，保证上传不中断。
    norm, nerr = await asyncio.to_thread(_normalize_avatar_bytes, data)
    if norm:
        with open(_USER_AVATAR_DIR / f"{username}.png", "wb") as f:
            f.write(norm)
        logging.info("[头像] 用户 %s 上传头像并压缩（%d -> %d 字节, %s）", username, len(data), len(norm), ext)
    else:
        with open(_USER_AVATAR_DIR / f"{username}.png", "wb") as f:
            f.write(data)
        logging.info("[头像] 用户 %s 上传头像（%d 字节, %s，未压缩: %s）", username, len(data), ext, nerr)
    url = f"/images/user_avatars/{username}.png"
    return web.json_response({"ok": True, "url": url})


async def gateway_api_chat(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效，请重新登录。"}, status=401)
    record_heartbeat(username)  # 发消息也是用户活跃信号

    # 认知循环服务端控制：未启动认知循环时，拦截发送并提示
    if not GLOBAL_COGNITION_ENABLED:
        return web.json_response({
            "ok": False,
            "error": "请联系管理员启动认知循环",
            "message": "请联系管理员启动认知循环",
            "reply": "【提示】请联系管理员启动认知循环",
            "think_seconds": 0.0,
        }, status=400)

    sender = data.get("sender", "测试群友")
    text = data.get("text", "")
    image = data.get("image", "")   # 图片 URL 指针（来自 /gateway/chat_image 上传）
    image_desc = data.get("image_desc", "") or ""   # 图片内容描述（上传时生成，可为空）
    mentioned = bool(data.get("mentioned", True))
    quote = data.get("quote")

    # 记忆体归属：家长/老师查看绑定学生时，对话写入该学生记忆体
    scope = _resolve_memory_scope(username, data.get("memory_user_id"))
    if scope is None:
        return web.json_response({"ok": False, "message": "未绑定学生，无法对话。"}, status=403)
    muid, role = scope

    # 确保主进程集中式模型服务已启动（所有子进程共用此单例模型服务）
    RUNTIME.initialize()

    # ========== 图片消息 ==========
    # 1) 写入图片历史（带内容描述，供前端/LLM 历史展示）
    # 2) 用文本化的图片描述复用 worker.chat 走完整认知分析（拆记忆/关键词/状态），
    #    分析文本不重复写入文本历史（skip_history=True），避免与图片历史重复。
    # 图片内容描述进 LLM/记忆时用"发送了一张图片，内容是xx"，避免图片数据污染提示词。
    if image:
        try:
            from utils.message_history import add_image_message_for
            # 若前端未带描述，尝试根据 URL 反解本地文件后台生成（兜底）
            if not image_desc:
                try:
                    fname = image.rsplit("/", 1)[-1]
                    local = _CHAT_IMAGES_DIR / fname
                    if local.exists():
                        image_desc = await _describe_chat_image(local)
                except Exception:
                    image_desc = ""
            extra = {"quote": quote} if quote else None
            add_image_message_for(str(PROJECT_DIR), username, sender, image, "客户端", image_desc=image_desc, extra=extra)
            logging.info("[图片消息] 用户 %s 发送图片 %s desc=%s", username, image, (image_desc or "")[:40])

            # 走分析：组装文本化消息（图片不进文本历史，仅用于理解层）
            analysis_text = f"{sender}发送了一张图片"
            if image_desc:
                analysis_text += f"，内容是“{image_desc}”"

            def _process_img():
                worker = USER_WORKERS.get_worker(username, memory_user_id=muid, role=role)
                return worker.chat(sender, analysis_text, mentioned, skip_history=True, quote=quote)

            await asyncio.to_thread(_process_img)
            return web.json_response({"ok": True, "image": image, "image_desc": image_desc, "message": "图片已发送"})
        except Exception as e:
            logging.exception("图片消息处理失败")
            return web.json_response({"ok": False, "message": f"图片消息处理失败: {e}"}, status=500)

    if not text.strip():
        return web.json_response({"ok": False, "message": "消息为空。"})

    def _process():
        worker = USER_WORKERS.get_worker(username, memory_user_id=muid, role=role)
        # 认知循环启动统一由 record_heartbeat（前端心跳）驱动，这里不直接启动
        return worker.chat(sender, text, mentioned, quote=quote)

    try:
        res = await asyncio.to_thread(_process)
        return web.json_response(res)
    except Exception as e:
        logging.exception("用户对话处理异常: %s", e)
        return web.json_response({"ok": False, "message": f"处理失败: {e}", "reply": f"【错误】处理失败: {e}"}, status=500)


# ---------------------------------------------------------------------------
# 用户即时通讯与社交接口（搜索用户 / 好友系统 / 群聊系统 / 消息收发与撤回）
# ---------------------------------------------------------------------------
async def gateway_api_search_users(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    q = (request.query.get("q") or "").strip()
    users = _load_users()
    res = social_manager.search_users(q, username, users)
    return web.json_response({"ok": True, "users": res})


async def gateway_api_friend_apply(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    target = (data.get("target_username") or "").strip()
    message = (data.get("message") or "").strip()
    if not target:
        return web.json_response({"ok": False, "message": "目标用户名不能为空"}, status=400)
    users = _load_users()
    if target not in users:
        return web.json_response({"ok": False, "message": "目标用户不存在"}, status=404)

    r = social_manager.apply_friend(username, target, message)
    if r.get("ok"):
        if r.get("instant_friend"):
            await _send_to_users_ws([username, target], {
                "type": "social_event", "event": "friend_accepted", "by_user": username, "from_user": target
            })
        else:
            await _send_to_user_ws(target, {
                "type": "social_event", "event": "friend_request", "from_user": username, "message": message
            })
    return web.json_response(r)


async def gateway_api_friend_requests(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    reqs = social_manager.get_friend_requests(username)
    return web.json_response({"ok": True, **reqs})


async def gateway_api_friend_handle_request(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    req_id = (data.get("request_id") or "").strip()
    action = (data.get("action") or "").strip()
    r = social_manager.handle_friend_request(req_id, username, action)
    if r.get("ok") and action == "accept":
        from_user = r.get("from_user")
        await _send_to_users_ws([username, from_user], {
            "type": "social_event", "event": "friend_accepted", "by_user": username, "from_user": from_user
        })
    return web.json_response(r)


async def gateway_api_friends_list(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    friends = social_manager.get_friends_list(username, _load_users(), _user_live_status)
    return web.json_response({"ok": True, "friends": friends})


async def gateway_api_friend_delete(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    target = (data.get("target_username") or "").strip()
    r = social_manager.delete_friend(username, target)
    if r.get("ok"):
        await _send_to_users_ws([username, target], {
            "type": "social_event", "event": "friend_deleted", "by_user": username, "target": target
        })
    return web.json_response(r)


async def gateway_api_friend_settings(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    target = (data.get("target_username") or "").strip()
    pinned = data.get("pinned")
    muted = data.get("muted")
    alias = data.get("alias")
    r = social_manager.set_friend_settings(username, target, pinned=pinned, muted=muted, alias=alias)
    return web.json_response(r)


async def gateway_api_group_create(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    name = (data.get("name") or "").strip()
    members = data.get("members") or []
    # 校验被邀请者必须为已注册用户，过滤非法/不存在用户
    users = _load_users()
    valid_members = [m for m in members if isinstance(m, str) and m.strip() in users and m.strip() != username]
    r = await asyncio.to_thread(social_manager.create_group, name, username, valid_members)
    if r.get("ok"):
        g_obj = r.get("group", {})
        await _send_to_users_ws(g_obj.get("members", []), {
            "type": "social_event", "event": "group_created", "group": g_obj
        })
    return web.json_response(r)


async def gateway_api_groups_list(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    groups = social_manager.get_groups_for_user(username)
    return web.json_response({"ok": True, "groups": groups})


async def gateway_api_group_detail(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (request.query.get("group_id") or "").strip()
    r = social_manager.get_group_detail(group_id, username, _load_users(), _user_live_status)
    return web.json_response(r)


async def gateway_api_group_settings(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()
    name = data.get("name")
    pinned = data.get("pinned")
    muted = data.get("muted")
    r = social_manager.set_group_settings(group_id, username, name=name, pinned=pinned, muted=muted)
    if r.get("ok") and name is not None:
        g_detail = social_manager.get_group_detail(group_id, username, _load_users(), _user_live_status)
        if g_detail.get("ok"):
            members = [m["username"] for m in g_detail["group"]["members"]]
            await _send_to_users_ws(members, {
                "type": "social_event", "event": "group_updated", "group_id": group_id, "name": name
            })
    return web.json_response(r)


async def gateway_api_group_invite(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()
    members = data.get("members") or []
    # 校验被邀请者必须为已注册用户，过滤非法/不存在用户名
    users = _load_users()
    valid_members = [m for m in members if isinstance(m, str) and m.strip() in users]
    r = await asyncio.to_thread(social_manager.invite_group_members, group_id, username, valid_members)
    if r.get("ok"):
        g_detail = await asyncio.to_thread(social_manager.get_group_detail, group_id, username, _load_users(), _user_live_status)
        if g_detail.get("ok"):
            all_m = [m["username"] for m in g_detail["group"]["members"]]
            await _send_to_users_ws(all_m, {
                "type": "social_event", "event": "group_updated", "group_id": group_id
            })
    return web.json_response(r)


async def gateway_api_group_leave(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()
    g_detail = social_manager.get_group_detail(group_id, username, _load_users(), _user_live_status)
    members = [m["username"] for m in g_detail["group"]["members"]] if g_detail.get("ok") else []
    r = social_manager.leave_group(group_id, username)
    if r.get("ok"):
        await _send_to_users_ws(members, {
            "type": "social_event", "event": "group_updated", "group_id": group_id
        })
    return web.json_response(r)


# ------------------------------------------------------------------------
# 群聊抽奖：抽取模式 / 骰子模式
# ------------------------------------------------------------------------
async def _broadcast_lottery(group_id: str, action: str, lottery):
    """把抽奖最新状态推给群内所有成员（同一份公共视图，前端按自己身份渲染）。"""
    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members:
        return
    await _send_to_users_ws(members, {
        "type": "lottery_event",
        "action": action,
        "group_id": group_id,
        "lottery": lottery,
    })


async def _send_lottery_system_message(group_id: str, content: str):
    """在群聊里留一条系统消息，让没打开抽奖面板的群友也能感知到抽奖动态。

    系统消息只是辅助提示：写入/推送失败不得影响抽奖主流程的返回结果。
    """
    try:
        msg = await asyncio.to_thread(
            social_manager.save_message, "group", "系统", group_id, content, msg_type="system"
        )
        members = await asyncio.to_thread(social_manager.get_group_members, group_id)
        if members:
            await _send_to_users_ws(members, {
                "type": "chat_message",
                "chat_type": "group",
                "group_id": group_id,
                "message": msg,
            })
        return msg
    except Exception:
        logging.exception("[抽奖] 写入抽奖系统消息失败")
        return None


async def gateway_api_lottery_current(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (request.query.get("group_id") or "").strip()
    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members or username not in members:
        return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)
    lottery = await asyncio.to_thread(lottery_manager.get_lottery, group_id, members)
    return web.json_response({"ok": True, "lottery": lottery})


async def gateway_api_lottery_create(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "请求格式错误"}, status=400)
    username = resolve_session(get_token_from_request(request, data))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()
    mode = (data.get("mode") or "").strip()
    invited = data.get("members") or []
    if not isinstance(invited, list):
        invited = []

    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members or username not in members:
        return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)

    r = await asyncio.to_thread(
        lottery_manager.create_lottery, group_id, username, mode, invited, members
    )
    if r.get("ok"):
        lottery = r["lottery"]
        await _broadcast_lottery(group_id, "created", lottery)
        mode_name = "抽取模式" if mode == lottery_manager.MODE_DRAW else "骰子模式"
        await _send_lottery_system_message(
            group_id,
            f"🎁 {username} 发起了群抽奖（{mode_name}），已加入 {lottery['participant_count']} 人，点击工具栏 🎁 参与",
        )
    return web.json_response(r)


async def gateway_api_lottery_invite(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "请求格式错误"}, status=400)
    username = resolve_session(get_token_from_request(request, data))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()
    invited = data.get("members") or []
    if not isinstance(invited, list):
        invited = []

    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members or username not in members:
        return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)

    r = await asyncio.to_thread(
        lottery_manager.invite_members, group_id, username, invited, members
    )
    if r.get("ok") and r.get("added"):
        await _broadcast_lottery(group_id, "invited", r.get("lottery"))
        await _send_lottery_system_message(
            group_id, f"🎁 {username} 邀请 {', '.join(r['added'])} 加入了抽奖"
        )
    return web.json_response(r)


async def gateway_api_lottery_roll(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "请求格式错误"}, status=400)
    username = resolve_session(get_token_from_request(request, data))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()

    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members or username not in members:
        return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)

    r = await asyncio.to_thread(lottery_manager.roll_dice, group_id, username, members)
    if r.get("ok"):
        await _broadcast_lottery(group_id, "rolled", r.get("lottery"))
    return web.json_response(r)


async def gateway_api_lottery_draw(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "请求格式错误"}, status=400)
    username = resolve_session(get_token_from_request(request, data))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()

    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members or username not in members:
        return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)

    r = await asyncio.to_thread(lottery_manager.draw_winner, group_id, username, members)
    if r.get("ok"):
        await _broadcast_lottery(group_id, "drawn", r.get("lottery"))
        await _send_lottery_system_message(group_id, f"🎉 抽奖结果：{r['winner']} 中奖！")
    return web.json_response(r)


async def gateway_api_lottery_finish(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "请求格式错误"}, status=400)
    username = resolve_session(get_token_from_request(request, data))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()

    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members or username not in members:
        return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)

    r = await asyncio.to_thread(lottery_manager.finish_dice, group_id, username, members)
    if r.get("ok"):
        lottery = r.get("lottery") or {}
        await _broadcast_lottery(group_id, "finished", lottery)
        top = lottery.get("top") or []
        if top:
            title = "最高分（并列）" if len(top) > 1 else "最高分"
            await _send_lottery_system_message(
                group_id,
                f"🎲 骰子抽奖结束，{title}：{'、'.join(top)}（{lottery.get('winner_value')} 点）",
            )
        else:
            await _send_lottery_system_message(group_id, "🎲 骰子抽奖结束，本局无人投掷。")
    return web.json_response(r)


async def gateway_api_lottery_cancel(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "请求格式错误"}, status=400)
    username = resolve_session(get_token_from_request(request, data))
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    group_id = (data.get("group_id") or "").strip()

    members = await asyncio.to_thread(social_manager.get_group_members, group_id)
    if not members or username not in members:
        return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)

    r = await asyncio.to_thread(lottery_manager.cancel_lottery, group_id, username, members)
    if r.get("ok"):
        await _broadcast_lottery(group_id, "cancelled", None)
        await _send_lottery_system_message(group_id, f"🛑 {username} 取消了本局抽奖")
    return web.json_response(r)


async def gateway_api_bot_settings(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    pinned = data.get("pinned")
    muted = data.get("muted")
    r = social_manager.set_bot_settings(username, pinned=pinned, muted=muted)
    return web.json_response(r)


async def gateway_api_conversations(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)

    # 尝试获取辉夜的最新一条消息与未读数（磁盘读取放到线程池，避免阻塞事件循环）
    # 注意：辉夜历史存的是虚拟时钟时间，已读位置/前端一律用真实时间；
    # 这里统一换算成真实时间后再比较与下发，避免时间域不一致（列表显示 1970 年 / 未读算错）。
    from core.virtual_clock import clock as _clock
    bot_last_msg = None
    bot_unread = 0
    try:
        from utils.message_history import read_history_page_for
        read_positions = await asyncio.to_thread(social_manager.get_read_positions, username)
        bot_last_read = read_positions.get("bot") or 0
        # 兼容旧数据：历史版本误把虚拟时间当作已读位置写入（约 2.3e6，远小于真实 Unix 时间），
        # 这里换算成真实时间，避免升级后把全部辉夜消息误判为未读。
        if 0 < bot_last_read < 1_000_000_000:
            bot_last_read = _clock.to_real_time(bot_last_read)
        def _read_bot_last():
            # 读最近一页（30 条）即可覆盖常见未读量，避免全量扫描
            page, _ = read_history_page_for(str(PROJECT_DIR), username, limit=30)
            return page
        page = await asyncio.to_thread(_read_bot_last)
        if page:
            m = page[-1]
            last_real = _clock.to_real_time(m.get("time")) if m.get("time") is not None else time.time()
            bot_last_msg = {
                "content": m.get("content") or m.get("text", ""),
                "time": last_real,
                "sender": m.get("sender", "辉夜"),
            }
            bot_unread = sum(
                1 for x in page
                if x.get("sender") != username
                and x.get("time") is not None
                and _clock.to_real_time(x.get("time")) > bot_last_read
            )
    except Exception:
        pass

    convs = await asyncio.to_thread(
        social_manager.get_conversations, username, _load_users(), _user_live_status, bot_last_msg, bot_unread
    )
    # 附带各会话已读位置，供前端渲染"以下为新消息"分隔（未读数已并入各会话 unread 字段）
    read_positions = await asyncio.to_thread(social_manager.get_read_positions, username)
    # 兼容旧数据：辉夜会话的已读位置若仍是虚拟时间，换算成真实时间再下发
    br = read_positions.get("bot")
    if br and 0 < br < 1_000_000_000:
        read_positions["bot"] = _clock.to_real_time(br)
    return web.json_response({"ok": True, "conversations": convs, "read_positions": read_positions})


async def gateway_api_mark_read(request):
    """标记某会话已读到指定时间：POST /gateway/messages/read  body: {chat_type, target, ts}"""
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)
    chat_type = (data.get("chat_type") or "").strip()
    target = (data.get("target") or "").strip()
    ts = data.get("ts")
    key = "bot" if chat_type == "bot" else target
    if not key:
        return web.json_response({"ok": False, "message": "缺少会话目标"}, status=400)
    if ts is None:
        ts = time.time()
    r = await asyncio.to_thread(social_manager.set_read_position, username, key, ts)
    return web.json_response(r)


async def gateway_api_messages_get(request):
    token = get_token_from_request(request)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)

    chat_type = (request.query.get("chat_type") or "bot").strip()
    target = (request.query.get("target") or "").strip()
    before_raw = request.query.get("before")
    before = None
    if before_raw:
        try:
            before = float(before_raw)
        except (TypeError, ValueError):
            before = None
    limit = 30
    try:
        limit = int(request.query.get("limit") or 30)
    except Exception:
        limit = 30

    if chat_type == "bot":
        # 辉夜历史存虚拟时钟时间，前端用真实时间：入参 before 还原成虚拟、出参 time 换算成真实。
        # DM/群聊消息存的是真实 Unix 时间，不走此换算（故 before 在此分支内单独处理）。
        from core.virtual_clock import clock as _clock
        from utils.message_history import read_history_page_for
        before_virtual = _clock.to_virtual_time(before) if before is not None else None
        def _read_bot_page():
            return read_history_page_for(str(PROJECT_DIR), username, before=before_virtual, limit=limit)
        page, has_more = await asyncio.to_thread(_read_bot_page)
        res = []
        for i, m in enumerate(page):
            text = m.get("content") if "content" in m else m.get("text", "")
            t = m.get("time")
            sender = m.get("sender", "未知")
            res.append({
                # 与 /gateway/history 共用同一套稳定 id（绝不含页内下标）
                "id": m.get("id") or _history_msg_id(username, sender, t, text),
                "sender": sender,
                "content": text,
                "source": m.get("source", "客户端"),
                "time": (_clock.to_real_time(t) if t is not None else None),
                "mtype": m.get("mtype", "text"),
                "image": m.get("image", ""),
                "quote": m.get("quote"),
                "recalled": False,
            })
        return web.json_response({"ok": True, "messages": res, "has_more": has_more})

    page, has_more = await asyncio.to_thread(
        social_manager.get_messages, chat_type, username, target, before, limit
    )
    return web.json_response({"ok": True, "messages": page, "has_more": has_more})


async def gateway_api_messages_send(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)

    chat_type = (data.get("chat_type") or "user").strip()
    target = (data.get("target") or "").strip()
    text = (data.get("text") or "").strip()
    image = (data.get("image") or "").strip()
    if not text and not image:
        return web.json_response({"ok": False, "message": "消息内容不能为空"}, status=400)
    if len(text) > 4000:
        return web.json_response({"ok": False, "message": "消息过长（上限 4000 字）"}, status=400)
    if not target:
        return web.json_response({"ok": False, "message": "缺少会话目标"}, status=400)

    # 图片/表情包一律只接受站内相对路径（/chat_images/...、/stickers/...）：
    # 杜绝外部 http(s)/data:/javascript: 等 URL 被写入历史后由渲染端加载，造成注入与跟踪风险。
    image_norm = ""
    if image:
        if not image.startswith("/") or image.startswith("//"):
            return web.json_response({"ok": False, "message": "非法的图片地址"}, status=400)
        # 归一化后再校验，防止 /a/../../evil 之类的穿越写法绕过前缀判断
        image_norm = posixpath.normpath(image)
        if not (image_norm.startswith("/chat_images/") or image_norm.startswith("/stickers/")):
            return web.json_response({"ok": False, "message": "非法的图片地址"}, status=400)

    # 鉴权：群聊必须为成员；私聊目标必须为已注册用户
    if chat_type == "group":
        if not await asyncio.to_thread(social_manager._is_group_member, target, username):
            return web.json_response({"ok": False, "message": "您不是该群成员"}, status=403)
    elif chat_type == "user":
        if target not in _load_users():
            return web.json_response({"ok": False, "message": "对方用户不存在"}, status=404)
    else:
        return web.json_response({"ok": False, "message": "不支持的会话类型"}, status=400)

    # mtype 由服务端判定：sticker 需显式声明且地址必须指向表情包目录，其余按有无图片归类
    mtype = "image" if image else "text"
    sticker_name = ""
    if (data.get("mtype") or "").strip() == "sticker":
        if not image_norm.startswith("/stickers/"):
            return web.json_response({"ok": False, "message": "非法的表情包地址"}, status=400)
        mtype = "sticker"
        # 表情包名优先取客户端声明的名字，其次从 URL 反解：
        # URL 文件名是 percent-encoded 的（中文名会被编码），必须先解码再取文件名，
        # 否则会把 %E6%B5%8B... 当成表情包名写进历史与会话预览。
        declared = (data.get("sticker_name") or "").strip()
        if declared:
            sticker_name = declared
        else:
            try:
                from urllib.parse import unquote
                sticker_name = os.path.splitext(posixpath.basename(unquote(image_norm)))[0]
            except Exception:
                sticker_name = os.path.splitext(posixpath.basename(image_norm))[0]
    # 表情包/图片若客户端未给正文，用可读占位补齐：
    # 会话列表预览与引用卡片都依赖 content，留空会显示成空白条目。
    if not text:
        if mtype == "sticker":
            text = f"[表情包] {sticker_name}" if sticker_name else "[表情包]"
        elif mtype == "image":
            text = "[图片]"
    quote = data.get("quote")
    msg_obj = await asyncio.to_thread(
        social_manager.save_message, chat_type, username, target, text, image, mtype,
        quote=quote, sticker_name=sticker_name
    )

    if chat_type == "user":
        await _send_to_users_ws([username, target], {
            "type": "chat_message",
            "chat_type": "user",
            "from": username,
            "target": target,
            "message": msg_obj,
        })
    elif chat_type == "group":
        g_detail = social_manager.get_group_detail(target, username, _load_users(), _user_live_status)
        if g_detail.get("ok"):
            members = [m["username"] for m in g_detail["group"]["members"]]
            await _send_to_users_ws(members, {
                "type": "chat_message",
                "chat_type": "group",
                "group_id": target,
                "message": msg_obj,
            })

    return web.json_response({"ok": True, "message": msg_obj})


async def gateway_api_messages_recall(request):
    data = await request.json()
    token = get_token_from_request(request, data)
    username = resolve_session(token)
    if not username:
        return web.json_response({"ok": False, "message": "登录已失效。"}, status=401)

    chat_type = (data.get("chat_type") or "user").strip()
    target = (data.get("target") or "").strip()
    msg_id = (data.get("msg_id") or "").strip()
    r = social_manager.recall_message(chat_type, username, target, msg_id)
    if r.get("ok"):
        if chat_type == "user":
            await _send_to_users_ws([username, target], {
                "type": "chat_recall",
                "chat_type": "user",
                "target": target,
                "msg_id": msg_id,
                "recalled_by": username,
            })
        elif chat_type == "group":
            g_detail = social_manager.get_group_detail(target, username, _load_users(), _user_live_status)
            if g_detail.get("ok"):
                members = [m["username"] for m in g_detail["group"]["members"]]
                await _send_to_users_ws(members, {
                    "type": "chat_recall",
                    "chat_type": "group",
                    "group_id": target,
                    "msg_id": msg_id,
                    "recalled_by": username,
                })
    return web.json_response(r)


def _is_user_online(username: str, now: float = None) -> bool:
    """判定指定用户当前是否在线（心跳优先原则）：
    1. 存在该用户的活跃 WebSocket 客户端连接；
    2. 或最近一次心跳在 _ONLINE_HEARTBEAT_WINDOW 秒内。
    """
    if not username:
        return False
    if now is None:
        now = time.time()
    try:
        with _ws_clients_lock:
            if bool(_ws_user_clients.get(username)):
                return True
    except Exception:
        pass
    hb = _USER_HEARTBEATS.get(username)
    if hb and (now - hb) <= _ONLINE_HEARTBEAT_WINDOW:
        return True
    return False


def record_heartbeat(username: str = None):
    """记录指定用户最近一次心跳时间戳，并驱动用户进程的生命周期与认知循环。

    安全优先与按需拉起原则：
      1. 若该用户正处于下线 1 分钟退出倒计时计划中，立即取消退出计划，重新计算活跃；
      2. 若该用户因下线/空闲已被暂停，收到心跳时自动恢复其专属认知循环；
      3. 若该用户专属进程此前已退出释放（下线满1分钟后重新上线）或尚未创建：
         若全局认知开启，自动为该用户重新拉起专属进程（加载记忆）并恢复认知循环。

    按用户独立计算：username 为 None 时仅刷新全局活跃标记（不启动/恢复任何用户）。
    """
    now = time.time()
    if not username:
        return
    _USER_HEARTBEATS[username] = now

    # 场景 1：用户处于 1 分钟下线退出倒计时中，收到心跳 → 取消退出计划，重新计算活跃
    if username in _PENDING_EXIT_USERS:
        _PENDING_EXIT_USERS.pop(username, None)
        logging.info("[心跳] 用户 %s 重新上线（1分钟内），已取消退出计划并重新计算活跃状态", username)
        _broadcast({"type": "status", "text": f"检测到用户 {username} 重新上线，已取消退出计划"})

    # 场景 2：该用户此前因下线/空闲被自动暂停，收到心跳 → 恢复其专属认知循环
    if _USER_PAUSED.get(username) and GLOBAL_COGNITION_ENABLED:
        _USER_PAUSED[username] = False
        if USER_WORKERS.has_worker(username):
            try:
                USER_WORKERS.start_cognition_for(username)
                _broadcast({"type": "status", "text": f"检测到用户 {username} 活跃连接，已恢复其认知循环"})
                logging.info("[心跳] 用户 %s 恢复活跃，已自动恢复其认知循环", username)
            except Exception as e:
                logging.exception("自动恢复用户 %s 认知循环失败: %s", username, e)

    # 场景 3：管理员期望认知循环开启，且该用户在"待心跳启动"映射 → 首次心跳到达
    if _COGNITION_DESIRED and username in _PENDING_COGNITION_USERS:
        muid, role = _PENDING_COGNITION_USERS.pop(username)
        try:
            worker = USER_WORKERS.get_worker(username, memory_user_id=muid, role=role)
            worker.start_cognition()
            _USER_PAUSED[username] = False
            _broadcast({"type": "status", "text": f"用户 {username} 心跳到达，认知循环已启动"})
            logging.info("[心跳] 用户 %s 心跳到达，已创建专属进程并启动认知循环 (memory=%s)", username, muid)
        except Exception as e:
            logging.exception("启动用户 %s 认知循环失败: %s", username, e)

    # 场景 4：一分钟后（进程已退出释放内存）用户重新上线，按需重新拉起进程并恢复循环
    elif GLOBAL_COGNITION_ENABLED and not USER_WORKERS.has_worker(username):
        scope = _resolve_memory_scope(username)
        if scope:
            muid, role = scope
        else:
            prof = _user_profile(username) or {}
            muid = prof.get("user_id") or username
            role = prof.get("role", ROLE_STUDENT)
        if muid:
            try:
                worker = USER_WORKERS.get_worker(username, memory_user_id=muid, role=role)
                worker.start_cognition()
                _USER_PAUSED[username] = False
                _broadcast({"type": "status", "text": f"用户 {username} 重新上线，已重新拉起专属进程并恢复认知循环"})
                logging.info("[心跳] 用户 %s 重新上线（原进程已退出），已重新拉起专属进程 (memory=%s, role=%s) 并恢复认知循环",
                             username, muid, role)
            except Exception as e:
                logging.exception("重新拉起用户 %s 专属进程失败: %s", username, e)


async def gateway_api_heartbeat(request):
    """客户端心跳上报接口：POST /gateway/heartbeat（按登录用户独立记录心跳）"""
    token = get_token_from_request(request)
    username = resolve_session(token)
    record_heartbeat(username)
    return web.json_response({
        "ok": True,
        "paused": _USER_PAUSED.get(username, False) if username else False,
        "cognition_running": GLOBAL_COGNITION_ENABLED and not _USER_PAUSED.get(username, False),
    })


async def _heartbeat_watchdog():
    """心跳看门狗后台协程：对每个用户独立计算心跳与生命周期。

    1. 用户下线判定：无活跃 WebSocket 连接且超过 _ONLINE_HEARTBEAT_WINDOW (45秒) 无心跳；
    2. 首次判定下线时，若认知循环在运行则自动暂停，并进入 1 分钟（_OFFLINE_EXIT_DELAY_SECONDS）退出倒计时计划；
    3. 若 1 分钟内用户重新上线（心跳/WebSocket/发消息），取消退出计划，恢复计时与认知循环；
    4. 若 1 分钟期满用户仍未上线，保存该用户沙箱数据（SQLite/FAISS/记忆/状态）并安全退出其专属子进程，释放系统内存；
    5. 一分钟后若用户重新上线，则自动重新拉起其专属子进程并恢复状态。
    """
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            active_usernames = USER_WORKERS.get_active_usernames()

            for username in active_usernames:
                last_ts = _USER_HEARTBEATS.get(username, now)
                idle_sec = now - last_ts
                is_online = _is_user_online(username, now)

                if is_online:
                    # 在线：如果在退出计划中，取消退出计划
                    if username in _PENDING_EXIT_USERS:
                        _PENDING_EXIT_USERS.pop(username, None)
                        logging.info("[心跳看门狗] 检测到用户 %s 处于在线状态，已清除退出计划", username)
                    # 若此前被自动暂停，且全局认知开启，恢复认知循环
                    if _USER_PAUSED.get(username) and GLOBAL_COGNITION_ENABLED:
                        _USER_PAUSED[username] = False
                        try:
                            USER_WORKERS.start_cognition_for(username)
                            _broadcast({"type": "status", "text": f"检测到用户 {username} 在线，已恢复其认知循环"})
                            logging.info("[心跳看门狗] 用户 %s 恢复活跃，已恢复其专属认知循环", username)
                        except Exception as e:
                            logging.exception("恢复用户 %s 认知循环失败: %s", username, e)
                else:
                    # 离线判定成立（无活跃 WebSocket 且心跳超时）
                    # 步骤一：确保暂停认知循环，节省 CPU/GPU
                    if not _USER_PAUSED.get(username):
                        _USER_PAUSED[username] = True
                        if GLOBAL_COGNITION_ENABLED:
                            try:
                                USER_WORKERS.stop_cognition_for(username)
                                _broadcast({"type": "status", "text": f"用户 {username} 已离线，其认知循环已自动暂停"})
                                logging.info("[心跳看门狗] 用户 %s 超过心跳窗口无心跳（空闲=%.1fs），已暂停其专属认知循环", username, idle_sec)
                            except Exception as e:
                                logging.exception("暂停用户 %s 认知循环失败: %s", username, e)

                    # 步骤二：管理 1 分钟延迟退出进程计划
                    if username not in _PENDING_EXIT_USERS:
                        _PENDING_EXIT_USERS[username] = now + _OFFLINE_EXIT_DELAY_SECONDS
                        logging.info("[心跳看门狗] 用户 %s 已判定下线（空闲=%.1fs），已设定 %.0f 秒后安全退出子进程并释放内存",
                                     username, idle_sec, _OFFLINE_EXIT_DELAY_SECONDS)
                        _broadcast({"type": "status", "text": f"用户 {username} 已下线，将在 1 分钟后自动保存数据并释放进程内存"})
                    else:
                        deadline = _PENDING_EXIT_USERS[username]
                        if now >= deadline:
                            # 等待满 1 分钟：安全落盘并退出该用户子进程，释放系统内存
                            _PENDING_EXIT_USERS.pop(username, None)
                            _USER_PAUSED.pop(username, None)
                            with _WORKER_STATS_LOCK:
                                _WORKER_STATS.pop(username, None)
                            logging.info("[心跳看门狗] 用户 %s 离线已满 1 分钟，正在保存沙箱数据并退出子进程释放内存...", username)

                            def _do_shutdown(uname=username):
                                try:
                                    if _is_user_online(uname):
                                        logging.info("[心跳看门狗] 用户 %s 在执行退出前已重新上线，放弃退出", uname)
                                        return
                                    ok = USER_WORKERS.shutdown_worker(uname)
                                    if ok:
                                        logging.info("[心跳看门狗] 用户 %s 专属子进程已安全退出，内存已释放", uname)
                                        _broadcast({"type": "status", "text": f"用户 {uname} 离线满1分钟，专属进程已退出，内存已释放"})
                                except Exception as ex:
                                    logging.exception("[心跳看门狗] 退出用户 %s 子进程失败: %s", uname, ex)

                            asyncio.create_task(asyncio.to_thread(_do_shutdown))

            # 清理已经不存在 worker 的用户的残留倒计时
            for u in list(_PENDING_EXIT_USERS.keys()):
                if not USER_WORKERS.has_worker(u):
                    _PENDING_EXIT_USERS.pop(u, None)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.exception("[心跳看门狗] 循环异常: %s", e)
            await asyncio.sleep(5)


async def _ws_safe_send(ws, text: str):
    """向 WebSocket 客户端安全发送消息；客户端已断开（连接重置）时静默忽略，避免日志刷屏。"""
    if ws is None or ws.closed:
        return
    try:
        await ws.send_str(text)
    except (ConnectionResetError, RuntimeError):
        pass  # 客户端已关闭连接（WinError 10054 / Cannot write to closing transport），忽略
    except Exception:
        pass


async def admin_ws_handler(request):
    """控制面板专属 WebSocket：推送系统日志、运行状态、监控数据。受管理员鉴权保护。"""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    with _ws_clients_lock:
        _admin_ws_clients.add(ws)
    signal_handler.add_client(ws)
    # 连接建立后补发最近日志（避免白屏）
    for cat, lines in _log_buffer.items():
        for entry in lines[-200:]:
            if isinstance(entry, dict):
                payload = {"type": "log", "cat": entry.get("cat", cat), "line": entry.get("line", ""),
                           "level": entry.get("level", "INFO"), "user": entry.get("user", "")}
            else:
                payload = {"type": "log", "cat": cat, "line": entry, "level": "INFO", "user": ""}
            await _ws_safe_send(ws, json.dumps(payload, ensure_ascii=False))
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if isinstance(data, dict) and data.get("type") == "ping":
                        await _ws_safe_send(ws, json.dumps({"type": "pong"}, ensure_ascii=False))
                except Exception:
                    pass
    except (ConnectionResetError, RuntimeError):
        pass  # 客户端主动断开，忽略
    finally:
        signal_handler.remove_client(ws)
        with _ws_clients_lock:
            _admin_ws_clients.discard(ws)
        return ws


async def client_ws_handler(request):
    """客户端专属 WebSocket（8987 端口）：推送聊天消息、撤回、实时广播、在线心跳。

    安全隔离：严格防止向客户端推送服务端系统日志 (_log_buffer) 或内部监控数据。
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    with _ws_clients_lock:
        _client_ws_clients.add(ws)
    ws_username = resolve_session(request.query.get("token", "")) or None
    if ws_username:
        with _ws_clients_lock:
            _ws_user_clients[ws_username].add(ws)
        record_heartbeat(ws_username)
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                if ws_username:
                    record_heartbeat(ws_username)
                try:
                    data = json.loads(msg.data)
                    if isinstance(data, dict) and data.get("type") == "ping":
                        await _ws_safe_send(ws, json.dumps({"type": "pong"}, ensure_ascii=False))
                except Exception:
                    pass
    except (ConnectionResetError, RuntimeError):
        pass  # 客户端断开
    finally:
        with _ws_clients_lock:
            _client_ws_clients.discard(ws)
            if ws_username:
                _ws_user_clients[ws_username].discard(ws)
                if not _ws_user_clients[ws_username]:
                    _ws_user_clients.pop(ws_username, None)
        return ws


ws_handler = admin_ws_handler


def _aggregate_worker_stats() -> dict:
    """汇总所有**存活** worker 的热数据规模（热记忆节点/热链接/词网）。

    多用户架构下记忆体在各用户子进程内，主进程不持有用户记忆（main 进程的
    provide_for_monitor 恒为初值，故旧实现数值无意义）。各子进程空闲时每约 2 秒
    主动上报一次热数据（见 core/user_worker.py 的 _worker_main），这里只读缓存
    并**仅对仍存活的 worker**求和：不发起任何 IPC、绝不 get_worker（不拉起子进程）。
    返回 {"mem","links","words","online"}。
    """
    total = {"mem": 0, "links": 0, "words": 0, "online": 0}
    try:
        workers = USER_WORKERS.get_all_workers()
    except Exception:
        return total
    with _WORKER_STATS_LOCK:
        snapshot = dict(_WORKER_STATS)
    for username, st in snapshot.items():
        try:
            worker = workers.get(username)
            if not worker or not worker.is_alive():
                continue   # worker 已不在或已退出：不计入（也不清理，交由下次上报覆盖）
            total["mem"] += int(st.get("mem", 0) or 0)
            total["links"] += int(st.get("links", 0) or 0)
            total["words"] += int(st.get("words", 0) or 0)
            total["online"] += 1
        except Exception:
            continue
    return total


async def api_status(request):
    """总览状态：热记忆/热链接/词网（所有在线用户之和）/运行时间/虚拟时间/认知循环/模型状态。"""
    try:
        from core.virtual_clock import clock
        import datetime as dtmod
        from core.model_backend import BACKENDS

        # 热数据：汇总所有在线 worker 之和（不拉起 worker，磁盘/内存读取放线程池）
        agg = await asyncio.to_thread(_aggregate_worker_stats)
        mem, links, words = agg["mem"], agg["links"], agg["words"]
        elapsed = clock.get_real_runtime()
        h, m, s = int(elapsed // 3600), int((elapsed % 3600) // 60), int(elapsed % 60)
        # 当前时间：直接显示真实系统时间（虚拟时钟在非 QQ 模式下并非真实 Unix 时间）
        clock_str = dtmod.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        clock_ts = time.time()

        model_status = {}
        for name, b in BACKENDS.items():
            model_status[name] = {
                "use_default": b.use_default(),
                "local_ready": b.is_local_ready(),
                "api_model": b.api_model(),
                "port": b.local_port,
            }

        return web.json_response({
            "mem": mem, "links": links, "words": words,
            "online_workers": agg["online"],
            "runtime": f"{h:02d}:{m:02d}:{s:02d}",
            "clock": clock_str,
            "clock_ts": clock_ts,
            "cognition": {"running": GLOBAL_COGNITION_ENABLED},
            "models": model_status,
            "initialized": RUNTIME.initialized,
            "bot_name": BOT_NAME,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_config_get(request):
    """返回配置（后端开关 + API + bot 名）。

    出于安全考虑，API Key 不返回给前端（置空）。
    前端保存时若未改动 key，保持原值；只有用户重新填写才覆盖。
    """
    safe = json.loads(json.dumps(config))
    for name in ("text", "embed", "multimodal"):
        safe["backends"].get(name, {})["api_key"] = ""
    return web.json_response(safe)


def _write_client_gateway(port: int):
    """把客户端 gateway 指向新端口（客户端刷新后即连接新端口）。"""
    path = CLIENT_DIR / "client_config.json"
    try:
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data["gateway"] = f"http://127.0.0.1:{port}"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[配置] 客户端 gateway 已同步为 http://127.0.0.1:%d", port)
    except Exception:
        logging.exception("[配置] 写入 client_config.json 失败")


async def _apply_server_port(port: int):
    """实时切换 HTTP 监听端口：停掉旧 site，在新端口启动新 site（复用同一 runner）。"""
    global _SITES, _CURRENT_PORT, _CURRENT_HOST
    if port == _CURRENT_PORT:
        return
    for site in _SITES:
        await site.stop()
    _SITES = []
    site = web.TCPSite(_RUNNER, _CURRENT_HOST, port)
    await site.start()
    _SITES = [site]
    _CURRENT_PORT = port
    logging.info("[配置] Web 控制面板监听端口已实时切换为 http://%s:%d", "127.0.0.1" if _CURRENT_HOST == "0.0.0.0" else _CURRENT_HOST, port)


async def api_config_save(request):
    """保存配置（「配置」页：Bot 名字 / 服务端监听端口 / 网页版客户端端口；「模型」页走 /api/model/switch）。

    - bot_name：仅修改配置，重启进程生效（原行为不变）
    - server_port：写入配置 + 同步客户端 gateway + 实时切换监听端口（无需重启）
    - client_port：写入配置 + 实时切换网页版客户端监听端口（无需重启）
    """
    data = await request.json() or {}
    bot_name = (data.get("bot_name") or "").strip()
    if bot_name:
        config["bot_name"] = bot_name

    port_msg = ""
    new_port = data.get("server_port")
    if new_port is not None:
        try:
            new_port = int(new_port)
        except (TypeError, ValueError):
            new_port = -1
        if not (1 <= new_port <= 65535):
            return web.json_response({"ok": False, "message": "端口需为 1-65535 的整数。"})
        config["server_port"] = new_port
        _write_client_gateway(new_port)
        try:
            await _apply_server_port(new_port)
            port_msg = f"，服务端端口已实时切换为 {new_port}"
        except Exception as e:
            logging.exception("[配置] 端口切换失败")
            return web.json_response({"ok": False, "message": f"端口切换失败: {e}"})

    client_port_msg = ""
    new_client_port = data.get("client_port")
    if new_client_port is not None:
        try:
            new_client_port = int(new_client_port)
        except (TypeError, ValueError):
            new_client_port = -1
        if not (1 <= new_client_port <= 65535):
            return web.json_response({"ok": False, "message": "网页版客户端端口需为 1-65535 的整数。"})
        config["client_port"] = new_client_port
        try:
            await _apply_client_port(new_client_port)
            client_port_msg = f"，网页版客户端端口已实时切换为 {new_client_port}"
        except Exception as e:
            logging.exception("[配置] 网页版客户端端口切换失败")
            return web.json_response({"ok": False, "message": f"网页版客户端端口切换失败: {e}"})

    # 无界面自动启动认知循环开关（重启进程生效）
    if "auto_start_cognition" in data:
        config["auto_start_cognition"] = bool(data.get("auto_start_cognition"))

    # 宵禁（睡眠）时间：sleep_start / sleep_end（HH:MM，支持跨天），保存后实时生效
    sleep_msg = ""
    if "sleep_start" in data or "sleep_end" in data:
        import re as _re
        def _valid_hhmm(v):
            return bool(_re.match(r"^\d{1,2}:\d{2}$", str(v or "").strip()))

        new_start = str(data.get("sleep_start", config.get("sleep_start", "23:00")) or "").strip()
        new_end = str(data.get("sleep_end", config.get("sleep_end", "07:00")) or "").strip()
        if not (_valid_hhmm(new_start) and _valid_hhmm(new_end)):
            return web.json_response({"ok": False, "message": "宵禁时间格式应为 HH:MM（如 23:00 / 07:00）。"})
        config["sleep_start"] = new_start
        config["sleep_end"] = new_end
        try:
            from core.virtual_clock import clock
            clock.reload_sleep_config()
            # 起止时间相同时不宵禁
            if clock.sleep_start_min == clock.sleep_end_min:
                sleep_msg = "，起止时间相同，已取消宵禁"
            else:
                sleep_msg = f"，宵禁时间已更新为 {new_start}–{new_end}"
        except Exception:
            logging.exception("[配置] 宵禁时间实时生效失败")

    save_config(config)
    reload_config()
    logging.info("配置已保存（bot_name=%s%s%s%s）", config.get("bot_name"), port_msg, client_port_msg, sleep_msg)
    return web.json_response({"ok": True, "needs_restart": bool(bot_name),
                              "message": f"已保存{port_msg}{client_port_msg}"})


_BOT_AVATAR_DIR = CLIENT_DIR / "images"
_BOT_AVATAR_FILE = _BOT_AVATAR_DIR / "bot_avatar.jpg"
_BOT_AVATAR_DEFAULT_FILE = _BOT_AVATAR_DIR / "bot_avatar_default.jpg"
_BOT_AVATAR_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_BOT_AVATAR_MAX_BYTES = 5 * 1024 * 1024  # 5MB


async def api_bot_avatar_upload(request):
    """上传机器人头像：POST /api/bot_avatar  (management, multipart file 字段).

    固定写入 client/images/bot_avatar.jpg（覆盖），客户端通过 /images/bot_avatar.jpg
    访问；未上传时使用 bot_avatar_default.jpg 兜底。校验图片类型与大小。
    """
    try:
        post = await request.post()
    except Exception as e:
        return web.json_response({"ok": False, "message": f"解析上传失败: {e}"})
    reader = post.get("file")
    if reader is None:
        return web.json_response({"ok": False, "message": "未找到 file 字段（请用 form-data 的 file 字段上传图片）"})
    data = reader.file.read()
    ctype = reader.content_type or ""
    ext = _BOT_AVATAR_TYPES.get(ctype.lower())
    if not ext:
        return web.json_response({"ok": False, "message": f"不支持的图片类型 {ctype or '未知'}（仅支持 jpg / png / webp）"})
    if len(data) > _BOT_AVATAR_MAX_BYTES:
        return web.json_response({"ok": False, "message": "图片过大（限制 5MB）"})
    if not data:
        return web.json_response({"ok": False, "message": "图片内容为空"})

    _BOT_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    # 统一压缩为小尺寸（与用户头像同规格），避免 640px+ 大图拖慢客户端加载；
    # 压缩失败则回退保存原始字节。
    norm, nerr = await asyncio.to_thread(_normalize_avatar_bytes, data)
    if norm:
        with open(_BOT_AVATAR_FILE, "wb") as f:
            f.write(norm)
        logging.info("[头像] 机器人头像已更新并压缩（%d -> %d 字节, %s）", len(data), len(norm), ctype)
    else:
        with open(_BOT_AVATAR_FILE, "wb") as f:
            f.write(data)
        logging.info("[头像] 机器人头像已更新（%d 字节, %s，未压缩: %s）", len(data), ctype, nerr)
    return web.json_response({"ok": True, "url": "/images/bot_avatar.jpg"})


async def api_model_switch(request):
    """切换某个后端的模式：/api/model/switch  body: {name, use_default, api_base_url, api_key, api_model}"""
    data = await request.json()
    name = data.get("name")
    result = _switch_backend(
        name,
        data.get("use_default", True),
        data.get("api_base_url", ""),
        data.get("api_key", ""),
        data.get("api_model", ""),
    )
    return web.json_response(result)


# ------------------------------------------------------------------------
# 维护页：语义向量维度重建
# ------------------------------------------------------------------------
async def api_embed_dim_status(request):
    """返回维度重建工具的状态：当前存储维度 / 模型输出维度 / 记忆数 / 任务进度。"""
    from core import embed_rebuild
    status = embed_rebuild.rebuild_status()
    # 任务未运行时，若 embed 后端已就绪，可即时探测模型输出维度（不触发模型启动）
    model_dim = status.get("model_dim")
    if model_dim is None and not status.get("running"):
        try:
            from core.model_backend import BACKENDS
            if BACKENDS["embed"].is_local_ready():
                model_dim = embed_rebuild.get_model_dim()
        except Exception:
            model_dim = None
    return web.json_response({
        "stored_dim": status.get("stored_dim"),
        "model_dim": model_dim,
        "memory_count": status.get("memory_count"),
        "running": status.get("running"),
        "done": status.get("done"),
        "total": status.get("total"),
        "target_dim": status.get("target_dim"),
        "error": status.get("error"),
        "log": status.get("log"),
    })


async def api_embed_dim_rebuild(request):
    """启动全量维度重建：/api/maintenance/embed_rebuild  body: {target_dim?}"""
    from core import embed_rebuild
    data = await request.json() or {}
    target_dim = data.get("target_dim")
    if target_dim is not None:
        try:
            target_dim = int(target_dim)
        except (TypeError, ValueError):
            target_dim = None
    result = embed_rebuild.start_rebuild(target_dim=target_dim)
    return web.json_response(result)


def _start_cognition_global():
    """全局启动认知循环（同步执行，可能在后台线程运行）。

    供管理面板按钮（api_cognition_start）与无界面自动启动
    （--cognition 参数 / auto_start_cognition 配置）共用，逻辑一致不分叉。

    安全优先 + 惰性初始化原则：
      1. 确保主进程集中式模型服务已初始化。
      2. 遍历已注册用户，仅记录"待心跳启动"映射（username -> (memory_user_id, role)）：
         - 学生：memory_user_id = 其专属 user_id；
         - 家长/老师：仅已绑定学生的参与，memory_user_id = 绑定学生 user_id；
      3. **不创建任何 Worker 子进程、不加载任何记忆（热记忆/FAISS/SQLite）**。
         直到该用户前端心跳到达（record_heartbeat），才真正 get_worker 创建子进程
         （届时才加载记忆）并启动其专属认知循环。
    """
    global GLOBAL_COGNITION_ENABLED, _COGNITION_DESIRED, _PENDING_COGNITION_USERS
    GLOBAL_COGNITION_ENABLED = True
    _COGNITION_DESIRED = True
    USER_WORKERS.cognition_enabled = True
    try:
        RUNTIME.initialize()
    except Exception as e:
        # embed/text 模型均未就绪且未配置外部 API 时初始化会失败：
        # 记录警告但不阻断认知循环授权（worker 内部也会按自身能力降级）
        logging.warning("主进程运行环境初始化未完全就绪，继续授权认知循环: %s", e)

    users = _load_users()
    for uname, udata in users.items():
        if not isinstance(udata, dict):
            continue
        role = udata.get("role", ROLE_STUDENT)
        user_id = udata.get("user_id", "")
        if role == ROLE_STUDENT:
            if user_id:
                _PENDING_COGNITION_USERS[uname] = (user_id, ROLE_STUDENT)
        else:
            linked = _normalize_linked(udata.get("linked_students") or [])
            if linked and linked[0].get("user_id"):
                _PENDING_COGNITION_USERS[uname] = (linked[0]["user_id"], role)

    logging.info("认知循环已全局授权开启；等待各用户前端心跳后逐个创建进程并启动（待启动用户数=%d）",
                 len(_PENDING_COGNITION_USERS))


async def api_cognition_start(request):
    try:
        await asyncio.to_thread(_start_cognition_global)
        _broadcast({"type": "status", "text": "认知循环已全局启动（各用户专属子进程已激活）"})
        return web.json_response({"ok": True})
    except Exception as e:
        logging.exception("启动认知循环失败")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_cognition_stop(request):
    global GLOBAL_COGNITION_ENABLED, _COGNITION_DESIRED, _PENDING_COGNITION_USERS
    try:
        GLOBAL_COGNITION_ENABLED = False
        _COGNITION_DESIRED = False
        _PENDING_COGNITION_USERS.clear()
        USER_WORKERS.stop_cognition_for_all()
        _broadcast({"type": "status", "text": "认知循环已全局停止"})
        return web.json_response({"ok": True})
    except Exception as e:
        logging.exception("停止认知循环失败")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_fake_think(request):
    data = await request.json()
    try:
        reply = await asyncio.to_thread(RUNTIME.fake_think, data.get("text", ""))
        return web.json_response({"ok": True, "reply": reply})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_inject_memory(request):
    data = await request.json()
    try:
        memory_id = await asyncio.to_thread(RUNTIME.inject_memory, data.get("content", ""))
        return web.json_response({"ok": True, "memory_id": memory_id})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_save(request):
    try:
        await asyncio.to_thread(RUNTIME.save)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


def _global_log_entries(limit=500):
    """全局日志视图：运行类日志 + 心跳 + 全局非用户相关的警告与报错。

    按需求：每个用户的记忆体思考日志全部放到该用户的日志中，不在总览/全局日志中显示。
    因此：
      1. 排除思考类日志（LOG_CAT_THINKING）；
      2. 排除明确归属于某个具体用户的日志（user != ""，无论属于哪个分类，全部下沉归属到用户日志）；
      3. 排除 aiohttp.access 访问日志噪音；
      4. 仅收纳全局系统运行类日志（LOG_CAT_RUNTIME）、心跳日志及全局警告/报错（is_warn 且 user==""）。
    """
    out = []
    for cat, lines in _log_buffer.items():
        # 思考类日志全部下沉到具体用户日志，总览日志不展示
        if cat == LOG_CAT_THINKING:
            continue
        for e in lines:
            if not isinstance(e, dict):
                continue
            line = e.get("line", "")
            user = (e.get("user") or "").strip()
            # 凡是归属于某个特定用户的日志，均只在该用户的独立日志中显示，不在总览日志中显示
            if user:
                continue
            level = (e.get("level") or "INFO").upper()
            if "aiohttp.access" in line:
                continue  # 访问日志噪音，不计入全局报错视图
            is_warn = level in ("WARNING", "ERROR", "CRITICAL")
            is_heartbeat = "[心跳" in line
            is_runtime = (cat == LOG_CAT_RUNTIME)
            if is_runtime or is_warn or is_heartbeat:
                out.append(e)
    return out[-limit:]


async def api_logs(request):
    """全局日志（运行日志 + 心跳 + 全部警告/报错），供「日志」页使用。"""
    return web.json_response({"logs": _global_log_entries(800)})


async def api_user_logs(request):
    """某用户的日志：从全局缓冲中按 user 归属过滤（可带 cat 参数）。"""
    username = (request.query.get("username") or "").strip()
    want_cat = (request.query.get("cat") or "").strip()
    if not username:
        return web.json_response({"ok": False, "message": "缺少 username"}, status=400)
    out = []
    for cat, lines in _log_buffer.items():
        if want_cat and cat != want_cat:
            continue
        for e in lines:
            if isinstance(e, dict) and e.get("user") == username:
                out.append(e)
    return web.json_response({"ok": True, "username": username, "logs": out[-800:]})


async def api_logs_clear(request):
    """清空日志缓冲（各分类）。"""
    for cat in _log_buffer:
        _log_buffer[cat].clear()
    logging.info("日志缓冲已清空")
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# 多用户控制面板：服务器负载 / 用户列表 / 单用户概况 / 单用户维护
# ---------------------------------------------------------------------------
# 每用户操作锁：保证同一用户的维护操作串行（规避 worker IPC 并发丢响应），
# 不同用户互不阻塞。查看类接口绝不加锁、绝不拉起 worker。
_USER_OP_LOCKS = {}
_USER_OP_LOCKS_GUARD = threading.Lock()


def _user_op_lock(username: str) -> threading.Lock:
    with _USER_OP_LOCKS_GUARD:
        lock = _USER_OP_LOCKS.get(username)
        if lock is None:
            lock = threading.Lock()
            _USER_OP_LOCKS[username] = lock
        return lock


def _collect_system_load() -> dict:
    """采集服务器负载；psutil 不可用时优雅降级（available=False + 应用层指标）。"""
    import platform
    base = {"platform": platform.platform(), "python": platform.python_version()}
    try:
        import psutil  # 可选依赖：缺失则降级
    except Exception as e:
        return {"available": False, "reason": f"未安装 psutil（{e}）", **base}
    try:
        vm = psutil.virtual_memory()
        try:
            du = psutil.disk_usage(str(PROJECT_DIR))
            disk = {"used": du.used, "total": du.total, "percent": du.percent}
        except Exception:
            disk = None
        try:
            load_avg = list(os.getloadavg())
        except Exception:
            load_avg = None
        return {
            "available": True,
            **base,
            "cpu_percent": psutil.cpu_percent(interval=None),
            "cpu_count": psutil.cpu_count(logical=True),
            "mem": {"used": vm.used, "total": vm.total, "percent": vm.percent},
            "disk": disk,
            "proc_count": len(psutil.pids()),
            "load_avg": load_avg,
            "boot_time": psutil.boot_time(),
        }
    except Exception as e:
        return {"available": False, "reason": f"psutil 采集失败（{e}）", **base}


def _user_disk_stats(username: str, user_id: str) -> dict:
    """从磁盘读取某用户的统计（只读，不拉起 worker）。"""
    stats = {"mem": 0, "links": 0, "msgs": 0, "clock": ""}
    try:
        # 记忆体目录：data/_memory/<user_id>/
        mem_dir = PROJECT_DIR / "data" / "_memory" / (user_id or username)
        db_file = mem_dir / "memory.db"
        if db_file.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_file))
            try:
                stats["mem"] = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                try:
                    stats["links"] = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
                except Exception:
                    stats["links"] = 0
            finally:
                conn.close()
        # 历史分块索引：data/<username>/history_index.json
        idx_file = PROJECT_DIR / "data" / username / "history_index.json"
        if idx_file.exists():
            try:
                idx = json.loads(idx_file.read_text(encoding="utf-8"))
                stats["msgs"] = int(idx.get("total", 0))
            except Exception:
                pass
    except Exception:
        pass
    return stats


def _user_live_status(username: str) -> dict:
    """worker 进程/心跳/暂停/认知循环的实时状态（只读内存态，不拉起）。

    在线判定以**心跳**为准（不再依赖认知循环是否启动）：
    客户端登录后即每 15 秒上报一次心跳，或保持活跃 WebSocket 连接，
    因此只要用户在线就能被识别，无需先给辉夜发消息触发认知循环。

    注意：这里**不调用 worker.get_cognition_status()**（那是 IPC，逐用户调用会拖慢
    /api/users 并可能因 worker 异常拖垮整个列表）。认知循环运行态用全局+暂停标记近似。
    """
    now = time.time()
    hb = _USER_HEARTBEATS.get(username)
    alive = False
    pid = None
    running = False
    try:
        worker = USER_WORKERS.get_all_workers().get(username)
        alive = bool(worker and worker.is_alive())
        pid = (worker.process.pid if (worker and worker.process) else None)
        if alive:
            running = GLOBAL_COGNITION_ENABLED and not _USER_PAUSED.get(username, False)
    except Exception:
        alive = False

    is_online = _is_user_online(username, now)
    paused = bool(_USER_PAUSED.get(username, False))
    pending_exit_sec = None
    if username in _PENDING_EXIT_USERS:
        pending_exit_sec = max(0.0, round(_PENDING_EXIT_USERS[username] - now, 1))

    return {
        "worker_alive": alive,
        "is_online": is_online,
        "pid": pid,
        "paused": paused,
        "cognition_running": running,
        "last_heartbeat": hb,
        "idle_sec": (round(now - hb, 1) if hb else None),
        "pending_exit_sec": pending_exit_sec,
    }


async def api_system(request):
    """服务器负载（psutil 缺失/失败时降级返回 available=False）。"""
    return web.json_response(_collect_system_load())


async def api_users(request):
    """用户列表（含只读统计与实时状态，均不拉起 worker）。

    单个用户数据异常不影响整体：逐个用 try 包裹，出错则给出降级条目。
    磁盘统计（SQLite 读取）放到线程池，避免每秒刷新时阻塞事件循环。
    """
    def _collect():
        users = _load_users()
        out = []
        for uname in users:
            uid = ""
            try:
                prof = _user_profile(uname, users=users) or {}
                uid = prof.get("user_id", "")
                st = _user_disk_stats(uname, uid)
                live = _user_live_status(uname)
                out.append({
                    "username": uname,
                    "role": prof.get("role", ROLE_STUDENT),
                    "user_id": uid,
                    "minor_mode": bool(prof.get("minor_mode", False)),
                    "created_at": prof.get("created_at"),
                    "linked_count": len(prof.get("linked_students") or []),
                    "banned": bool(prof.get("banned", False)),
                    "ban_reason": prof.get("ban_reason", ""),
                    "stats": st,
                    **live,
                })
            except Exception as e:
                logging.warning("[用户列表] 读取用户 %s 失败: %s", uname, e)
                out.append({
                    "username": uname, "role": ROLE_STUDENT, "user_id": uid,
                    "minor_mode": False, "created_at": None, "linked_count": 0,
                    "banned": False, "ban_reason": "",
                    "stats": {"mem": 0, "links": 0, "msgs": 0, "clock": ""},
                    "worker_alive": False, "is_online": False, "pid": None, "paused": False,
                    "cognition_running": False, "last_heartbeat": None, "idle_sec": None,
                    "pending_exit_sec": None,
                })
        return out
    out = await asyncio.to_thread(_collect)
    return web.json_response({"ok": True, "users": out})


async def api_user_overview(request):
    """单用户概况（profile + 磁盘统计 + 实时状态；只读，绝不拉起 worker）。"""
    username = (request.query.get("username") or "").strip()
    if not username:
        return web.json_response({"ok": False, "message": "缺少 username"}, status=400)

    def _collect():
        prof = _user_profile(username)
        if not prof:
            return None
        st = _user_disk_stats(username, prof.get("user_id", ""))
        live = _user_live_status(username)
        return {"ok": True, "profile": prof, "stats": st, **live}

    res = await asyncio.to_thread(_collect)
    if res is None:
        return web.json_response({"ok": False, "message": "用户不存在"}, status=404)
    return web.json_response(res)


async def api_user_set_password(request):
    """管理员修改指定用户密码：POST /api/user/password  {username, new_password}

    改密后立即吊销该用户全部登录会话——否则旧 token 在有效期内仍可用，
    用户会以为"密码改了但别人还能登"。
    """
    data = await request.json() or {}
    username = _sanitize_username((data.get("username") or "").strip())
    new_password = str(data.get("new_password") or "")
    if not username:
        return web.json_response({"ok": False, "message": "缺少 username"}, status=400)
    if len(new_password) < 4:
        return web.json_response({"ok": False, "message": "新密码至少 4 位。"}, status=400)
    if len(new_password) > 128:
        return web.json_response({"ok": False, "message": "新密码过长（上限 128 位）。"}, status=400)

    users = await asyncio.to_thread(_load_users)
    rec = users.get(username)
    if not isinstance(rec, dict):
        return web.json_response({"ok": False, "message": "用户不存在"}, status=404)

    def _apply():
        salt = secrets.token_hex(16)
        rec["salt"] = salt
        rec["hash"] = _hash_password(new_password, salt)
        rec["password_changed_at"] = time.time()
        _save_users(users)

    try:
        await asyncio.to_thread(_apply)
    except Exception as e:
        logging.exception("[用户管理] 修改用户 %s 密码失败", username)
        return web.json_response({"ok": False, "message": f"保存失败: {e}"}, status=500)

    revoked = await asyncio.to_thread(_revoke_user_sessions, username)
    _invalidate_ban_cache()
    logging.info("[用户管理] 管理员修改了用户 %s 的密码，已吊销 %d 个会话", username, revoked)
    return web.json_response({
        "ok": True,
        "message": f"已修改 {username} 的密码" + (f"，并强制 {revoked} 个登录会话下线" if revoked else ""),
        "revoked_sessions": revoked,
    })


async def api_user_ban(request):
    """封禁/解封指定用户：POST /api/user/ban  {username, banned, reason}

    封禁会同时：吊销全部会话 + 断开全部 WebSocket + 清理心跳等运行态，
    保证"封了就是真的进不来"，而不是只改一个标记。
    """
    data = await request.json() or {}
    username = _sanitize_username((data.get("username") or "").strip())
    banned = bool(data.get("banned", True))
    reason = str(data.get("reason") or "").strip()[:200]
    if not username:
        return web.json_response({"ok": False, "message": "缺少 username"}, status=400)

    # 防止管理员把自己（控制面板账号同名用户）封掉导致自锁
    try:
        admin_name = str(config.get("admin_username", "admin")).strip()
        if banned and admin_name and username == admin_name:
            return web.json_response({"ok": False, "message": "不能封禁管理员账号本身"}, status=400)
    except Exception:
        pass

    users = await asyncio.to_thread(_load_users)
    rec = users.get(username)
    if not isinstance(rec, dict):
        return web.json_response({"ok": False, "message": "用户不存在"}, status=404)

    def _apply():
        rec["banned"] = banned
        rec["ban_reason"] = reason if banned else ""
        rec["banned_at"] = time.time() if banned else None
        _save_users(users)

    try:
        await asyncio.to_thread(_apply)
    except Exception as e:
        logging.exception("[用户管理] 更新用户 %s 封禁状态失败", username)
        return web.json_response({"ok": False, "message": f"保存失败: {e}"}, status=500)

    _invalidate_ban_cache()
    revoked = ws_closed = 0
    if banned:
        revoked = await asyncio.to_thread(_revoke_user_sessions, username)
        ws_closed = _disconnect_user_ws(username)
        _purge_user_runtime_state(username)
        logging.warning("[用户管理] 已封禁账号 %s（原因：%s），吊销 %d 个会话、断开 %d 个连接",
                        username, reason or "未填写", revoked, ws_closed)
    else:
        logging.info("[用户管理] 已解除账号 %s 的封禁", username)

    return web.json_response({
        "ok": True,
        "banned": banned,
        "message": (f"已封禁 {username}" + (f"，并踢下线 {revoked} 个会话" if revoked else "")) if banned
                   else f"已解除 {username} 的封禁",
        "revoked_sessions": revoked,
        "closed_ws": ws_closed,
    })


async def api_user_bans(request):
    """账号封禁名单：GET /api/user/bans

    返回已封禁（黑名单）与正常（白名单）两组用户，供工具页总览与快速操作。
    """
    def _collect():
        users = _load_users()
        banned, normal = [], []
        for uname, rec in users.items():
            if not isinstance(rec, dict):
                continue
            item = {
                "username": uname,
                "role": rec.get("role", ROLE_STUDENT),
                "user_id": rec.get("user_id", ""),
            }
            if rec.get("banned"):
                item["reason"] = rec.get("ban_reason", "")
                item["banned_at"] = rec.get("banned_at")
                banned.append(item)
            else:
                normal.append(item)
        banned.sort(key=lambda x: x.get("banned_at") or 0, reverse=True)
        normal.sort(key=lambda x: x["username"])
        return banned, normal

    banned, normal = await asyncio.to_thread(_collect)
    return web.json_response({
        "ok": True,
        "banned": banned,
        "normal": normal,
        "banned_count": len(banned),
        "normal_count": len(normal),
    })


async def api_user_maintenance(request):
    """单用户维护：仅在操作时拉起该用户 worker，且同用户操作串行。"""
    data = await request.json() or {}
    username = (data.get("username") or "").strip()
    action = (data.get("action") or "").strip()
    valid = {"rebuild_index", "sleep_maintenance", "purge_expired", "cognition_start", "cognition_stop"}
    if not username or action not in valid:
        return web.json_response({"ok": False, "message": "参数错误（username/action）"}, status=400)
    prof = _user_profile(username)
    if not prof:
        return web.json_response({"ok": False, "message": "用户不存在"}, status=404)
    # 家长/老师没有自己的记忆体：未绑定学生时无可用记忆体，直接拒绝（400，非服务端错误）
    role = prof.get("role", ROLE_STUDENT)
    if role != ROLE_STUDENT:
        linked = [e.get("user_id") for e in (prof.get("linked_students") or []) if e.get("user_id")]
        if not linked:
            return web.json_response({"ok": False, "message": "该家长/老师未绑定学生，无可用记忆体"}, status=400)

    def _run():
        # 按用户串行：避免同用户并发维护导致的 worker IPC 竞态
        with _user_op_lock(username):
            # 记忆体归属：学生用自己 user_id；家长/老师只能操作其绑定学生（第一个）的记忆体
            if role == ROLE_STUDENT:
                muid = prof.get("user_id") or username
            else:
                muid = linked[0]
            worker = USER_WORKERS.get_worker(username, memory_user_id=muid, role=role)  # 操作时才拉起
            if action == "cognition_start":
                _PENDING_EXIT_USERS.pop(username, None)
                _USER_PAUSED[username] = False
                worker.start_cognition()
                return {"ok": True, "message": "认知循环已启动"}
            if action == "cognition_stop":
                worker.stop_cognition()
                return {"ok": True, "message": "认知循环已停止"}
            return worker.maintenance(action)

    try:
        res = await asyncio.to_thread(_run)
        if not res.get("ok"):
            return web.json_response(res, status=500)
        return web.json_response(res)
    except Exception as e:
        logging.exception("用户 %s 维护操作失败: %s", username, e)
        return web.json_response({"ok": False, "message": f"维护失败: {e}"}, status=500)


# ---------------------------------------------------------------------------
# 全站广播（工具页）：持久化到 gateway/broadcast.json，客户端每次打开弹窗提示
# ---------------------------------------------------------------------------
def _load_broadcast() -> dict:
    """读取已保存的广播内容；无内容时返回空结构。"""
    try:
        if BROADCAST_FILE.exists():
            data = json.loads(BROADCAST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "enabled": bool(data.get("enabled", False)),
                    "content": str(data.get("content", "") or ""),
                    "updated_at": data.get("updated_at"),
                }
    except Exception as e:
        logging.warning("[广播] 读取广播文件失败: %s", e)
    return {"enabled": False, "content": "", "updated_at": None}


def _save_broadcast(data: dict):
    """原子写入广播内容（临时文件 + os.replace，避免中断导致文件损坏）。"""
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BROADCAST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, BROADCAST_FILE)


async def api_broadcast_get(request):
    """读取当前全站广播内容（供控制面板「工具」页编辑）。"""
    return web.json_response({"ok": True, **_load_broadcast()})


async def api_broadcast_save(request):
    """保存全站广播：POST /api/broadcast  body: {enabled, content}"""
    data = await request.json() or {}
    content = str(data.get("content", "") or "")
    enabled = bool(data.get("enabled", False))
    if len(content) > 20000:
        return web.json_response({"ok": False, "message": "广播内容过长（上限 20000 字）"}, status=400)
    record = {"enabled": enabled and bool(content.strip()), "content": content, "updated_at": time.time()}
    try:
        await asyncio.to_thread(_save_broadcast, record)
    except Exception as e:
        logging.exception("保存广播失败")
        return web.json_response({"ok": False, "message": f"保存失败: {e}"}, status=500)
    logging.info("[广播] 全站广播已%s（%d 字）", "启用" if record["enabled"] else "停用", len(content))
    return web.json_response({"ok": True, **record})


async def api_broadcast_disable(request):
    """关闭全站广播（清空启用标记，保留内容以便下次修改）。"""
    record = _load_broadcast()
    record["enabled"] = False
    record["updated_at"] = time.time()
    try:
        await asyncio.to_thread(_save_broadcast, record)
    except Exception as e:
        return web.json_response({"ok": False, "message": f"操作失败: {e}"}, status=500)
    logging.info("[广播] 全站广播已关闭")
    return web.json_response({"ok": True, **record})


async def api_broadcast_instant(request):
    """发送实时广播：POST /api/broadcast/instant  body: {content}

    向所有在线 WebSocket 连接即时推送弹窗（在线用户立即弹窗，但仅限实时这一次，刷新不存）。
    """
    data = await request.json() or {}
    content = str(data.get("content", "") or "").strip()
    if not content:
        return web.json_response({"ok": False, "message": "广播内容不能为空"}, status=400)
    if len(content) > 20000:
        return web.json_response({"ok": False, "message": "广播内容过长（上限 20000 字）"}, status=400)
    now = time.time()
    payload = {
        "type": "instant_broadcast",
        "content": content,
        "time": now,
        "sender": "控制面板",
    }
    _broadcast(payload)
    logging.info("[实时广播] 已向所有在线客户端推送即时广播（%d 字）", len(content))
    return web.json_response({"ok": True, "message": "实时广播已成功推送给所有在线客户端"})


async def api_ip_rules_get(request):
    """获取当前 IP 黑白名单规则及访问限流设定。"""
    rules = await asyncio.to_thread(_load_ip_rules)
    ip = _get_request_ip(request)
    return web.json_response({
        "ok": True,
        "whitelist": rules.get("whitelist", []),
        "blacklist": rules.get("blacklist", []),
        "current_ip": ip,
        "rate_limit_per_sec": _RATE_IP_PER_SEC,
    })


async def api_ip_rules_save(request):
    """配置 IP 黑白名单：POST /api/ip_rules

    支持格式：
      - {"action": "add", "type": "whitelist"|"blacklist", "ip": "1.2.3.4"}
      - {"action": "remove", "type": "whitelist"|"blacklist", "ip": "1.2.3.4"}
      - {"whitelist": [...], "blacklist": [...]} (全量替换)
    """
    data = await request.json() or {}
    rules = await asyncio.to_thread(_load_ip_rules)
    wl = set(rules.get("whitelist", []))
    bl = set(rules.get("blacklist", []))

    action = data.get("action")
    if action in ("add", "remove"):
        t = (data.get("type") or "").strip()
        ip = _normalize_ip_str(data.get("ip"))
        if not ip:
            return web.json_response({"ok": False, "message": "IP 地址不能为空"}, status=400)
        target_set = wl if t == "whitelist" else (bl if t == "blacklist" else None)
        if target_set is None:
            return web.json_response({"ok": False, "message": "名单类型必须为 whitelist 或 blacklist"}, status=400)
        if action == "add":
            target_set.add(ip)
            # 白名单与黑名单互斥：加入白名单则自动移出黑名单；反之亦然
            if t == "whitelist":
                bl.discard(ip)
            else:
                wl.discard(ip)
        else:
            target_set.discard(ip)
    else:
        if "whitelist" in data:
            wl = set([ip for ip in (_normalize_ip_str(x) for x in data["whitelist"]) if ip])
        if "blacklist" in data:
            bl = set([ip for ip in (_normalize_ip_str(x) for x in data["blacklist"]) if ip])

    new_rules = {"whitelist": list(wl), "blacklist": list(bl)}
    await asyncio.to_thread(_save_ip_rules, new_rules)
    current_ip = _get_request_ip(request)
    return web.json_response({
        "ok": True,
        "message": "IP 规则已更新生效",
        "whitelist": sorted(list(wl)),
        "blacklist": sorted(list(bl)),
        "current_ip": current_ip,
        "rate_limit_per_sec": _RATE_IP_PER_SEC,
    })


async def api_update(request):
    """在线更新：在项目目录执行 git pull，返回真实输出。

    当前阶段只做代码拉取（不重启、不处理依赖）。设计上刻意把 git 的
    stdout/stderr 原样回显给控制面板，便于在部署环境（如反向代理 +
    非 git 仓库 / 无远端 / 无权限 / 需要凭据）下直接看出失败原因，
    而不是抛一个笼统的"更新失败"。

    后续可用于识别 dll/dlc 或并入启动逻辑，这里先把接口与回显打牢。
    """
    import subprocess

    def _run_git(args, timeout=120):
        """执行 git 子命令，返回 (ok, output)。合并 stdout/stderr 以便回显。"""
        try:
            proc = subprocess.run(
                ["git"] + args,
                cwd=str(PROJECT_DIR),
                capture_output=True,
                text=True,
                timeout=timeout,
                # 避免 git 因等待输入（凭据/分页）而挂死
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"},
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            return proc.returncode == 0, out
        except FileNotFoundError:
            return False, "未找到 git 命令（服务器未安装 git 或不在 PATH 中）"
        except subprocess.TimeoutExpired:
            return False, f"git {' '.join(args)} 执行超时（{timeout} 秒）"
        except Exception as e:
            return False, f"执行 git 失败: {e}"

    def _collect():
        # 先做环境自检，给出明确原因而不是让 git 报一堆难以理解的信息
        ok_repo, repo_out = _run_git(["rev-parse", "--is-inside-work-tree"], timeout=20)
        if not ok_repo or "true" not in repo_out:
            return {
                "ok": False,
                "stage": "check",
                "message": "当前目录不是 git 仓库，无法在线更新（请确认部署方式）",
                "output": repo_out,
            }
        ok_remote, remote_out = _run_git(["remote", "get-url", "origin"], timeout=20)
        if not ok_remote:
            return {
                "ok": False,
                "stage": "check",
                "message": "未配置 origin 远端，无法拉取更新",
                "output": remote_out,
            }

        # 记录更新前的提交，便于判断是否真的有变化
        _, before = _run_git(["rev-parse", "--short", "HEAD"], timeout=20)

        ok, out = _run_git(["pull", "--ff-only"], timeout=120)
        _, after = _run_git(["rev-parse", "--short", "HEAD"], timeout=20)

        changed = bool(before and after and before != after)
        if not ok:
            return {
                "ok": False,
                "stage": "pull",
                "message": "git pull 失败（详见下方输出）",
                "output": out,
                "before": before, "after": after,
            }
        return {
            "ok": True,
            "stage": "pull",
            "changed": changed,
            "message": (f"更新成功：{before} → {after}" if changed
                        else "已是最新版本（无更新）"),
            "output": out,
            "before": before, "after": after,
            "remote": remote_out,
        }

    logging.info("[更新] 收到在线更新请求，开始执行 git pull")
    try:
        result = await asyncio.to_thread(_collect)
    except Exception as e:
        logging.exception("[更新] 执行异常")
        return web.json_response({"ok": False, "message": f"更新异常: {e}", "output": ""}, status=500)

    if result.get("ok"):
        logging.info("[更新] %s", result.get("message"))
    else:
        logging.warning("[更新] 失败(%s): %s | 输出: %s",
                        result.get("stage"), result.get("message"), (result.get("output") or "")[:500])
    return web.json_response(result)


async def api_restart(request):
    """安全重启服务：在子进程中重启当前 webui 进程（保持同样的启动参数）。"""
    logging.info("收到安全重启请求，正在重启服务...")
    _broadcast({"type": "status", "text": "服务正在安全重启..."})

    def _restart_process():
        # 复用安全退出流程完成数据落盘，再由外层调度重新拉起进程
        try:
            _safe_finalize()
        except Exception:
            logging.exception("重启前保存数据失败")
        logging.info("服务已安全重启，正在拉起新进程...")
        try:
            # 用同一解释器与同一启动参数重新执行 webui.py
            argv = [sys.executable] + sys.argv
            os.execv(sys.executable, argv)
        except Exception:
            # os.execv 在部分环境（如被其他程序托管）下不可用，降级为 spawn + 退出
            try:
                import subprocess
                subprocess.Popen([sys.executable] + sys.argv, cwd=str(PROJECT_DIR))
            except Exception:
                logging.exception("重启拉起新进程失败")
            os._exit(0)

    def _force_exit_watchdog():
        time.sleep(30)
        logging.info("安全重启超时，强制终止进程（由启动脚本重新拉起）。")
        os._exit(1)

    threading.Thread(target=_restart_process, daemon=True).start()
    threading.Thread(target=_force_exit_watchdog, daemon=True).start()
    return web.json_response({"ok": True, "message": "服务正在重启..."})


async def api_shutdown(request):
    """安全退出 Web 服务与全部后台。"""
    logging.info("收到安全退出请求，准备关闭所有子进程与服务...")
    _broadcast({"type": "status", "text": "服务正在安全退出..."})

    def _shutdown_process():
        time.sleep(0.4)
        _safe_finalize()
        logging.info("服务已安全退出，进程终止。")
        os._exit(0)

    def _force_exit_watchdog():
        # 兜底看门狗：若正常退出流程超时（各用户子进程优雅停止认知循环约需
        # 15~20 秒），才强制终止进程，保证服务一定能退出。
        time.sleep(30)
        try:
            logging.info("安全退出超时，强制终止服务进程。")
            USER_WORKERS.shutdown_all()
        except Exception:
            pass
        os._exit(1)

    threading.Thread(target=_shutdown_process, daemon=True).start()
    threading.Thread(target=_force_exit_watchdog, daemon=True).start()
    return web.json_response({"ok": True, "message": "服务正在退出..."})


# ------------------------------------------------------------------------
# 退出保护（保证任何路径关闭时都执行最终保存）
# ------------------------------------------------------------------------
_shutdown_guard = threading.Lock()


def _safe_finalize():
    """幂等地执行最终关停与保存。

    供按钮退出 / 信号处理 / atexit / _serve finally 多路径调用，
    通过 _shutdown_guard 保证只执行一次，避免重复保存或并发竞争。
    """
    with _shutdown_guard:
        try:
            USER_WORKERS.shutdown_all()
        except Exception:
            pass
        try:
            RUNTIME.shutdown()
        except Exception:
            logging.exception("最终关停异常")
        from utils.message_history import flush_to_file
        try:
            flush_to_file()
        except Exception:
            pass


def _signal_exit(*_):
    """信号处理器：终端 Ctrl+C / 关闭信号 → 先最终保存，再退出进程。"""
    logging.info("收到退出信号，正在执行最终保存并退出…")
    try:
        _safe_finalize()
    except Exception:
        pass
    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(0)


def _register_exit_protection():
    """注册多层退出保护：

    1. atexit 钩子：进程任何方式退出前兜底保存（含 os._exit 之外的正常退出）
    2. SIGINT / SIGTERM 信号：终端 Ctrl+C / taskkill 优雅信号 → 保存后退出
    """
    import atexit
    atexit.register(_safe_finalize)
    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _signal_exit)
            except (ValueError, OSError):
                pass  # 非主线程或平台不支持时忽略


def _graceful_exit():
    """按钮退出路径：等前端收到响应后执行最终关停。"""
    time.sleep(0.3)
    _safe_finalize()
    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(0)


# ------------------------------------------------------------------------
# 日志配置
# ------------------------------------------------------------------------
class _IgnoreProactorResetFilter(logging.Filter):
    """过滤 Windows asyncio Proactor 的无害回调异常。

    浏览器关闭 WebSocket 连接时，Windows Proactor 事件循环在 socket 清理
    回调中可能抛出 ConnectionResetError（WinError 10054）。这是连接已关闭
    后的无害噪音，不属于真实错误。此过滤器精准匹配该场景，避免日志刷屏，
    同时保留其他 asyncio 真实错误。
    """

    def filter(self, record):
        msg = record.getMessage()
        if "Exception in callback" in msg and "_call_connection_lost" in msg:
            return False
        if "ConnectionResetError" in msg and "10054" in msg:
            return False
        if "远程主机强迫关闭" in msg:
            return False
        return True


def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    session_log = LOG_DIR / f"webui_{time.strftime('%Y%m%d_%H%M%S')}.log"
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    file_handler = logging.FileHandler(session_log, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    global signal_handler
    signal_handler = SignalLogHandler()
    signal_handler.setFormatter(formatter)
    root.addHandler(signal_handler)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    # 静音 aiohttp 的访问日志（Web 轮询/ping 刷屏），保留其 WARNING/ERROR 级日志
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    # 过滤 Windows Proactor 的无害 ConnectionResetError 回调噪音（浏览器断连时）
    logging.getLogger("asyncio").addFilter(_IgnoreProactorResetFilter())
    # 清空上一会话的日志缓冲（环形缓冲仅保留本次会话）
    for cat in _log_buffer:
        _log_buffer[cat].clear()


# ------------------------------------------------------------------------
# CORS：客户端独立存放后可跨源访问（file:// 或其它端口的静态服务器）
# ------------------------------------------------------------------------
@web.middleware
async def _cors_middleware(request, handler):
    origin = request.headers.get("Origin", "")
    if origin:
        # 放行任意来源（客户端为纯本地工具；auth 依赖 token，与 CORS 无关）
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
        return resp
    return await handler(request)


@web.middleware
async def _security_middleware(request, handler):
    """网络安全中间件：IP 黑白名单控制 + IP 访问限流（每秒最多10次） + 已登录签名防重放。"""
    ip = _get_request_ip(request)

    # 0. 黑名单 IP 严格全站拦截：无论访问任何路径直接阻断，禁止访问
    if is_ip_blacklisted(ip) and not is_ip_whitelisted(ip):
        logging.warning("[安全拦截] 黑名单 IP %s 尝试访问 %s，已直接阻断", ip, request.path)
        if "text/html" in (request.headers.get("Accept") or ""):
            return web.Response(
                text="<h1 style='text-align:center;margin-top:22vh;font-family:sans-serif;'>403 Forbidden</h1><p style='text-align:center;color:#666;'>该 IP 已被系统列入黑名单，禁止访问本站点。</p>",
                content_type="text/html",
                status=403
            )
        return web.json_response({"ok": False, "message": "该IP已被系统列入黑名单，禁止访问。"}, status=403)

    # 白名单 IP 豁免所有频率限制与防爆破风控（完全开放，直接放行）
    if is_ip_whitelisted(ip):
        return await handler(request)

    path = request.path

    # 静态前端资源文件（样式、脚本、图片、图标）放行，避免页面初次并行加载多资源时误触发限流
    if path.startswith(("/static", "/admin_static", "/webui_static", "/client", "/images", "/nav", "/stickers")) \
            or path in ("/favicon.ico",):
        return await handler(request)

    # 1. IP 访问频率限制：单 IP 每秒最多 10 次请求（超出则返回 429）
    if not check_ip_rate_limit(request):
        return web.json_response({"ok": False, "message": "请求过于频繁，请稍后重试。"}, status=429)

    # 公开页面与无需鉴权签名的 API（如登录、注册、心跳、基础信息）直接放行
    if path in ("/", "/index.html", "/admin/login") \
            or path.startswith(("/gateway/login", "/gateway/register",
                                "/gateway/heartbeat", "/gateway/profile",
                                "/gateway/chat_image", "/gateway/avatar",
                                "/gateway/broadcast", "/gateway/history")) \
            or path == "/gateway/bot":
        return await handler(request)

    # 2. 已登录用户的单用户限流 + 签名防重放
    token = get_token_from_request(request)
    username = resolve_session(token)
    if username:
        if not check_user_rate_limit(username):
            return web.json_response({"ok": False, "message": "请求过于频繁，请稍后重试。"}, status=429)
        # 若已登录且非 WebSocket，校验时间戳+签名（登录/注册/心跳/静态资源已放行）
        if not request.path.startswith("/ws"):
            if not verify_request_signature(request, username, token):
                return web.json_response({"ok": False, "message": "签名校验失败或请求已过期。"}, status=403)
    return await handler(request)


@web.middleware
async def _cors_options_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
        origin = request.headers.get("Origin", "")
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp
    return await handler(request)


# ------------------------------------------------------------------------
# 管理员 Session 存储与校验（控制面板独立认证，硬编码于配置 api_config）
# ------------------------------------------------------------------------
_ADMIN_SESSIONS = {}  # token -> {"username": str, "expire_at": float}
_ADMIN_SESSION_COOKIE = "nhy_admin_token"


def _create_admin_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _ADMIN_SESSIONS[token] = {
        "username": username,
        "expire_at": time.time() + 86400 * 7,  # 7 天有效期
    }
    return token


def _verify_admin_session(request: web.Request):
    # 1. 从 Cookie 读取
    token = request.cookies.get(_ADMIN_SESSION_COOKIE)
    # 2. 从 Authorization Header 读取
    if not token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
    if not token or token not in _ADMIN_SESSIONS:
        return None
    sess = _ADMIN_SESSIONS[token]
    if time.time() > sess["expire_at"]:
        _ADMIN_SESSIONS.pop(token, None)
        return None
    return sess


async def api_admin_login(request: web.Request):
    """管理员登录接口：校验账号密码，成功后设置 Cookie 并返回 Token。带 IP 密码风控防爆破。"""
    ip = _get_request_ip(request)
    is_blocked, rem_sec, block_mins = check_ip_login_blocked(ip)
    if is_blocked:
        rem_min = max(1, (rem_sec + 59) // 60)
        return web.json_response({
            "ok": False,
            "blocked": True,
            "remaining_seconds": rem_sec,
            "message": f"密码错误次数过多，该IP已被风控限制登录，请于 {rem_min} 分钟后重试。"
        }, status=429)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "请求格式错误"}, status=400)
    uname = str(data.get("username", "")).strip()
    pwd = str(data.get("password", "")).strip()

    expected_uname = str(config.get("admin_username", "admin")).strip()
    expected_pwd = str(config.get("admin_password", "")).strip()
    if not expected_pwd:
        logging.error("[控制面板] 未配置管理员密码，请先在 config/api_config.json 中设置 admin_password")
        return web.json_response({"ok": False, "message": "管理员密码未配置"}, status=503)

    if uname == expected_uname and hmac.compare_digest(pwd.encode("utf-8"), expected_pwd.encode("utf-8")):
        record_login_success(ip)
        token = _create_admin_session(uname)
        resp = web.json_response({"ok": True, "token": token, "username": uname})
        resp.set_cookie(
            _ADMIN_SESSION_COOKIE,
            token,
            max_age=86400 * 7,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        logging.info("[控制面板] 管理员 '%s' 登录成功 (IP=%s)", uname, ip)
        return resp

    logging.warning("[控制面板] 管理员登录失败：用户名或密码错误 (尝试用户名: %s, IP=%s)", uname, ip)
    is_blocked_now, block_mins, block_secs, failed_count = record_login_failure(ip)
    if is_blocked_now:
        return web.json_response({
            "ok": False,
            "blocked": True,
            "remaining_seconds": block_secs,
            "message": f"密码连续错误 3 次，该IP已被风控限制登录 {block_mins} 分钟，请稍后再试。"
        }, status=429)

    remain_tries = max(0, 3 - failed_count)
    return web.json_response({
        "ok": False,
        "message": f"用户名或密码错误（连续错误 3 次将风控该IP，还剩 {remain_tries} 次机会）"
    }, status=401)


async def api_admin_logout(request: web.Request):
    """管理员退出登录接口：清理 session 并删除 Cookie。"""
    token = request.cookies.get(_ADMIN_SESSION_COOKIE)
    if token:
        _ADMIN_SESSIONS.pop(token, None)
    resp = web.json_response({"ok": True})
    resp.del_cookie(_ADMIN_SESSION_COOKIE, path="/")
    return resp


async def api_admin_me(request: web.Request):
    """查询当前控制面板管理员登录状态。"""
    sess = _verify_admin_session(request)
    if not sess:
        return web.json_response({"ok": False, "authenticated": False}, status=401)
    return web.json_response({"ok": True, "authenticated": True, "username": sess["username"]})


@web.middleware
async def _admin_auth_middleware(request: web.Request, handler):
    """控制面板专属中间件：未登录时拦截所有静态/API，页面请求仅展示独立登录页，零泄露控制面板代码。"""
    path = request.path
    # 登录接口放行
    if path in ("/admin/login", "/favicon.ico"):
        return await handler(request)

    sess = _verify_admin_session(request)
    if not sess:
        # 未登录：如果是页面请求（GET / 或 /index.html 或 /admin 等），返回独立的登录页面
        if request.method == "GET" and (path in ("/", "/index.html", "/admin", "/login") or not path.startswith(("/api/", "/ws", "/admin_static", "/webui_static", "/static"))):
            login_html = WEBUI_DIR / "login.html"
            if login_html.exists():
                return web.FileResponse(login_html)
            return web.Response(text="<h1>401 Unauthorized</h1><p>请登录管理控制面板</p>", content_type="text/html", status=401)
        # API、WebSocket、静态资源等未登录直接 401 拦截
        return web.json_response({"error": "Unauthorized", "code": 401, "message": "请先登录控制面板"}, status=401)

    return await handler(request)


def _register_gateway_routes(app: web.Application):
    """向应用挂载完整的用户网关接口集（/gateway/*）。"""
    app.router.add_post("/gateway/register", gateway_api_register)
    app.router.add_post("/gateway/login", gateway_api_login)
    app.router.add_get("/gateway/bot", gateway_api_bot_info)
    app.router.add_get("/gateway/broadcast", gateway_api_broadcast)
    app.router.add_get("/gateway/profile", gateway_api_profile)
    app.router.add_get("/gateway/history", gateway_api_history)
    app.router.add_post("/gateway/chat", gateway_api_chat)
    app.router.add_post("/gateway/chat_image", gateway_api_chat_image_upload)
    app.router.add_post("/gateway/avatar", gateway_api_avatar_upload)
    app.router.add_post("/gateway/bind", gateway_api_bind)
    app.router.add_post("/gateway/heartbeat", gateway_api_heartbeat)
    app.router.add_post("/gateway/delete_account", gateway_api_delete_account)
    app.router.add_post("/gateway/minor_mode", gateway_api_minor_mode)
    # 社交、好友、群聊与即时消息
    app.router.add_get("/gateway/users/search", gateway_api_search_users)
    app.router.add_post("/gateway/friends/apply", gateway_api_friend_apply)
    app.router.add_get("/gateway/friends/requests", gateway_api_friend_requests)
    app.router.add_post("/gateway/friends/handle_request", gateway_api_friend_handle_request)
    app.router.add_get("/gateway/friends/list", gateway_api_friends_list)
    app.router.add_post("/gateway/friends/delete", gateway_api_friend_delete)
    app.router.add_post("/gateway/friends/settings", gateway_api_friend_settings)
    app.router.add_post("/gateway/groups/create", gateway_api_group_create)
    app.router.add_get("/gateway/groups/list", gateway_api_groups_list)
    app.router.add_get("/gateway/groups/detail", gateway_api_group_detail)
    app.router.add_post("/gateway/groups/settings", gateway_api_group_settings)
    app.router.add_post("/gateway/groups/invite", gateway_api_group_invite)
    app.router.add_post("/gateway/groups/leave", gateway_api_group_leave)
    # 群聊抽奖（抽取 / 骰子）
    app.router.add_get("/gateway/lottery/current", gateway_api_lottery_current)
    app.router.add_post("/gateway/lottery/create", gateway_api_lottery_create)
    app.router.add_post("/gateway/lottery/invite", gateway_api_lottery_invite)
    app.router.add_post("/gateway/lottery/roll", gateway_api_lottery_roll)
    app.router.add_post("/gateway/lottery/draw", gateway_api_lottery_draw)
    app.router.add_post("/gateway/lottery/finish", gateway_api_lottery_finish)
    app.router.add_post("/gateway/lottery/cancel", gateway_api_lottery_cancel)
    app.router.add_post("/gateway/bot/settings", gateway_api_bot_settings)
    app.router.add_get("/gateway/conversations", gateway_api_conversations)
    app.router.add_get("/gateway/messages", gateway_api_messages_get)
    app.router.add_post("/gateway/messages/send", gateway_api_messages_send)
    app.router.add_post("/gateway/messages/recall", gateway_api_messages_recall)
    app.router.add_post("/gateway/messages/read", gateway_api_mark_read)
    # 表情包（贴纸）：清单与图片分发
    app.router.add_get("/gateway/stickers/manifest", gateway_api_stickers_manifest)
    app.router.add_get("/stickers/{pack}/{filename}", sticker_asset_handler)


# ------------------------------------------------------------------------
# 启动与应用构建
# ------------------------------------------------------------------------
def build_client_app() -> web.Application:
    """构建网页版客户端应用（独立端口 8987）：

    提供纯净的客户端静态页面 + 完整的 /gateway/* 用户网关 API，
    完全不暴露控制面板代码与 /api/* 管理接口。
    彻底修复 8987 端口网关请求 404 错误。
    """
    app = web.Application(client_max_size=12 * 1024 * 1024,
                          middlewares=[_cors_options_middleware, _cors_middleware, _security_middleware])

    # 1. 挂载完整网关 API 与客户端专属 WebSocket
    _register_gateway_routes(app)
    app.router.add_get("/ws", client_ws_handler)

    # 2. 客户端静态文件与图标路由
    async def client_favicon(request):
        ico = CLIENT_DIR / "favicon.ico"
        if not ico.exists():
            ico = WEBUI_DIR / "favicon.ico"
        if ico.exists():
            return web.FileResponse(ico)
        return web.Response(status=404)

    app.router.add_get("/favicon.ico", client_favicon)
    app.router.add_get("/", client_index_handler)
    app.router.add_get("/index.html", client_index_handler)
    app.router.add_get("/client_config.json", client_config_handler)
    app.router.add_get("/{name:style.css|app.js|nav_style.css|nav_script.js|logo.PNG|favicon.ico|privacy_policy.md}", client_asset_handler)
    app.router.add_get("/nav/{name:nav_style.css|nav_script.js}", client_asset_handler, name="client_nav_asset")
    app.router.add_get("/images/{name:logo.PNG|favicon.ico|bot_avatar.jpg|bot_avatar_default.jpg|bot_avatar.png|bot_avatar_default.png}", client_asset_handler, name="client_img_asset")
    app.router.add_get("/images/user_avatars/{name}", user_avatar_handler)
    app.router.add_get("/images/ads/{name}", client_ads_handler)
    app.router.add_get("/chat_images/{name}", chat_image_handler)
    app.router.add_static("/client", CLIENT_DIR, name="client_static_prefix")
    return app


def build_app() -> web.Application:
    """构建控制面板应用（独立端口 8787）：

    受 _admin_auth_middleware 严格保护，未登录时仅提供 webui/login.html，
    登录成功后方可加载控制面板的前端文件 (/admin_static, /webui_static) 与 /api/*、/ws。
    """
    app = web.Application(client_max_size=12 * 1024 * 1024, middlewares=[
        _cors_options_middleware,
        _cors_middleware,
        _security_middleware,
        _admin_auth_middleware,
    ])

    # 1. 管理员认证 API 与 Favicon
    async def admin_favicon(request):
        ico = WEBUI_DIR / "favicon.ico"
        if not ico.exists():
            ico = CLIENT_DIR / "favicon.ico"
        if ico.exists():
            return web.FileResponse(ico)
        return web.Response(status=404)

    app.router.add_get("/favicon.ico", admin_favicon)
    app.router.add_post("/admin/login", api_admin_login)
    app.router.add_post("/admin/logout", api_admin_logout)
    app.router.add_get("/admin/me", api_admin_me)

    # 2. 控制面板主入口 (/ 与 /admin)
    app.router.add_get("/", admin_index_handler)
    app.router.add_get("/index.html", admin_index_handler)
    app.router.add_get("/admin", admin_index_handler)
    app.router.add_get("/admin/index.html", admin_index_handler)

    # 3. 静态资源（登录鉴权通过后才能访问）
    app.router.add_static("/admin_static", WEBUI_DIR, name="admin_static")
    app.router.add_static("/webui_static", WEBUI_DIR, name="webui_static")
    app.router.add_static("/static", WEBUI_DIR, name="static_dir")

    # 4. 控制面板专属 API & WebSocket
    app.router.add_get("/admin/ws", ws_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/system", api_system)
    app.router.add_get("/api/users", api_users)
    app.router.add_get("/api/user/overview", api_user_overview)
    app.router.add_get("/api/user_logs", api_user_logs)
    app.router.add_post("/api/user/maintenance", api_user_maintenance)
    # 管理员：修改用户密码 / 封禁解封 / 封禁名单
    app.router.add_post("/api/user/password", api_user_set_password)
    app.router.add_post("/api/user/ban", api_user_ban)
    app.router.add_get("/api/user/bans", api_user_bans)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config_save)
    app.router.add_post("/api/bot_avatar", api_bot_avatar_upload)
    app.router.add_post("/api/model/switch", api_model_switch)
    app.router.add_get("/api/maintenance/embed_dim", api_embed_dim_status)
    app.router.add_post("/api/maintenance/embed_rebuild", api_embed_dim_rebuild)
    app.router.add_post("/api/cognition/start", api_cognition_start)
    app.router.add_post("/api/cognition/stop", api_cognition_stop)
    app.router.add_post("/api/fake_think", api_fake_think)
    app.router.add_post("/api/inject_memory", api_inject_memory)
    app.router.add_post("/api/save", api_save)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_post("/api/logs/clear", api_logs_clear)
    app.router.add_get("/api/broadcast", api_broadcast_get)
    app.router.add_post("/api/broadcast", api_broadcast_save)
    app.router.add_post("/api/broadcast/disable", api_broadcast_disable)
    app.router.add_post("/api/broadcast/instant", api_broadcast_instant)
    app.router.add_get("/api/ip_rules", api_ip_rules_get)
    app.router.add_post("/api/ip_rules", api_ip_rules_save)
    app.router.add_post("/api/update", api_update)
    app.router.add_post("/api/restart", api_restart)
    app.router.add_post("/api/shutdown", api_shutdown)

    # 启动时加载 IP 规则到内存缓存
    _load_ip_rules()

    # 兼顾本地调试挂载网关
    _register_gateway_routes(app)
    return app


def main():
    args = sys.argv[1:]
    # 解析主机地址：命令行 --host 优先，其次读配置 host，默认 0.0.0.0
    host = "0.0.0.0"
    if "--host" in args:
        idx = args.index("--host")
        if idx + 1 < len(args):
            host = args[idx + 1].strip()
    else:
        try:
            cfg_host = config.get("host")
            if cfg_host:
                host = str(cfg_host).strip()
        except Exception:
            pass

    # 解析端口：命令行 --port 优先，否则读配置中保存的 server_port
    port = DEFAULT_WEB_PORT
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])
    else:
        try:
            cfg_port = config.get("server_port")
            if cfg_port:
                port = int(cfg_port)
        except Exception:
            pass

    # 网页版客户端独立端口（读配置；随服务器运行）
    client_port = DEFAULT_CLIENT_PORT
    try:
        cfg_cp = config.get("client_port")
        if cfg_cp:
            client_port = int(cfg_cp)
    except Exception:
        pass

    # 无界面自动启动认知循环：--cognition 命令行参数优先，其次读配置 auto_start_cognition
    auto_cognition = "--cognition" in args
    if not auto_cognition:
        auto_cognition = bool(config.get("auto_start_cognition"))

    os.chdir(PROJECT_DIR)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    configure_logging()
    logging.info("Web 控制面板启动，项目目录=%s，监听地址=%s", PROJECT_DIR, host)

    # 主进程生命周期统一托管：把 multiprocessing 的辅助进程（resource_tracker）
    # 也绑定到共享 Job Object，保证主进程退出（含强杀）时连同这些辅助进程一并回收。
    # 注意：resource_tracker 在 import阶段创建 USER_WORKERS 的 Queue 时已被启动。
    try:
        import multiprocessing.resource_tracker as _rt
        from core.win_job import assign_pid
        _tracker_pid = getattr(getattr(_rt, "_resource_tracker", None), "_pid", None)
        if _tracker_pid:
            assign_pid(_tracker_pid)
    except Exception:
        pass

    # 系统启动：全局只允许一个 llama-server，先杀掉所有残留/其它实例的 llama-server
    # （孤儿进程、崩溃遗留、其它实例启动的进程都会被清理，避免端口冲突与内存翻倍）
    try:
        from core.model_backend import kill_all_llama_servers
        kill_all_llama_servers()
    except Exception:
        logging.exception("启动时清理 llama-server 进程失败")

    # 事件总线 → WebSocket 转发
    BUS.subscribe("message", _bus_forwarder)
    BUS.subscribe("status", _bus_forwarder)

    # 注册退出保护：atexit + 信号 → 保证任何方式关闭时最终保存
    _register_exit_protection()

    app = build_app()
    runner = web.AppRunner(app)
    asyncio.run(_serve(runner, port, client_port, auto_cognition, host=host))


async def _start_client_site(cp: int, host: str = "0.0.0.0"):
    """在独立端口启动网页版客户端服务（随服务器运行）。"""
    global _CLIENT_RUNNER, _CLIENT_SITES, _CLIENT_PORT
    runner = web.AppRunner(build_client_app())
    await runner.setup()
    site = web.TCPSite(runner, host, cp)
    await site.start()
    _CLIENT_RUNNER = runner
    _CLIENT_SITES = [site]
    _CLIENT_PORT = cp
    logging.info("网页版客户端已就绪：http://%s:%d （与本地客户端相同）", "127.0.0.1" if host == "0.0.0.0" else host, cp)


async def _apply_client_port(cp: int):
    """实时切换网页版客户端监听端口（配置页保存 client_port 时调用）。"""
    global _CLIENT_RUNNER, _CLIENT_SITES, _CLIENT_PORT, _CURRENT_HOST
    if cp == _CLIENT_PORT:
        return
    for site in _CLIENT_SITES:
        await site.stop()
    _CLIENT_SITES = []
    if _CLIENT_RUNNER:
        await _CLIENT_RUNNER.cleanup()
        _CLIENT_RUNNER = None
    if cp == _CURRENT_PORT:
        # 与主服务端口相同：不再独立监听，主服务根路径已提供客户端
        _CLIENT_PORT = cp
        logging.info("网页版客户端端口与服务端相同（%d），共用主服务入口", cp)
        return
    await _start_client_site(cp, host=_CURRENT_HOST)


async def _serve(runner, port, client_port=None, auto_cognition=False, host: str = "0.0.0.0"):
    global _ws_broadcast_queue, _ws_broadcast_task, _main_loop, _RUNNER, _SITES, _CURRENT_PORT, _CURRENT_HOST, _heartbeat_watchdog_task
    # 记录主事件循环 + 初始化广播队列，供任意线程的日志/事件入队
    _main_loop = asyncio.get_event_loop()
    _ws_broadcast_queue = asyncio.Queue(maxsize=2000)
    _ws_broadcast_task = asyncio.create_task(_ws_broadcast_worker())
    _heartbeat_watchdog_task = asyncio.create_task(_heartbeat_watchdog())
    # 记录 runner 与初始 site（供「配置」页实时切换端口）
    _RUNNER = runner
    _CURRENT_PORT = port
    _CURRENT_HOST = host

    # 启动 IPC 日志与事件监听线程：将所有子进程 Worker 的日志与事件聚合到主进程
    def _ipc_log_worker():
        while True:
            try:
                item = USER_WORKERS.log_queue.get()
                if item is None:
                    break
                if isinstance(item, dict) and item.get("_ipc_type") == "bus_event":
                    topic = item.get("topic")
                    args = item.get("args", ())
                    _bus_forwarder(topic, *args)
                elif isinstance(item, dict) and item.get("_ipc_type") == "worker_stats":
                    # 子进程主动上报的热数据规模：写入缓存供总览汇总
                    uname = item.get("username")
                    if uname:
                        with _WORKER_STATS_LOCK:
                            _WORKER_STATS[uname] = {
                                "mem": int(item.get("mem", 0) or 0),
                                "links": int(item.get("links", 0) or 0),
                                "words": int(item.get("words", 0) or 0),
                                "ts": time.time(),
                            }
                elif hasattr(item, "name"):
                    logger = logging.getLogger(item.name)
                    logger.handle(item)
            except Exception:
                pass

    ipc_log_thread = threading.Thread(target=_ipc_log_worker, daemon=True, name="IPC-Log-Drainer")
    ipc_log_thread.start()

    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    _SITES = [site]
    logging.info("Web 控制面板已就绪：http://%s:%d （按 Ctrl+C 退出）", "127.0.0.1" if host == "0.0.0.0" else host, port)

    # 网页版客户端独立端口：随服务器启动（与本地客户端相同的网页服务）
    if client_port and client_port != port:
        try:
            await _start_client_site(client_port, host=host)
        except Exception:
            logging.exception("启动网页版客户端服务失败（端口=%s）", client_port)

    # 无界面自动启动认知循环（--cognition 参数或配置 auto_start_cognition）
    # 安全优先：授权开启后等待各用户前端心跳才真正启动其认知循环
    if auto_cognition:
        logging.info("[认知循环] 检测到自动启动配置，正在授权开启认知循环（等待前端心跳后逐个启动）...")
        await asyncio.to_thread(_start_cognition_global)
        _broadcast({"type": "status", "text": "认知循环已授权开启（等待前端心跳后逐个启动）"})

    try:
        # 保持事件循环运行（等待 Ctrl+C / 退出请求）
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if _heartbeat_watchdog_task:
            _heartbeat_watchdog_task.cancel()
        if _ws_broadcast_task:
            _ws_broadcast_task.cancel()
        try:
            if _CLIENT_RUNNER:
                await _CLIENT_RUNNER.cleanup()
        except Exception:
            pass
        try:
            await runner.cleanup()
        except Exception:
            pass
        # 兜底最终保存（Ctrl+C 等路径在关闭 runner 后仍保存）
        _safe_finalize()


if __name__ == "__main__":
    main()
