# core/runtime_paths.py
# ========================================================================
# 运行时数据目录（双目录：记忆体目录 + 对话历史目录）
#
# 多用户共享记忆体架构下：
#   - 记忆体目录 DATA_DIR   : data/_memory/<memory_user_id>（memory.db / faiss /
#                             memory.json / clock_state.txt 等记忆相关文件）
#   - 对话历史目录 HISTORY_DIR : data/<username>（message_state.json /
#                             dialogue_state.json / dialogue_log.jsonl 等）
#
# 各数据模块通过本模块的 file() / file_history() 解析路径，
# 切换目录后由注册的刷新回调统一重算路径常量。
# 兼容旧接口：set_data_dir / set_user / file 保留原语义（单目录模式）。
# ========================================================================

import os

DATA_DIR = os.environ.get("NASCENCE_DATA_DIR", "data/test")
HISTORY_DIR = None          # 对话历史目录（None 时回落 DATA_DIR）

_refreshers = []   # [(callable)]：切换目录后重新计算各模块路径常量


def register(refresher):
    """注册一个路径刷新回调（由各数据模块在导入时调用）。"""
    _refreshers.append(refresher)


def get_data_dir() -> str:
    return DATA_DIR


def get_history_dir() -> str:
    return HISTORY_DIR or DATA_DIR


def set_data_dir(path: str):
    """兼容旧接口：仅设置记忆体目录（历史目录回落同一目录）。"""
    global DATA_DIR
    DATA_DIR = str(path)
    os.makedirs(DATA_DIR, exist_ok=True)
    for fn in _refreshers:
        fn()


def set_context(username: str = None, memory_user_id: str = None, base_dir: str = None):
    """设置双目录上下文：对话目录 = data/<username>，记忆体目录 = data/_memory/<memory_user_id>。

    任一参数为 None 时保持该目录不变；base_dir 为项目根目录（默认当前目录）。
    """
    global DATA_DIR, HISTORY_DIR
    root = base_dir or "."
    if memory_user_id:
        DATA_DIR = os.path.join(root, "data", "_memory", memory_user_id)
        os.makedirs(DATA_DIR, exist_ok=True)
    if username:
        HISTORY_DIR = os.path.join(root, "data", username)
        os.makedirs(HISTORY_DIR, exist_ok=True)
    for fn in _refreshers:
        fn()


def set_user(username: str, base_dir: str = None):
    """兼容旧接口：单用户模式（记忆体与历史同目录 data/<username>）。"""
    set_context(username=username, memory_user_id=None, base_dir=base_dir)
    global DATA_DIR
    DATA_DIR = os.path.join(base_dir or ".", "data", username)
    os.makedirs(DATA_DIR, exist_ok=True)
    for fn in _refreshers:
        fn()


def file(name: str) -> str:
    """返回记忆体目录下某文件的完整路径。"""
    return os.path.join(DATA_DIR, name)


def file_history(name: str) -> str:
    """返回对话历史目录下某文件的完整路径。"""
    return os.path.join(get_history_dir(), name)
