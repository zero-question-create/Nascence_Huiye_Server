import os, json, threading, uuid

from core import runtime_paths

# ==========================================================================
# 历史对话存储
#   - 内存快照 _message_history：最近 _max_history 条，供认知循环/提示词
#     (get_recent / get_all) 即时使用 —— 行为与旧版一致，零改动。
#   - 磁盘分块 history_<seq>.json：完整历史（追加式，每 _CHUNK_SIZE 条一块），
#     供前端分页回溯（read_history_page）；不受 _max_history 截断。
#   - message_state.json：兜底快照（旧逻辑/未迁移场景仍可读）。
#   - history_index.json：分块索引，记录 total 与各块边界。
# ==========================================================================
_message_history = []
_max_history = 200
_lock = threading.Lock()
_flushed_count = 0

_HISTORY_FILE = "data/test/message_history.log"
_STATE_FILE = "data/test/message_state.json"
_INDEX_FILE = "data/test/history_index.json"
_CHUNK_SIZE = 30


def _refresh_paths():
    global _HISTORY_FILE, _STATE_FILE, _INDEX_FILE
    # 历史对话文件放在"对话历史目录"（data/<username>），与记忆体目录分离
    base = runtime_paths.get_history_dir()
    _HISTORY_FILE = os.path.join(base, "message_history.log")
    _STATE_FILE = os.path.join(base, "message_state.json")
    _INDEX_FILE = os.path.join(base, "history_index.json")


runtime_paths.register(_refresh_paths)


def _chunk_path(seq: int) -> str:
    """返回第 seq 个分块文件的完整路径（seq 越大越新）。"""
    base = os.path.dirname(_STATE_FILE)
    return os.path.join(base, f"history_{seq}.json")


def _is_image(record) -> bool:
    """是否为图片消息（通过 mtype 标记）。"""
    return isinstance(record, dict) and record.get("mtype") == "image"


def _display_text(record) -> str:
    """返回消息用于文本上下文（提示词/记忆）的展示文本。

    文本消息：原样返回 content。
    图片消息：转成"xx发送了一张图片，内容是“desc”"；无描述则"xx发送了一张图片"。
    这样历史传入 LLM 时能看到图片消息的语义描述，而不是被直接过滤掉。
    """
    if not isinstance(record, dict):
        return ""
    sender = record.get("sender", "未知")
    if record.get("mtype") == "image":
        desc = record.get("image_desc") or ""
        if desc:
            return f"{sender}发送了一张图片，内容是“{desc}”"
        return f"{sender}发送了一张图片"
    return record.get("content", "") or record.get("text", "")


def _append_to_chunks(record):
    """把一条新记录追加到磁盘分块（基于全局 context 的历史目录）。"""
    _append_to_chunks_in(os.path.dirname(_STATE_FILE), record)


def _append_to_chunks_in(hist_dir: str, record):
    """把一条新记录追加到指定历史目录的分块（若最新一块满 _CHUNK_SIZE 则滚动新块）。

    仅追加、不覆盖旧块，因此磁盘分块能完整保留全部历史，不受内存快照截断影响。
    hist_dir 独立于全局 runtime_paths 上下文，可用于多用户并发场景按用户定址。
    """
    try:
        os.makedirs(hist_dir, exist_ok=True)
        index_file = os.path.join(hist_dir, "history_index.json")

        def _atomic_write_json(target_path: str, data):
            """原子写入 JSON 文件（写临时文件后 os.replace），避免并发读写导致空文件或残缺 JSON。"""
            tmp_path = target_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, target_path)
            except Exception:
                # 极端异常下重试一次直接写
                try:
                    with open(target_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

        def _load():
            if os.path.exists(index_file):
                for _ in range(3):
                    try:
                        with open(index_file, "r", encoding="utf-8") as f:
                            idx = json.load(f)
                            if isinstance(idx, dict) and "chunks" in idx:
                                return idx
                    except Exception:
                        time.sleep(0.02)
            # 如果 index_file 不可用，尝试扫描现有 history_*.json 重构索引，绝不无脑清零
            try:
                found_chunks = []
                for fn in os.listdir(hist_dir):
                    if fn.startswith("history_") and fn.endswith(".json") and not fn.endswith(".tmp"):
                        try:
                            s = int(fn[len("history_"):-len(".json")])
                            found_chunks.append((s, fn))
                        except ValueError:
                            pass
                if found_chunks:
                    found_chunks.sort(key=lambda x: x[0])
                    c_list = [{"file": fn, "seq": s, "count": _CHUNK_SIZE} for s, fn in found_chunks]
                    return {
                        "total": len(found_chunks) * _CHUNK_SIZE,
                        "chunk_size": _CHUNK_SIZE,
                        "chunks": c_list,
                        "newest_seq": found_chunks[-1][0],
                        "oldest_seq": found_chunks[0][0]
                    }
            except Exception:
                pass
            return {"total": 0, "chunk_size": _CHUNK_SIZE, "chunks": [], "newest_seq": None, "oldest_seq": None}

        def _write(idx):
            _atomic_write_json(index_file, idx)

        def _chunk(seq):
            return os.path.join(hist_dir, f"history_{seq}.json")

        def _write_chunk(seq, chunk):
            _atomic_write_json(_chunk(seq), chunk)

        idx = _load()
        chunks = idx.get("chunks", [])
        total = idx.get("total", 0)
        newest_seq = idx.get("newest_seq")

        if newest_seq is None or not chunks:
            seq = 0
            _write_chunk(seq, [record])
            chunks = [{"file": f"history_{seq}.json", "seq": seq, "count": 1,
                       "start_time": record.get("time"), "end_time": record.get("time")}]
            newest_seq = seq
        else:
            last = chunks[-1]
            seq = last["seq"]
            cur = []
            path = _chunk(seq)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        cur = json.load(f)
                    if not isinstance(cur, list):
                        cur = []
                except Exception:
                    cur = []
            if len(cur) < _CHUNK_SIZE:
                cur.append(record)
                _write_chunk(seq, cur)
                last["count"] = len(cur)
                last["end_time"] = record.get("time")
            else:
                seq = last["seq"] + 1
                _write_chunk(seq, [record])
                chunks.append({"file": f"history_{seq}.json", "seq": seq, "count": 1,
                               "start_time": record.get("time"), "end_time": record.get("time")})
                newest_seq = seq

        idx["total"] = total + 1
        idx["newest_seq"] = newest_seq
        idx["chunks"] = chunks
        _write(idx)
    except Exception:
        # 分块写入失败不应影响正常对话；内存快照仍会兜底保存到 message_state.json
        pass


def _write_chunk_file(seq: int, chunk):
    with open(_chunk_path(seq), "w", encoding="utf-8") as f:
        json.dump(chunk, f, ensure_ascii=False, indent=2)


def _load_index() -> dict:
    if os.path.exists(_INDEX_FILE):
        try:
            with open(_INDEX_FILE, "r", encoding="utf-8") as f:
                idx = json.load(f)
                if isinstance(idx, dict):
                    return idx
        except Exception:
            pass
    return {"total": 0, "chunk_size": _CHUNK_SIZE, "chunks": [], "newest_seq": None, "oldest_seq": None}


def _write_index(idx: dict):
    with open(_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def _with_id(record: dict) -> dict:
    """给历史记录补一个稳定唯一 id（写入时生成一次，之后永不变）。

    为什么必须落盘：前端引用卡片会记住被引用消息的 id，点击时按 id 找回原文。
    若 id 由读取端临时生成（例如"时间+页内下标"），新消息一到页内下标就会整体
    位移，旧 id 立刻失配 → 表现为"点引用跳不过去/找不到原文"。写入时定 id 可
    彻底避免这类位移。
    """
    if not record.get("id"):
        record["id"] = f"h_{uuid.uuid4().hex[:16]}"
    return record


def add_message(sender, content, source, extra=None):
    """写入一条文本历史消息。"""
    from core.virtual_clock import clock
    with _lock:
        record = {"sender": sender, "content": content, "source": source, "time": clock.now()}
        if extra and isinstance(extra, dict) and extra.get("mtype") != "image":
            record.update(extra)
        _with_id(record)
        _message_history.append(record)
        if len(_message_history) > _max_history:
            del _message_history[:len(_message_history) - _max_history]
    _append_to_chunks(record)
    save_state()


def add_image_message(sender, image_url, source, image_desc=None, extra=None):
    """写入一条图片历史消息（content 存占位，image 存 URL，image_desc 存内容描述）。

    图片内容描述供 LLM 历史/认知分析使用（见 _display_text）；不要把原始
    图片数据或 base64 拼进 content。
    """
    from core.virtual_clock import clock
    with _lock:
        record = {"sender": sender, "content": "[图片]", "source": source, "time": clock.now(),
                  "mtype": "image", "image": image_url}
        if image_desc:
            record["image_desc"] = image_desc
        if extra and isinstance(extra, dict):
            record.update(extra)
        _with_id(record)
        _message_history.append(record)
        if len(_message_history) > _max_history:
            del _message_history[:len(_message_history) - _max_history]
    _append_to_chunks(record)
    save_state()


def add_message_for(base_dir: str, username: str, sender: str, content: str, source: str, extra=None):
    """按用户显式定址写入一条文本历史（主进程多用户场景，不依赖全局 context）。

    只写磁盘分块（<base_dir>/data/<username>/），不改动全局内存快照，
    避免多用户并发时内存快照串扰（认知循环各自用独立 Worker 上下文）。
    """
    from core.virtual_clock import clock
    record = {"sender": sender, "content": content, "source": source, "time": clock.now()}
    if extra and isinstance(extra, dict) and extra.get("mtype") != "image":
        record.update(extra)
    _append_to_chunks_in(os.path.join(base_dir, "data", username), _with_id(record))


def add_image_message_for(base_dir: str, username: str, sender: str, image_url: str, source: str, image_desc=None, extra=None):
    """按用户显式定址写入一条图片历史（主进程多用户场景，不依赖全局 context）。"""
    from core.virtual_clock import clock
    record = {"sender": sender, "content": "[图片]", "source": source, "time": clock.now(),
              "mtype": "image", "image": image_url}
    if image_desc:
        record["image_desc"] = image_desc
    if extra and isinstance(extra, dict):
        record.update(extra)
    _append_to_chunks_in(os.path.join(base_dir, "data", username), _with_id(record))


def _from_disk_recent(limit=60):
    """从磁盘分块读取最近 limit 条消息记录（含图片，image_desc 等完整字段）。

    用于认知循环/提示词：主进程通过 add_message_for / add_image_message_for 写入的
    历史只落盘到分块文件，不在本进程内存快照里；这里补读，避免 worker 漏看图片消息。
    按 time 升序返回，每条 content 已用 _display_text 转成可读文本。
    """
    try:
        rows, _ = read_history_page(limit=limit)
    except Exception:
        return []
    out = []
    for m in rows:
        if not isinstance(m, dict):
            continue
        copy = dict(m)
        copy["content"] = _display_text(m)
        out.append(copy)
    # read_history_page 返回时间升序；这里保持升序
    return out


def _merge_memory_and_disk(mem_rows, disk_rows):
    """合并内存快照与磁盘分块记录，按 time 升序，并按 (time, sender, content) 去重。

    disk_rows 优先（含图片完整描述），避免同一条在内存与磁盘间重复。
    """
    out = []
    seen = set()
    for m in list(disk_rows) + list(mem_rows):
        if not isinstance(m, dict):
            continue
        key = (m.get("time"), m.get("sender"), m.get("content"))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    out.sort(key=lambda m: m.get("time") or 0)
    return out


def get_recent(n=10):
    """最近 n 条消息的上下文文本（图片消息转为"发送了一张图片，内容是xx"）。

    合并内存快照与磁盘分块（主进程写入的图片/文本也能被读到）。
    """
    with _lock:
        mem = [dict(m) for m in _message_history[-n * 3:]]
        for m in mem:
            m["content"] = _display_text(m)
        mem = mem[-n:]
    disk = _from_disk_recent(limit=max(n * 3, 30))
    merged = _merge_memory_and_disk(mem, disk)
    recent = merged[-n:]
    lines = []
    for msg in recent:
        lines.append(f"{msg['sender']}说：{msg['content']}")
    return "\n".join(lines)


def get_all():
    """全部历史消息（图片消息以"发送了一张图片，内容是xx"形式返回 content）。

    供认知循环/记忆嵌入使用：合并内存快照 + 磁盘分块（含主进程写入的图片描述），
    既能被 LLM 看到、又不会把原始图片数据/URL 拼进去污染提示词。
    """
    with _lock:
        mem = []
        for m in _message_history:
            if not isinstance(m, dict):
                continue
            copy = dict(m)
            copy["content"] = _display_text(m)
            mem.append(copy)
    disk = _from_disk_recent(limit=120)
    return _merge_memory_and_disk(mem, disk)


def remove_last(n=2):
    with _lock:
        del _message_history[-n:]
    save_state()


def save_state():
    """兜底：把当前内存快照写入 message_state.json（不覆盖分块）。"""
    with _lock:
        data = list(_message_history)
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state():
    """从 message_state.json 重建内存快照（旧的最近 _max_history 条）。"""
    global _message_history, _flushed_count
    if not os.path.exists(_STATE_FILE):
        return
    with open(_STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    with _lock:
        _message_history = data[-_max_history:]
        _flushed_count = len(_message_history)


def flush_to_file():
    global _flushed_count
    with _lock:
        pending = _message_history[_flushed_count:]
        if not pending:
            return
        _flushed_count = len(_message_history)
    os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
    with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
        for msg in pending:
            if _is_image(msg):
                f.write(f"{msg['sender']}说：[图片] {msg.get('image', '')}\n")
            else:
                f.write(f"{msg['sender']}说：{msg['content']}\n")


def _read_chunk(seq: int):
    path = _chunk_path(seq)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def read_history_page(before=None, limit=_CHUNK_SIZE):
    """按时间倒序读取一页历史（分块文件），供前端分页使用。

    before 为 None 时返回最新一页；否则返回 time < before 的更早一页。
    返回 (messages, has_more)，消息为完整字段（含 mtype / image），按时间正序。
    """
    idx = _load_index()
    chunks = idx.get("chunks", [])
    if not chunks:
        # 无分块：退回 message_state.json（旧逻辑）
        if not os.path.exists(_STATE_FILE):
            return [], False
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return [], False
        msgs = [m for m in (data if isinstance(data, list) else []) if isinstance(m, dict)]
        msgs.sort(key=lambda m: m.get("time") or 0)
        return _paginate(msgs, before, limit)

    # 收集所有分块内容，按时间升序合并
    all_msgs = []
    for c in chunks:
        seq = c.get("seq")
        if seq is None:
            fn = c.get("file", "")
            if fn.startswith("history_") and fn.endswith(".json"):
                try:
                    seq = int(fn[len("history_"):-len(".json")])
                except ValueError:
                    continue
        all_msgs.extend(_read_chunk(seq))
    all_msgs.sort(key=lambda m: m.get("time") or 0)
    return _paginate(all_msgs, before, limit)


def _paginate(msgs, before, limit):
    """在已按时间升序的 msgs 上分页。

    before 缺省：取最新 limit 条。before 有值：取 time < before 的最靠后 limit 条。
    返回 (page_msgs, has_more)，page 内时间升序（与 loadHistory 期望一致）；
    has_more 表示是否还有比本页更早的消息。
    """
    msgs = [m for m in msgs if isinstance(m, dict)]
    if before is not None:
        older = [m for m in msgs if (m.get("time") or 0) < before]
    else:
        older = msgs
    page = older[-limit:]
    if page:
        # 还有比本页第一条更早的消息吗？
        first_t = page[0].get("time")
        has_more = any((m.get("time") or 0) < first_t for m in msgs)
    else:
        has_more = False
    return page, has_more


def read_history_page_for(base_dir: str, username: str, before=None, limit=_CHUNK_SIZE):
    """按用户显式定址读取一页历史（不依赖全局 runtime_paths 上下文）。

    直接读取 <base_dir>/data/<username>/ 下的分块文件与索引，适用于 WEB 主进程
    多用户并发场景（全局 runtime_paths 上下文在同一时刻只指向一个用户）。
    返回 (page_msgs, has_more)。
    """
    hist_dir = os.path.join(base_dir, "data", username)
    index_file = os.path.join(hist_dir, "history_index.json")
    state_file = os.path.join(hist_dir, "message_state.json")

    chunks = []
    if os.path.exists(index_file):
        for _ in range(3):
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    idx = json.load(f)
                chunks = idx.get("chunks", []) if isinstance(idx, dict) else []
                if chunks:
                    break
            except Exception:
                time.sleep(0.02)

    # 容错：若索引暂不可读但分块文件存在，直接扫描已有分块
    if not chunks and os.path.isdir(hist_dir):
        try:
            for fn in sorted(os.listdir(hist_dir)):
                if fn.startswith("history_") and fn.endswith(".json") and not fn.endswith(".tmp"):
                    chunks.append({"file": fn})
        except Exception:
            pass

    if chunks:
        all_msgs = []
        for c in chunks:
            seq = c.get("seq")
            fn = c.get("file", "")
            if seq is None:
                if fn.startswith("history_") and fn.endswith(".json"):
                    try:
                        seq = int(fn[len("history_"):-len(".json")])
                    except ValueError:
                        continue
            path = os.path.join(hist_dir, f"history_{seq}.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        m = json.load(f)
                        if isinstance(m, list):
                            all_msgs.extend([x for x in m if isinstance(x, dict)])
                except Exception:
                    pass
        all_msgs = [m for m in all_msgs if isinstance(m, dict)]
        all_msgs.sort(key=lambda m: m.get("time") or 0)
        return _paginate(all_msgs, before, limit)

    # 无分块索引：退回 message_state.json（旧逻辑）
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return [], False
        msgs = [m for m in (data if isinstance(data, list) else []) if isinstance(m, dict)]
        msgs.sort(key=lambda m: m.get("time") or 0)
        return _paginate(msgs, before, limit)
    return [], False
