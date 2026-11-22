# gateway/social_manager.py —— 社交、好友、群聊与消息持久化管理
import os
import json
import time
import uuid
import threading
import logging
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
GATEWAY_DIR = PROJECT_DIR / "gateway"
SOCIAL_FILE = GATEWAY_DIR / "social.json"
MESSAGES_DIR = DATA_DIR / "social_messages"

_social_lock = threading.Lock()
_msg_lock = threading.Lock()


def _ensure_dirs():
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    MESSAGES_DIR.mkdir(parents=True, exist_ok=True)


def _load_social() -> dict:
    _ensure_dirs()
    if not SOCIAL_FILE.exists():
        return {"friends": {}, "friend_requests": [], "groups": {}, "user_settings": {}, "read_positions": {}}
    try:
        with open(SOCIAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            data.setdefault("friends", {})
            data.setdefault("friend_requests", [])
            data.setdefault("groups", {})
            data.setdefault("user_settings", {})
            data.setdefault("read_positions", {})
            return data
    except Exception as e:
        logging.warning("[social_manager] 读取 social.json 失败: %s", e)
        return {"friends": {}, "friend_requests": [], "groups": {}, "user_settings": {}, "read_positions": {}}


def _atomic_write(path: Path, data):
    """原子写：先写临时文件再 os.replace，避免中途崩溃导致文件损坏/数据丢失。"""
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _save_social(data: dict):
    _atomic_write(SOCIAL_FILE, data)


def _safe_name(s: str) -> str:
    """净化用于文件名的用户名/ID：去除路径分隔符与控制字符，防路径穿越（保留中文等合法字符）。"""
    s = str(s)
    for bad in ("/", "\\", "\0"):
        s = s.replace(bad, "_")
    s = "".join(c for c in s if ord(c) >= 32)
    return s.replace("..", "_")


def _get_dm_filepath(user1: str, user2: str) -> Path:
    _ensure_dirs()
    u1, u2 = sorted([_safe_name(user1.lower()), _safe_name(user2.lower())])
    return MESSAGES_DIR / f"dm_{u1}_{u2}.json"


def _get_group_filepath(group_id: str) -> Path:
    _ensure_dirs()
    safe_id = _safe_name(group_id)
    return MESSAGES_DIR / f"group_{safe_id}.json"


def _load_messages(filepath: Path) -> list:
    if not filepath.exists():
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            msgs = json.load(f)
            return msgs if isinstance(msgs, list) else []
    except Exception as e:
        logging.warning("[social_manager] 读取消息文件失败 %s: %s", filepath.name, e)
        return []


def _save_messages(filepath: Path, msgs: list):
    _atomic_write(filepath, msgs)


# =========================================================================
# 用户搜索与好友管理
# =========================================================================
def search_users(query: str, current_user: str, all_users_dict: dict) -> list:
    """按用户名或 user_id 搜索全服用户。"""
    query = (query or "").strip().lower()
    if not query:
        return []

    with _social_lock:
        social = _load_social()

    my_friends = social.get("friends", {}).get(current_user, {})
    my_requests = social.get("friend_requests", [])

    results = []
    for uname, urec in all_users_dict.items():
        if not isinstance(urec, dict):
            continue
        if uname.lower() == current_user.lower():
            continue

        uid = str(urec.get("user_id") or "")
        if query in uname.lower() or query in uid.lower():
            is_friend = uname in my_friends
            has_pending = any(
                r.get("from_user") == current_user and r.get("to_user") == uname and r.get("status") == "pending"
                for r in my_requests
            )
            incoming_pending = any(
                r.get("from_user") == uname and r.get("to_user") == current_user and r.get("status") == "pending"
                for r in my_requests
            )
            results.append({
                "username": uname,
                "user_id": uid,
                "role": urec.get("role", "student"),
                "is_friend": is_friend,
                "has_pending_request": has_pending,
                "incoming_request": incoming_pending,
            })
    return results


def apply_friend(from_user: str, to_user: str, message: str = "") -> dict:
    """申请加好友。若对方已向自己申请，则直接成为好友。"""
    if from_user.lower() == to_user.lower():
        return {"ok": False, "message": "不能添加自己为好友"}

    with _social_lock:
        social = _load_social()
        friends = social.setdefault("friends", {})
        my_friends = friends.setdefault(from_user, {})
        if to_user in my_friends:
            return {"ok": False, "message": "双方已经是好友"}

        requests = social.setdefault("friend_requests", [])

        # 检查对方是否已向我申请
        incoming = next(
            (r for r in requests if r.get("from_user") == to_user and r.get("to_user") == from_user and r.get("status") == "pending"),
            None
        )
        if incoming:
            # 对方已申请，直接同意成为好友
            incoming["status"] = "accepted"
            now = time.time()
            my_friends[to_user] = {"alias": "", "created_at": now, "pinned": False, "muted": False}
            friends.setdefault(to_user, {})[from_user] = {"alias": "", "created_at": now, "pinned": False, "muted": False}
            _save_social(social)
            return {"ok": True, "message": "双方互加好友成功", "instant_friend": True}

        # 检查是否已发送过 pending 申请
        existing = next(
            (r for r in requests if r.get("from_user") == from_user and r.get("to_user") == to_user and r.get("status") == "pending"),
            None
        )
        if existing:
            existing["message"] = message or existing.get("message", "")
            existing["created_at"] = time.time()
            _save_social(social)
            return {"ok": True, "message": "好友申请已更新，等待对方同意"}

        req_id = f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        new_req = {
            "id": req_id,
            "from_user": from_user,
            "to_user": to_user,
            "message": message or "请求添加您为好友",
            "status": "pending",
            "created_at": time.time(),
        }
        requests.append(new_req)
        _save_social(social)
        return {"ok": True, "message": "好友申请已发送", "request": new_req}


def get_friend_requests(username: str) -> dict:
    """获取与当前用户相关的收到的申请和发出的申请。"""
    with _social_lock:
        social = _load_social()
    reqs = social.get("friend_requests", [])
    received = [r for r in reqs if r.get("to_user") == username and r.get("status") == "pending"]
    sent = [r for r in reqs if r.get("from_user") == username]
    received.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    sent.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"received": received, "sent": sent}


def handle_friend_request(request_id: str, username: str, action: str) -> dict:
    """处理好友申请（accept / reject）。"""
    with _social_lock:
        social = _load_social()
        requests = social.setdefault("friend_requests", [])
        target_req = next((r for r in requests if r.get("id") == request_id and r.get("to_user") == username), None)
        if not target_req:
            return {"ok": False, "message": "未找到有效的好友申请"}

        from_user = target_req["from_user"]
        if action == "accept":
            target_req["status"] = "accepted"
            now = time.time()
            friends = social.setdefault("friends", {})
            friends.setdefault(username, {})[from_user] = {"alias": "", "created_at": now, "pinned": False, "muted": False}
            friends.setdefault(from_user, {})[username] = {"alias": "", "created_at": now, "pinned": False, "muted": False}
            _save_social(social)
            return {"ok": True, "message": f"已同意与 {from_user} 成为好友", "from_user": from_user}
        elif action == "reject":
            target_req["status"] = "rejected"
            _save_social(social)
            return {"ok": True, "message": f"已拒绝 {from_user} 的好友申请", "from_user": from_user}
        else:
            return {"ok": False, "message": "无效的操作"}


def get_friends_list(username: str, all_users_dict: dict, live_status_fn) -> list:
    """获取好友列表（含在线状态、置顶、免打扰、备注）。"""
    with _social_lock:
        social = _load_social()
    my_friends = social.get("friends", {}).get(username, {})
    out = []
    for f_uname, f_info in my_friends.items():
        urec = all_users_dict.get(f_uname, {})
        live = live_status_fn(f_uname) if live_status_fn else {}
        out.append({
            "username": f_uname,
            "user_id": urec.get("user_id", ""),
            "role": urec.get("role", "student"),
            "alias": f_info.get("alias", ""),
            "pinned": bool(f_info.get("pinned", False)),
            "muted": bool(f_info.get("muted", False)),
            "created_at": f_info.get("created_at"),
            "worker_alive": live.get("worker_alive", False),
            "is_online": live.get("is_online", False),
        })
    out.sort(key=lambda x: (not x["pinned"], x["username"]))
    return out


def delete_friend(username: str, target_user: str) -> dict:
    """删除好友（双向解除）。"""
    with _social_lock:
        social = _load_social()
        friends = social.setdefault("friends", {})
        if target_user in friends.get(username, {}):
            del friends[username][target_user]
        if username in friends.get(target_user, {}):
            del friends[target_user][username]
        _save_social(social)
    return {"ok": True, "message": f"已解除与 {target_user} 的好友关系"}


def set_friend_settings(username: str, target_user: str, pinned: bool = None, muted: bool = None, alias: str = None) -> dict:
    """设置好友的置顶、免打扰或备注名。"""
    with _social_lock:
        social = _load_social()
        my_friends = social.setdefault("friends", {}).setdefault(username, {})
        if target_user not in my_friends:
            return {"ok": False, "message": "该用户不是您的好友"}
        info = my_friends[target_user]
        if pinned is not None:
            info["pinned"] = bool(pinned)
        if muted is not None:
            info["muted"] = bool(muted)
        if alias is not None:
            info["alias"] = str(alias).strip()
        _save_social(social)
    return {"ok": True, "message": "好友设置已更新", "settings": info}


# =========================================================================
# 群聊管理
# =========================================================================
def create_group(name: str, owner: str, members: list) -> dict:
    """创建群聊。建群人数不得少于3人（含创建者自身，即至少邀请2名其他成员）。"""
    name = (name or "").strip() or "新建群聊"
    group_id = f"grp_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    # 保证创建者在群成员中，且去重
    member_set = {owner}
    for m in members or []:
        if isinstance(m, str) and m.strip():
            member_set.add(m.strip())
    # 建群人数校验：群聊人数不少于 3 人
    if len(member_set) < 3:
        return {"ok": False, "message": "建群人数不少于3人（请至少选择邀请2位好友）"}

    now = time.time()
    group_obj = {
        "id": group_id,
        "name": name,
        "owner": owner,
        "members": list(member_set),
        "created_at": now,
        "member_settings": {
            owner: {"pinned": False, "muted": False}
        }
    }
    with _social_lock:
        social = _load_social()
        social.setdefault("groups", {})[group_id] = group_obj
        _save_social(social)

    # 写入第一条系统消息
    save_message(
        chat_type="group",
        sender="系统",
        target=group_id,
        content=f"{owner} 创建了群聊「{name}」",
        msg_type="system"
    )

    return {"ok": True, "message": "群聊创建成功", "group": group_obj}


def get_groups_for_user(username: str) -> list:
    """获取指定用户所在的所有群聊。"""
    with _social_lock:
        social = _load_social()
    groups = social.get("groups", {})
    out = []
    for gid, g in groups.items():
        members = g.get("members", [])
        if username in members:
            m_settings = g.get("member_settings", {}).get(username, {})
            out.append({
                "id": gid,
                "name": g.get("name", "未命名群聊"),
                "owner": g.get("owner", ""),
                "member_count": len(members),
                "pinned": bool(m_settings.get("pinned", False)),
                "muted": bool(m_settings.get("muted", False)),
                "created_at": g.get("created_at"),
            })
    out.sort(key=lambda x: (not x["pinned"], x["created_at"] or 0), reverse=False)
    return out


def _is_group_member(group_id: str, username: str) -> bool:
    """校验 username 是否为 group_id 的成员（用于群聊读写鉴权）。"""
    if not group_id or not username:
        return False
    with _social_lock:
        social = _load_social()
    g = social.get("groups", {}).get(group_id)
    if not g:
        return False
    return username in (g.get("members") or [])


def get_group_members(group_id: str) -> list:
    """返回群成员用户名列表；群不存在时返回空列表。"""
    if not group_id:
        return []
    with _social_lock:
        social = _load_social()
    g = social.get("groups", {}).get(group_id)
    return list(g.get("members") or []) if g else []


def get_group_detail(group_id: str, username: str, all_users_dict: dict, live_status_fn) -> dict:
    """获取群聊详情及成员列表。"""
    with _social_lock:
        social = _load_social()
    g = social.get("groups", {}).get(group_id)
    if not g:
        return {"ok": False, "message": "群聊不存在"}
    members = g.get("members", [])
    if username not in members:
        return {"ok": False, "message": "您不是该群成员"}

    member_details = []
    for m_uname in members:
        urec = all_users_dict.get(m_uname, {})
        live = live_status_fn(m_uname) if live_status_fn else {}
        member_details.append({
            "username": m_uname,
            "user_id": urec.get("user_id", ""),
            "role": urec.get("role", "student"),
            "is_owner": (m_uname == g.get("owner")),
            "is_online": live.get("is_online", False),
        })

    m_settings = g.get("member_settings", {}).get(username, {})
    return {
        "ok": True,
        "group": {
            "id": group_id,
            "name": g.get("name", ""),
            "owner": g.get("owner", ""),
            "created_at": g.get("created_at"),
            "pinned": bool(m_settings.get("pinned", False)),
            "muted": bool(m_settings.get("muted", False)),
            "members": member_details,
        }
    }


def set_group_settings(group_id: str, username: str, name: str = None, pinned: bool = None, muted: bool = None) -> dict:
    """修改群设置（群名：群主可改；置顶/免打扰：成员各设各的）。"""
    sys_msg = None
    with _social_lock:
        social = _load_social()
        g = social.setdefault("groups", {}).get(group_id)
        if not g:
            return {"ok": False, "message": "群聊不存在"}
        if username not in g.get("members", []):
            return {"ok": False, "message": "您不是该群成员"}

        if name is not None and (g.get("owner") == username):
            old_name = g.get("name")
            g["name"] = name.strip() or g["name"]
            if old_name != g["name"]:
                sys_msg = f"{username} 将群名称修改为「{g['name']}」"

        m_settings = g.setdefault("member_settings", {}).setdefault(username, {})
        if pinned is not None:
            m_settings["pinned"] = bool(pinned)
        if muted is not None:
            m_settings["muted"] = bool(muted)

        _save_social(social)
    # 出锁后再写消息，避免与 recall_message 形成锁序反转（死锁）
    if sys_msg:
        save_message(chat_type="group", sender="系统", target=group_id, content=sys_msg, msg_type="system")
    return {"ok": True, "message": "群设置已更新"}


def invite_group_members(group_id: str, inviter: str, new_members: list) -> dict:
    """邀请成员入群（调用方负责校验被邀请者是否为真实用户）。"""
    added = []
    sys_msg = None
    with _social_lock:
        social = _load_social()
        g = social.setdefault("groups", {}).get(group_id)
        if not g:
            return {"ok": False, "message": "群聊不存在"}
        if inviter not in g.get("members", []):
            return {"ok": False, "message": "您不是该群成员"}

        for m in new_members or []:
            if not isinstance(m, str):
                continue
            name = m.strip()
            if not name or name in g["members"]:
                continue
            g["members"].append(name)
            added.append(name)

        if added:
            _save_social(social)
            sys_msg = f"{inviter} 邀请 {', '.join(added)} 加入了群聊"
    # 出锁后再写消息，避免锁序反转（死锁）
    if sys_msg:
        save_message(chat_type="group", sender="系统", target=group_id, content=sys_msg, msg_type="system")
    return {"ok": True, "message": f"已成功邀请 {len(added)} 位好友", "added": added}


def leave_group(group_id: str, username: str) -> dict:
    """退出群聊。如果是群主退出且还有成员，自动移交群主；无成员则解散群。"""
    with _social_lock:
        social = _load_social()
        g = social.setdefault("groups", {}).get(group_id)
        if not g:
            return {"ok": False, "message": "群聊不存在"}
        members = g.get("members", [])
        if username not in members:
            return {"ok": False, "message": "您不是该群成员"}

        members.remove(username)
        if username in g.get("member_settings", {}):
            del g["member_settings"][username]

        if not members:
            # 无成员，解散删除群及消息文件
            del social["groups"][group_id]
            _save_social(social)
            try:
                fp = _get_group_filepath(group_id)
                if fp.exists():
                    os.remove(fp)
            except Exception:
                pass
            return {"ok": True, "message": "群聊已解散"}

        # 若是群主离开，移交给第一个成员
        if g.get("owner") == username:
            g["owner"] = members[0]
            new_owner_msg = f"，群主已自动转交给 {g['owner']}"
        else:
            new_owner_msg = ""

        _save_social(social)

    save_message(
        chat_type="group",
        sender="系统",
        target=group_id,
        content=f"{username} 退出了群聊{new_owner_msg}",
        msg_type="system"
    )
    return {"ok": True, "message": "已退出群聊"}


# =========================================================================
# 会话已读位置（用于"新消息"标识与未读计数，跨会话/刷新持久化）
# =========================================================================
def get_read_positions(username: str) -> dict:
    """返回某用户各会话的已读时间戳：{conversation_key: last_read_time}。

    conversation_key 取 target（好友用户名 / 群 id）或 "bot"。
    """
    with _social_lock:
        social = _load_social()
    return dict(social.get("read_positions", {}).get(username, {}))


def set_read_position(username: str, key: str, ts: float) -> dict:
    """把某会话的已读位置推进到 ts（只前进不后退）。"""
    if not username or not key:
        return {"ok": False, "message": "参数缺失"}
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return {"ok": False, "message": "时间戳无效"}
    with _social_lock:
        social = _load_social()
        positions = social.setdefault("read_positions", {}).setdefault(username, {})
        if ts > (positions.get(key) or 0):
            positions[key] = ts
            _save_social(social)
    return {"ok": True, "key": key, "last_read": positions.get(key, ts)}


# =========================================================================
# 机器人会话设置（置顶/免打扰）
# =========================================================================
def get_bot_settings(username: str) -> dict:
    with _social_lock:
        social = _load_social()
    return social.get("user_settings", {}).get(username, {}).get("bot", {"pinned": True, "muted": False})


def set_bot_settings(username: str, pinned: bool = None, muted: bool = None) -> dict:
    with _social_lock:
        social = _load_social()
        u_settings = social.setdefault("user_settings", {}).setdefault(username, {})
        b_settings = u_settings.setdefault("bot", {"pinned": True, "muted": False})
        if pinned is not None:
            b_settings["pinned"] = bool(pinned)
        if muted is not None:
            b_settings["muted"] = bool(muted)
        _save_social(social)
    return {"ok": True, "settings": b_settings}


# =========================================================================
# 消息发送、获取与撤回
# =========================================================================
def save_message(chat_type: str, sender: str, target: str, content: str = "", image: str = "", msg_type: str = "text", quote: dict = None, sticker_name: str = "") -> dict:
    """保存单聊/群聊消息，支持消息引用（quote）与表情包名（sticker_name）。"""
    msg_id = f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    now = time.time()
    msg_obj = {
        "id": msg_id,
        "chat_type": chat_type,
        "sender": sender,
        "target": target,
        "content": content or "",
        "image": image or "",
        "mtype": msg_type,
        "time": now,
        "recalled": False,
    }
    # 表情包名（取自文件名）随消息持久化，让接收端无需解析 URL 也能显示名称
    if sticker_name and msg_type == "sticker":
        msg_obj["sticker_name"] = str(sticker_name)
    if quote and isinstance(quote, dict):
        msg_obj["quote"] = {
            "id": quote.get("id", ""),
            "sender": quote.get("sender", ""),
            "content": quote.get("content", ""),
            "mtype": quote.get("mtype", "text"),
            "image": quote.get("image", ""),
        }

    if chat_type == "user":
        fp = _get_dm_filepath(sender, target)
    elif chat_type == "group":
        fp = _get_group_filepath(target)
    else:
        return msg_obj

    with _msg_lock:
        msgs = _load_messages(fp)
        msgs.append(msg_obj)
        # 限制单个会话最多保留 2000 条
        if len(msgs) > 2000:
            msgs = msgs[-2000:]
        _save_messages(fp, msgs)

    return msg_obj


def get_messages(chat_type: str, user1: str, target: str, before: float = None, limit: int = 30) -> tuple:
    """分页拉取消息。返回 (list, has_more)。非群成员不得读取群历史。"""
    if chat_type == "user":
        fp = _get_dm_filepath(user1, target)
    elif chat_type == "group":
        if not _is_group_member(target, user1):
            return [], False   # 非群成员：不泄露群历史
        fp = _get_group_filepath(target)
    else:
        return [], False

    with _msg_lock:
        all_msgs = _load_messages(fp)

    if before is not None:
        filtered = [m for m in all_msgs if m.get("time", 0) < before]
    else:
        filtered = all_msgs

    has_more = len(filtered) > limit
    page = filtered[-limit:] if limit > 0 else filtered
    return page, has_more


def recall_message(chat_type: str, username: str, target: str, msg_id: str) -> dict:
    """撤回消息（对标QQ撤回功能：仅本人可撤回，撤回后标记 recalled=True）。"""
    if chat_type == "user":
        fp = _get_dm_filepath(username, target)
    elif chat_type == "group":
        if not _is_group_member(target, username):
            return {"ok": False, "message": "您不是该群成员"}
        fp = _get_group_filepath(target)
    else:
        return {"ok": False, "message": "不支持撤回该类型消息"}

    with _msg_lock:
        msgs = _load_messages(fp)
        target_msg = next((m for m in msgs if m.get("id") == msg_id), None)
        if not target_msg:
            return {"ok": False, "message": "消息不存在"}

        # 检查是否为本人发送（群聊中群主亦可管理）
        if target_msg.get("sender") != username:
            if chat_type == "group":
                with _social_lock:
                    g = _load_social().get("groups", {}).get(target, {})
                    is_owner = (g.get("owner") == username)
                if not is_owner:
                    return {"ok": False, "message": "只能撤回自己发送的消息"}
            else:
                return {"ok": False, "message": "只能撤回自己发送的消息"}

        # 检查撤回时限（对标 QQ：2 分钟内可撤回）
        msg_time = target_msg.get("time", 0)
        if time.time() - msg_time > 120:
            return {"ok": False, "message": "已超过 2 分钟，无法撤回该消息"}

        if target_msg.get("recalled"):
            return {"ok": False, "message": "该消息已经撤回"}

        target_msg["recalled"] = True
        target_msg["content"] = "此消息已撤回"
        target_msg["image"] = ""
        _save_messages(fp, msgs)

    return {"ok": True, "message": "撤回成功", "msg_id": msg_id, "recalled_by": username}


# =========================================================================
# 会话列表聚合（对标 QQ 消息会话首页）
# =========================================================================
def get_conversations(username: str, all_users_dict: dict, live_status_fn, bot_last_msg: dict = None,
                      bot_unread: int = 0) -> list:
    """返回聚合会话列表：包括辉夜机器人、好友单聊、群聊。

    每个会话附带 unread 字段（未读数），依据服务端持久化的已读位置
    （social.json 的 read_positions）计算，跨刷新/重登保持一致。
    bot_unread：由调用方（主进程读辉夜历史）算好的未读数。
    """
    with _social_lock:
        social = _load_social()
    read_positions = social.get("read_positions", {}).get(username, {})

    def _count_unread(msgs, key):
        """统计晚于已读位置、且非本人发送的消息条数。"""
        if not msgs:
            return 0
        last_read = read_positions.get(key) or 0
        n = 0
        for m in msgs:
            t = m.get("time") or 0
            if t > last_read and m.get("sender") != username:
                n += 1
        return n

    conversations = []

    # 1. 辉夜机器人会话（默认置顶）
    bot_settings = social.get("user_settings", {}).get(username, {}).get("bot", {"pinned": True, "muted": False})
    conversations.append({
        "id": "bot",
        "chat_type": "bot",
        "target": "bot",
        "name": "辉夜",
        "avatar_text": "辉",
        "role": "ai",
        "is_online": True,
        "pinned": bool(bot_settings.get("pinned", True)),
        "muted": bool(bot_settings.get("muted", False)),
        "unread": int(bot_unread or 0),
        "last_message": bot_last_msg or {"content": "记忆认知对话 · 随时开始", "time": time.time(), "sender": "辉夜"},
    })

    # 2. 好友私聊会话
    my_friends = social.get("friends", {}).get(username, {})
    for f_uname, f_info in my_friends.items():
        fp = _get_dm_filepath(username, f_uname)
        last_m = None
        msgs = []
        if fp.exists():
            with _msg_lock:
                msgs = _load_messages(fp)
            if msgs:
                last_m = msgs[-1]

        urec = all_users_dict.get(f_uname, {})
        live = live_status_fn(f_uname) if live_status_fn else {}
        display_name = f_info.get("alias") or f_uname
        conversations.append({
            "id": f"user_{f_uname}",
            "chat_type": "user",
            "target": f_uname,
            "name": display_name,
            "avatar_text": (f_uname[:1] or "?").upper(),
            "role": urec.get("role", "student"),
            "user_id": urec.get("user_id", ""),
            "is_online": bool(live.get("is_online", False)),
            "pinned": bool(f_info.get("pinned", False)),
            "muted": bool(f_info.get("muted", False)),
            "unread": _count_unread(msgs, f_uname),
            "last_message": last_m or {"content": "暂无消息", "time": f_info.get("created_at", 0), "sender": ""},
        })

    # 3. 群聊会话
    groups = social.get("groups", {})
    for gid, g in groups.items():
        if username in g.get("members", []):
            fp = _get_group_filepath(gid)
            last_m = None
            msgs = []
            if fp.exists():
                with _msg_lock:
                    msgs = _load_messages(fp)
                if msgs:
                    last_m = msgs[-1]

            m_settings = g.get("member_settings", {}).get(username, {})
            conversations.append({
                "id": f"group_{gid}",
                "chat_type": "group",
                "target": gid,
                "name": g.get("name", "群聊"),
                "avatar_text": "群",
                "role": "group",
                "member_count": len(g.get("members", [])),
                "is_online": True,
                "pinned": bool(m_settings.get("pinned", False)),
                "muted": bool(m_settings.get("muted", False)),
                "unread": _count_unread(msgs, gid),
                "last_message": last_m or {"content": "群聊已建立", "time": g.get("created_at", 0), "sender": ""},
            })

    # 排序：置顶在前，其余按最后一条消息的更新时间降序排列
    def _sort_key(c):
        pinned = 1 if c.get("pinned") else 0
        t = c.get("last_message", {}).get("time") or 0
        return (pinned, t)

    conversations.sort(key=_sort_key, reverse=True)
    return conversations
