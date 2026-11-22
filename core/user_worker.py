# core/user_worker.py
# ========================================================================
# 单用户专属独立子进程 Worker
#
# 架构设计：
#   1. 每个注册/登录的用户拥有专属的独立子进程（UserWorkerProcess）。
#   2. 子进程内拥有独立的 Python 进程空间、独立的数据目录沙箱（data/<username>/）、
#      独立的 SQLite 连接、独立的 FAISS 向量索引与独立的词网倒排索引。
#   3. 子进程在收到 start_cognition 指令后，在其子进程内部启动属于该用户的
#      认知循环（core.cognition_runner.COGNITION），主进程绝不运行认知循环。
#   4. 所有子进程的模型调用统一向主进程的共享模型后端（llama-server 11437 / API）
#      发起 HTTP 请求，排队共用同一套显存与权重。
#   5. 所有子进程的日志通过 multiprocessing.Queue 统一发回主进程打印并推送前端。
# ========================================================================

import asyncio
import logging
import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
from logging.handlers import QueueHandler
from typing import Dict, Optional

# Windows Job Object：统一由 core.win_job 提供单一 Job，主进程退出（含强杀）时
# 操作系统自动销毁所有绑定到该 Job 的用户子进程，杜绝孤儿进程。
from core.win_job import assign_process as _assign_process


def _handle_user_message(username: str, sender: str, text: str,
                         memory_user_id: str = None, role: str = None,
                         skip_add_history: bool = False, quote: dict = None):
    """后台线程处理用户消息 —— 复刻 0.6.6 qq_bot 的「理解层」消息处理架构：

    1. 消息解读 LLM（decompose_input）将输入拆分为 记忆片段 / 模式 / 新状态 / 关键词
    2. 记忆在解读完直接入库（create_memory，按模式半衰期），附加 user_id/角色标签
    3. 更新对话状态
    4. 消息 jieba 分词关键词注入认知循环队列 —— 下一轮认知循环会将其与
       「上一轮回复结果分词得到的关键词」合并后一起检索
    5. 用户消息写入对话历史（前端即时同步）
    回复由该用户的认知循环异步产出（消费队列关键词 → 检索 → verbalize →
    发言写入历史并推送），前端轮询历史即可获取。
    """
    logger = logging.getLogger(f"UserWorker[{username}]")
    try:
        from core.cognition import (inject_message_keywords, extract_keywords_jieba,
                                    MODE_HALF_LIFE, set_user_context, _mem_user_id,
                                    _mem_speaker_role)
        from core.llm_interface import decompose_input
        from core.memory_engine import create_memory, sync_memory_from_sqlite
        from utils.dialogue_state import set_state
        from utils.message_history import add_message as _add_message
        from utils.message_history import get_all as _get_messages
        from utils.persistence import save_all_data, save_state

        # 多用户共享记忆体：先同步其他进程新写入的记忆
        try:
            sync_memory_from_sqlite()
        except Exception:
            pass

        # 标签：当前记忆体归属（memory_user_id）与当前对话用户角色
        mem_uid = memory_user_id or _mem_user_id() or username
        sp_role = role or _mem_speaker_role()

        if quote and isinstance(quote, dict):
            q_sender = quote.get("sender", "对方")
            q_text = (quote.get("content") or "")[:150]
            augmented = f"{sender}说：（回复 {q_sender}：“{q_text}”）{text}"
        else:
            augmented = f"{sender}说：{text}"
        logger.info(f"[用户对话] 发送者={sender} 输入={augmented} (memory={mem_uid}, role={sp_role})")

        # 发送的消息立即加入历史对话（前端可即时同步到"已发送"状态）
        _prev = _get_messages()
        prev_time = _prev[-1]["time"] if _prev else None
        if not skip_add_history:
            extra = {"quote": quote} if quote else None
            _add_message(sender, text, "客户端", extra=extra)
        _cur = _get_messages()
        cur_time = _cur[-1]["time"] if _cur else (prev_time or 0)
        think_seconds = round(cur_time - prev_time, 2) if (prev_time is not None and cur_time > prev_time) else 0.0

        # ===== 理解层：消息解读 LLM 拆分为记忆 / 关键词 / 状态 / 概念 / 时间指代 =====
        mem_fragments, mode, new_state, _keywords, concept, time_intent = decompose_input(augmented)
        if new_state:
            set_state(new_state)
            save_state()
        # 记忆在解读完直接入库（不去重，按模式半衰期），附加用户/角色标签
        half_life = MODE_HALF_LIFE.get(mode, 2 * 24 * 3600)
        user_mem_ids = []
        for frag in mem_fragments:
            mid = create_memory(frag, half_life=half_life, user_id=mem_uid, speaker_role=sp_role)
            user_mem_ids.append(mid)

        # 归入概念层：把本轮记忆挂到概念下，供日后定向检索与时间回溯。
        # 概念抽不出来时不建概念，该批记忆仍走原有向量检索。
        if concept and user_mem_ids:
            try:
                from core.concept_store import record_concept
                record_concept(concept, user_mem_ids)
            except Exception as e:
                logger.warning(f"概念层写入失败: {e}")

        # 时间指代：交给认知循环，供概念路做时间窗筛选（不做词表解析）
        if time_intent and time_intent != "none":
            try:
                from core.cognition import inject_time_intent
                inject_time_intent(time_intent)
            except Exception as e:
                logger.warning(f"时间指代注入失败: {e}")

        # 消息 jieba 分词关键词注入认知循环队列
        # （下一轮认知循环与上一轮回复分词合并后一起搜索）
        msg_keywords = extract_keywords_jieba(text)
        if msg_keywords:
            inject_message_keywords(msg_keywords)

        save_all_data()
        save_state()
        logger.info(f"[用户对话] 理解层完成，记忆入库 {len(mem_fragments)} 条，"
                    f"jieba关键词: {msg_keywords} (思考时间={think_seconds}s)")
        # 注意：这里不负责启动认知循环——认知循环由 start_cognition 指令统一管理。
        # 若在停止后这里又启动，会把刚停止的循环复活（实测复现过）。
        # 回复由"正在运行"的认知循环消费队列关键词后异步产出并写入历史。
    except Exception as e:
        logger.error(f"消息理解处理异常: {e}\n{traceback.format_exc()}")


def _run_maintenance_op(op: str) -> dict:
    """在用户沙箱内执行维护操作（供 worker 主循环调用）。返回 detail 字典。"""
    if op == "rebuild_index":
        from core.memory_engine import _rebuild_faiss_index
        _rebuild_faiss_index()
        try:
            from utils.persistence import _get_faiss_count
            return {"faiss_count": _get_faiss_count()}
        except Exception:
            return {}
    if op == "sleep_maintenance":
        from utils.persistence import sleep_cleanup
        sleep_cleanup()
        return {}
    if op == "purge_expired":
        from core.memory_engine import _purge_expired_memories
        expired = _purge_expired_memories()
        return {"purged": len(expired) if expired else 0}
    raise ValueError(f"未知维护操作: {op}")


def _worker_main(
    username: str,
    req_queue: multiprocessing.Queue,
    resp_queue: multiprocessing.Queue,
    log_queue: multiprocessing.Queue,
    project_dir: str,
    memory_user_id: str = None,
    role: str = None,
):
    """子进程顶层入口函数（兼容 Windows spawn 与 Linux fork）。"""
    # 0. 标记为子进程环境：本进程绝不启动本地 llama-server（模型统一由主进程
    #    启动并共享，所有认知循环排队共用同一个 llama-server，防止多实例争抢端口）。
    os.environ["NASCENCE_IS_WORKER"] = "1"

    # 1. 确保 sys.path 包含项目根目录
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)

    # 2. 配置子进程日志：将子进程所有日志通过 QueueHandler 发往主进程
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = []
    q_handler = QueueHandler(log_queue)
    root_logger.addHandler(q_handler)

    logger = logging.getLogger(f"UserWorker[{username}]")
    logger.info(f"用户沙箱子进程已启动 (PID={os.getpid()}, User={username})")

    try:
        # 3. 先导入所有数据模块（它们会在导入时向 runtime_paths 注册路径刷新回调）
        from core import runtime_paths
        from utils.message_history import load_state as load_history_state
        from utils.persistence import load_all_data, load_state, save_all_data, save_state
        from core.cognition_runner import COGNITION
        from core.cognition import process_dialogue
        from core import concept_store          # 概念层：须在 set_context 之前导入以注册路径刷新
        from config.constants import BOT_NAME
        from utils.event_bus import BUS

        # 4. 再隔离数据目录路径：对话目录 data/<username>，记忆体目录 data/_memory/<memory_user_id>
        #    （记忆体归属由登录用户与所选学生决定；未指定时回落单用户模式）
        memory_uid = memory_user_id or username
        runtime_paths.set_context(username=username, memory_user_id=memory_uid, base_dir=project_dir)
        
        # 彻底重置全局记忆引擎状态与数据库句柄，确保不同 Worker 间绝对 100% 物理隔离
        from core.memory_engine import reset_engine_state
        reset_engine_state()
        # 概念层同步重置：关闭旧连接、清空概念索引，避免跨用户串数据
        try:
            concept_store.reset_concept_state()
        except Exception:
            logger.exception("概念层状态重置失败")
        
        # 设置用户上下文：记忆创建时自动附加 user_id / speaker_role 标签
        from core.cognition import set_user_context
        set_user_context(memory_uid, role or ("student" if memory_user_id is None else role))

        # 挂载事件总线转发：子进程产生的 message/status 转发回主进程推送前端
        def _ipc_event_forwarder(topic, *args):
            try:
                log_queue.put_nowait({
                    "_ipc_type": "bus_event",
                    "username": username,
                    "topic": topic,
                    "args": args,
                })
            except Exception:
                pass

        BUS.subscribe("message", _ipc_event_forwarder)
        BUS.subscribe("status", _ipc_event_forwarder)

        # 加载沙箱历史与记忆
        load_history_state()
        load_state()
        load_all_data()
        # 概念层：按当前用户目录重建概念索引（失败不阻断，概念路自动降级为不启用）
        try:
            concept_store.reload_from_db()
            logger.info(f"概念层已加载: {concept_store.stats()}")
        except Exception:
            logger.exception("概念层初始化失败，本次运行将不启用概念检索")

        logger.info(f"用户沙箱环境初始化完成，开始监听指令队列")

        # 5. 主循环：监听处理主进程发来的请求
        # 周期性把本沙箱热数据规模推给主进程（供控制面板总览汇总），
        # 采用"子进程主动上报"而非主进程轮询 IPC：避免面板每秒取数时与
        # 正在进行的 chat 请求在 resp_queue 上竞争（_call 会丢弃不匹配的响应）。
        import time as _time
        _last_stats_push = 0.0
        while True:
            try:
                msg = req_queue.get(timeout=0.5)
            except queue.Empty:
                # 空闲时每约 2 秒上报一次热数据规模
                nowt = _time.time()
                if nowt - _last_stats_push >= 2.0:
                    _last_stats_push = nowt
                    try:
                        from core.memory_engine import provide_for_monitor
                        _m, _l, _w = provide_for_monitor()
                        log_queue.put_nowait({
                            "_ipc_type": "worker_stats", "username": username,
                            "mem": _m, "links": _l, "words": _w,
                        })
                    except Exception:
                        pass
                continue

            if msg is None:
                logger.info(f"收到终止指令，正在安全关闭沙箱子进程...")
                # 优雅停止认知循环：完成当前轮（含复搜上限）后再退出，
                # 避免强杀导致认知状态/记忆不一致。
                try:
                    from core.cognition_runner import COGNITION
                    COGNITION.shutdown()
                    logger.info(f"认知循环已优雅停止")
                except Exception:
                    logger.exception("优雅停止认知循环失败")
                # 最终保存一次沙箱数据
                try:
                    from utils.persistence import save_all_data, save_state
                    save_all_data()
                    save_state()
                    logger.info(f"沙箱数据已最终保存")
                except Exception:
                    logger.exception("最终保存失败")
                break

            action = msg.get("action")
            req_id = msg.get("req_id")

            if action == "ping":
                resp_queue.put_nowait({"req_id": req_id, "ok": True, "pong": True, "pid": os.getpid()})

            elif action == "chat":
                sender = msg.get("sender", username)
                text = msg.get("text", "")
                mentioned = msg.get("mentioned", True)

                # 立即确认收到（不阻塞 worker 主循环：停止指令可即时处理，认知循环能及时刹停）
                resp_queue.put_nowait({
                    "req_id": req_id,
                    "ok": True,
                    "reply": "",  # 回复由认知循环异步产出并写入历史，前端轮询获取
                    "think_seconds": 0.0,
                })
                # 理解层（消息解读 LLM 拆记忆/关键词/状态 + 记忆入库 + 关键词入队认知循环）
                # 在后台线程执行，避免长时间阻塞本进程指令队列
                skip_history = bool(msg.get("skip_history", False))
                quote = msg.get("quote")
                threading.Thread(
                    target=_handle_user_message,
                    kwargs={
                        "username": username, "sender": sender, "text": text,
                        "memory_user_id": memory_user_id, "role": role,
                        "skip_add_history": skip_history,
                        "quote": quote,
                    },
                    daemon=True,
                ).start()

            elif action == "start_cognition":
                try:
                    if not COGNITION.is_running():
                        COGNITION.start()
                        logger.info(f"用户子进程专属认知循环已启动")
                    resp_queue.put_nowait({"req_id": req_id, "ok": True, "running": True})
                except Exception as e:
                    logger.error(f"启动认知循环失败: {e}\n{traceback.format_exc()}")
                    resp_queue.put_nowait({"req_id": req_id, "ok": False, "error": str(e)})

            elif action == "stop_cognition":
                try:
                    # 优雅停止：设置停止标志（request_graceful_stop），当前轮跑完即退出。
                    # 立即返回，不等待——等待会阻塞本进程指令队列并触发主进程 _call 超时。
                    COGNITION.stop()
                    logger.info(f"认知循环已请求优雅停止（当前轮完成后退出）")
                    resp_queue.put_nowait({"req_id": req_id, "ok": True, "running": False})
                except Exception as e:
                    logger.error(f"停止认知循环失败: {e}\n{traceback.format_exc()}")
                    resp_queue.put_nowait({"req_id": req_id, "ok": False, "error": str(e)})

            elif action == "get_cognition_status":
                resp_queue.put_nowait({
                    "req_id": req_id,
                    "ok": True,
                    "running": COGNITION.is_running(),
                })

            elif action == "maintenance":
                # 在用户沙箱内执行维护操作（此处 runtime_paths 已指向该用户的数据目录）
                op = msg.get("op")
                try:
                    detail = _run_maintenance_op(op)
                    logger.info(f"维护操作完成: {op}")
                    resp_queue.put_nowait({"req_id": req_id, "ok": True, "op": op, "message": f"维护完成: {op}", "detail": detail})
                except Exception as e:
                    logger.error(f"维护操作失败 ({op}): {e}\n{traceback.format_exc()}")
                    resp_queue.put_nowait({"req_id": req_id, "ok": False, "op": op, "message": f"维护失败: {e}"})

    except Exception as e:
        logger.error(f"子进程发生未捕获致命异常退出: {e}\n{traceback.format_exc()}")
    finally:
        try:
            from core.cognition_runner import COGNITION
            if COGNITION.is_running():
                COGNITION.stop()
        except Exception:
            pass
        logger.info(f"用户沙箱子进程已彻底退出 (PID={os.getpid()})")
        # 强制立即退出：避免 multiprocessing.Queue 的 feeder 线程在进程收尾时挂死
        os._exit(0)


class UserWorkerClient:
    """运行在主进程中、用于与专属子进程进行 IPC 通信的客户端句柄。"""

    def __init__(self, username: str, log_queue: multiprocessing.Queue, project_dir: str,
                 memory_user_id: str = None, role: str = None):
        self.username = username
        self.log_queue = log_queue
        self.project_dir = project_dir
        # 记忆体归属：家长/老师登录后切换到绑定学生时，memory_user_id 指向学生
        self.memory_user_id = memory_user_id or username
        self.role = role
        self.req_queue = multiprocessing.Queue()
        self.resp_queue = multiprocessing.Queue()
        # 可重入锁：_call 持锁期间会调用 ensure_alive（内部同样加锁），
        # 普通 Lock 会导致同一线程二次加锁死锁，必须用 RLock
        self._lock = threading.RLock()
        self.process: Optional[multiprocessing.Process] = None
        self._spawn_process()

    def _spawn_process(self):
        """拉起子进程。"""
        self.process = multiprocessing.Process(
            target=_worker_main,
            args=(
                self.username,
                self.req_queue,
                self.resp_queue,
                self.log_queue,
                self.project_dir,
                self.memory_user_id,
                self.role,
            ),
            name=f"UserWorker-{self.username}",
            daemon=True,
        )
        self.process.start()
        # 绑定到共享 Job Object：主进程消亡（含强杀）时自动终止子进程
        _assign_process(self.process)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.is_alive()

    def ensure_alive(self):
        """确保子进程处于存活状态，若崩溃则自动重启。"""
        with self._lock:
            if not self.is_alive():
                logger = logging.getLogger(f"UserWorker[{self.username}]")
                logger.warning(f"检测到用户子进程未存活，正在自动拉起新实例...")
                self._spawn_process()

    def _call(self, payload: dict, timeout: float = 120.0) -> dict:
        """同步发送请求并等待专属子进程返回结果。

        注意：等待响应期间绝不持有 self._lock（否则 shutdown 会被卡死），
        仅在入队瞬间使用短锁保证同一时刻只有一个请求在途。
        """
        req_id = payload.get("req_id") or f"req_{time.time()}_{id(payload)}"
        payload["req_id"] = req_id

        # 短锁入队（保证同一子进程同一时刻只有一个请求在途）
        with self._lock:
            self.ensure_alive()
            try:
                self.req_queue.put_nowait(payload)
            except Exception as e:
                return {"ok": False, "error": f"发送指令失败: {e}", "reply": f"【系统】无法连接用户沙箱: {e}"}

        # 无锁等待响应（按 req_id 精确匹配）
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = self.resp_queue.get(timeout=0.5)
                if isinstance(resp, dict) and resp.get("req_id") == req_id:
                    return resp
            except queue.Empty:
                # 队列为空时，再检查子进程是否已终止
                if not self.is_alive():
                    return {
                        "ok": False,
                        "error": "Worker 进程异常退出",
                        "reply": "【系统】用户沙箱进程异常退出",
                        "think_seconds": 0.0,
                    }
                continue
            except Exception as e:
                return {"ok": False, "error": str(e), "reply": f"【系统】IPC 错误: {e}"}

        return {
            "ok": False,
            "error": "请求超时",
            "reply": "【系统】用户沙箱响应超时，请稍后重试",
            "think_seconds": timeout,
        }

    def chat(self, sender: str, text: str, mentioned: bool = True, timeout: float = 180.0, skip_history: bool = False, quote: dict = None) -> dict:
        """向子进程发送对话请求。"""
        return self._call({
            "action": "chat",
            "sender": sender,
            "text": text,
            "mentioned": mentioned,
            "skip_history": skip_history,
            "quote": quote,
        }, timeout=timeout)

    def start_cognition(self) -> dict:
        """让子进程启动其专属认知循环。"""
        return self._call({"action": "start_cognition"}, timeout=10.0)

    def maintenance(self, op: str, timeout: float = 300.0) -> dict:
        """在用户沙箱内执行维护操作（重建索引/睡眠维护/清理衰变）。"""
        return self._call({"action": "maintenance", "op": op}, timeout=timeout)

    def stop_cognition(self) -> dict:
        """让子进程停止其专属认知循环。"""
        return self._call({"action": "stop_cognition"}, timeout=10.0)

    def get_cognition_status(self) -> dict:
        """获取子进程认知循环状态。"""
        return self._call({"action": "get_cognition_status"}, timeout=5.0)

    def shutdown(self):
        """安全关闭子进程。

        短锁发送 None 哨兵后立即释放锁；随后在无锁状态下 join，若仍存活则强制 kill，
        避免与正在进行的 _call 等待死锁。
        """
        with self._lock:
            if not self.is_alive():
                return
            try:
                self.req_queue.put_nowait(None)
            except Exception:
                pass

        # 无锁等待子进程退出。子进程收到 None 后会优雅停止认知循环（最多约15秒）
        # 并保存沙箱数据，因此给出充足等待时间；仍存活才强制 kill。
        self.process.join(timeout=20.0)
        if self.process.is_alive():
            try:
                self.process.kill()
            except Exception:
                pass
            self.process.join(timeout=1.0)
        try:
            self.req_queue.close()
            self.req_queue.cancel_join_thread()
        except Exception:
            pass
        try:
            self.resp_queue.close()
            self.resp_queue.cancel_join_thread()
        except Exception:
            pass


class UserWorkerPool:
    """主进程中管理所有用户专属子进程的进程池。"""

    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.log_queue = multiprocessing.Queue()
        self._workers: Dict[str, UserWorkerClient] = {}
        self.cognition_enabled = False
        self._lock = threading.Lock()

    def get_worker(self, username: str, memory_user_id: str = None, role: str = None) -> UserWorkerClient:
        """获取或创建指定用户的专属 Worker 客户端。

        memory_user_id：记忆体归属（家长/老师切换到绑定学生时指向学生 user_id）。
        与已存在 worker 的记忆体不一致时，重建 worker（切换记忆体）。
        """
        memory_user_id = memory_user_id or username
        with self._lock:
            worker = self._workers.get(username)
            if worker is not None and (worker.memory_user_id != memory_user_id or (role and worker.role != role)):
                # 切换记忆体或角色：优雅停旧 worker 后重建
                try:
                    worker.shutdown()
                except Exception:
                    pass
                del self._workers[username]
                worker = None
            if worker is None:
                worker = UserWorkerClient(username, self.log_queue, self.project_dir,
                                          memory_user_id=memory_user_id, role=role)
                self._workers[username] = worker
                if self.cognition_enabled:
                    try:
                        worker.start_cognition()
                    except Exception:
                        pass
            else:
                worker.ensure_alive()
                if self.cognition_enabled:
                    try:
                        worker.start_cognition()
                    except Exception:
                        pass
            return worker

    def get_all_workers(self) -> Dict[str, UserWorkerClient]:
        with self._lock:
            return dict(self._workers)

    def start_cognition_for_all(self):
        """为当前所有已激活的用户子进程启动认知循环。"""
        with self._lock:
            self.cognition_enabled = True
            for username, worker in self._workers.items():
                try:
                    worker.start_cognition()
                except Exception:
                    pass

    def stop_cognition_for_all(self):
        """为当前所有已激活的用户子进程停止认知循环。"""
        with self._lock:
            self.cognition_enabled = False
            for username, worker in self._workers.items():
                try:
                    worker.stop_cognition()
                except Exception:
                    pass

    def start_cognition_for(self, username: str):
        """仅为指定用户启动其专属认知循环（按用户独立控制）。"""
        with self._lock:
            worker = self._workers.get(username)
            if worker:
                try:
                    worker.start_cognition()
                    return True
                except Exception:
                    return False
        return False

    def stop_cognition_for(self, username: str):
        """仅为指定用户暂停其专属认知循环（按用户独立控制）。"""
        with self._lock:
            worker = self._workers.get(username)
            if worker:
                try:
                    worker.stop_cognition()
                    return True
                except Exception:
                    return False
        return False

    def get_active_usernames(self) -> list:
        """返回当前所有已激活 Worker 的用户名列表。"""
        with self._lock:
            return list(self._workers.keys())

    def has_worker(self, username: str) -> bool:
        with self._lock:
            return username in self._workers

    def shutdown_worker(self, username: str) -> bool:
        """安全关闭并注销指定用户的专属子进程，释放系统资源与内存。

        从进程池移除句柄，并在锁外执行 shutdown，等待优雅保存与子进程完全退出。
        """
        with self._lock:
            worker = self._workers.pop(username, None)
        if worker is not None:
            try:
                worker.shutdown()
                return True
            except Exception:
                pass
        return False

    def shutdown_all(self):
        """关闭所有子进程。"""
        with self._lock:
            for worker in self._workers.values():
                try:
                    worker.shutdown()
                except Exception:
                    pass
            self._workers.clear()
