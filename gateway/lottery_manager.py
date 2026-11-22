# gateway/lottery_manager.py —— 群聊抽奖（抽取 / 骰子）状态管理
#
# 每个群同时只保留一局抽奖：进行中时禁止再发起，结束后保留结果供查看，
# 下一次发起会直接替换旧结果。状态持久化到 gateway/lottery.json，
# 服务重启后仍可继续；长时间无人操作的历史房间会自动过期清理。
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
GATEWAY_DIR = PROJECT_DIR / "gateway"
LOTTERY_FILE = GATEWAY_DIR / "lottery.json"

ROLL_COOLDOWN_SECONDS = 3.0   # 骰子模式：同一成员两次投掷的最小间隔
DICE_MIN = 1
DICE_MAX = 6
ROOM_IDLE_TTL_SECONDS = 12 * 3600   # 超过 12 小时无操作视为过期
# 进行中的房间长时间无人操作（发起者掉线/忘记结束）后，允许群友直接开启新一局，
# 避免一局被遗忘的抽奖把群聊功能锁死太久。
ROOM_STALE_REPLACE_SECONDS = 10 * 60
MAX_PARTICIPANTS = 200

MODE_DRAW = "draw"
MODE_DICE = "dice"
VALID_MODES = (MODE_DRAW, MODE_DICE)

STATUS_OPEN = "open"          # 抽取模式：等待抽取
STATUS_ROLLING = "rolling"    # 骰子模式：等待投掷
STATUS_FINISHED = "finished"
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_ROLLING)

_lock = threading.Lock()


def _empty_store() -> dict:
    return {"rooms": {}}


def _ensure_dirs():
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)


def _load_store() -> dict:
    _ensure_dirs()
    if not LOTTERY_FILE.exists():
        return _empty_store()
    try:
        with open(LOTTERY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_store()
        rooms = data.get("rooms")
        if not isinstance(rooms, dict):
            data["rooms"] = {}
        return data
    except Exception:
        return _empty_store()


def _save_store(store: dict):
    _ensure_dirs()
    tmp = LOTTERY_FILE.with_suffix(LOTTERY_FILE.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LOTTERY_FILE)


def _cleanup(store: dict, now: float):
    """清理长时间无操作的房间，避免持久化文件无限增长。"""
    rooms = store.get("rooms", {})
    expired = [gid for gid, room in rooms.items()
               if now - float(room.get("updated_at") or 0) > ROOM_IDLE_TTL_SECONDS]
    for gid in expired:
        rooms.pop(gid, None)


def _new_room_id() -> str:
    return f"lot_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"


def _roll_record(room: dict, username: str):
    return (room.get("rolls") or {}).get(username)


def _public(room: dict, group_members=None) -> dict:
    """构造可下发给任意群成员的房间视图（不包含与查看者绑定的字段）。"""
    now = time.time()
    members = set(group_members) if group_members is not None else None
    participants = []
    for name in room.get("participants", []):
        rec = _roll_record(room, name) or {}
        participants.append({
            "username": name,
            "value": rec.get("value"),
            "rolls": int(rec.get("count") or 0),
            "last_roll": rec.get("last"),
            "in_group": (name in members) if members is not None else True,
        })

    rolled = [p for p in participants if p.get("value") is not None and p.get("in_group", True)]
    top, low = [], []
    if rolled:
        max_v = max(p["value"] for p in rolled)
        min_v = min(p["value"] for p in rolled)
        top = [p["username"] for p in rolled if p["value"] == max_v]
        # 全场同分时不再标注最低分，避免同一人同时绿红高亮
        if max_v != min_v:
            low = [p["username"] for p in rolled if p["value"] == min_v]

    return {
        "id": room.get("id", ""),
        "group_id": room.get("group_id", ""),
        "creator": room.get("creator", ""),
        "creator_in_group": ((room.get("creator") in members) if members is not None else True),
        "mode": room.get("mode", MODE_DRAW),
        "status": room.get("status", STATUS_FINISHED),
        "participants": participants,
        "participant_count": len(participants),
        "winner": room.get("winner") or "",
        "winner_value": room.get("winner_value"),
        "top": top,
        "low": low,
        "created_at": room.get("created_at"),
        "updated_at": room.get("updated_at"),
        "finished_at": room.get("finished_at"),
        "server_now": now,
        "cooldown_seconds": ROLL_COOLDOWN_SECONDS,
        "dice_min": DICE_MIN,
        "dice_max": DICE_MAX,
    }


def get_lottery(group_id: str, group_members=None) -> dict:
    """返回该群当前抽奖视图；没有则返回 None。"""
    if not group_id:
        return None
    with _lock:
        store = _load_store()
        _cleanup(store, time.time())
        room = store.get("rooms", {}).get(group_id)
        if not room:
            return None
        return _public(room, group_members)


def create_lottery(group_id: str, creator: str, mode: str, invited, group_members) -> dict:
    """发起一局抽奖。invited 中的成员必须仍在该群内，创建者自动加入。"""
    if not group_id or not creator:
        return {"ok": False, "message": "参数缺失"}
    if mode not in VALID_MODES:
        return {"ok": False, "message": "不支持的抽奖模式"}
    members = list(group_members or [])
    if creator not in members:
        return {"ok": False, "message": "您不是该群成员"}

    participant_set = [creator]
    for name in invited or []:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name and name in members and name not in participant_set:
            participant_set.append(name)
    if len(participant_set) > MAX_PARTICIPANTS:
        participant_set = participant_set[:MAX_PARTICIPANTS]

    now = time.time()
    with _lock:
        store = _load_store()
        _cleanup(store, now)
        existing = store.get("rooms", {}).get(group_id)
        if (existing and existing.get("status") in ACTIVE_STATUSES
                and existing.get("creator") in members
                and now - float(existing.get("updated_at") or 0) < ROOM_STALE_REPLACE_SECONDS):
            return {
                "ok": False,
                "message": "本群已有进行中的抽奖，请先结束或取消",
                "lottery": _public(existing, members),
            }

        room = {
            "id": _new_room_id(),
            "group_id": group_id,
            "creator": creator,
            "mode": mode,
            "status": STATUS_OPEN if mode == MODE_DRAW else STATUS_ROLLING,
            "participants": participant_set,
            "rolls": {},
            "winner": "",
            "winner_value": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        store.setdefault("rooms", {})[group_id] = room
        _save_store(store)
        return {"ok": True, "lottery": _public(room, members)}


def invite_members(group_id: str, actor: str, invited, group_members) -> dict:
    """邀请群友加入当前抽奖（仅发起者可操作，仅进行中的抽奖可邀请）。"""
    members = list(group_members or [])
    if actor not in members:
        return {"ok": False, "message": "您不是该群成员"}

    now = time.time()
    with _lock:
        store = _load_store()
        _cleanup(store, now)
        room = store.get("rooms", {}).get(group_id)
        if not room:
            return {"ok": False, "message": "当前没有进行中的抽奖"}
        if room.get("creator") != actor:
            return {"ok": False, "message": "只有发起者可以邀请成员"}
        if room.get("status") not in ACTIVE_STATUSES:
            return {"ok": False, "message": "本局抽奖已结束", "lottery": _public(room, members)}

        added = []
        participants = room.setdefault("participants", [])
        for name in invited or []:
            if not isinstance(name, str):
                continue
            name = name.strip()
            if not name or name not in members or name in participants:
                continue
            if len(participants) >= MAX_PARTICIPANTS:
                break
            participants.append(name)
            added.append(name)

        if added:
            room["updated_at"] = now
            _save_store(store)
        return {
            "ok": True,
            "added": added,
            "message": (f"已邀请 {len(added)} 位群友加入" if added else "没有新的成员需要加入"),
            "lottery": _public(room, members),
        }


def roll_dice(group_id: str, username: str, group_members) -> dict:
    """骰子模式投掷一次：每人 3 秒冷却，取最近一次点数作为当前点数。"""
    members = list(group_members or [])
    if username not in members:
        return {"ok": False, "message": "您不是该群成员"}

    now = time.time()
    with _lock:
        store = _load_store()
        _cleanup(store, now)
        room = store.get("rooms", {}).get(group_id)
        if not room:
            return {"ok": False, "message": "当前没有进行中的抽奖"}
        if room.get("mode") != MODE_DICE:
            return {"ok": False, "message": "本局不是骰子模式"}
        if room.get("status") != STATUS_ROLLING:
            return {"ok": False, "message": "本局投掷已结束", "lottery": _public(room, members)}
        if username not in (room.get("participants") or []):
            return {"ok": False, "message": "您尚未加入本局抽奖"}

        rolls = room.setdefault("rolls", {})
        rec = rolls.get(username) or {}
        last = float(rec.get("last") or 0)
        elapsed = now - last
        if last and elapsed < ROLL_COOLDOWN_SECONDS:
            return {
                "ok": False,
                "code": "cooldown",
                "retry_after": round(ROLL_COOLDOWN_SECONDS - elapsed, 2),
                "message": "投掷冷却中，请稍后再试",
                "lottery": _public(room, members),
            }

        value = secrets.randbelow(DICE_MAX - DICE_MIN + 1) + DICE_MIN
        rolls[username] = {
            "value": value,
            "count": int(rec.get("count") or 0) + 1,
            "last": now,
        }
        room["updated_at"] = now
        _save_store(store)
        return {
            "ok": True,
            "value": value,
            "lottery": _public(room, members),
        }


def draw_winner(group_id: str, username: str, group_members) -> dict:
    """抽取模式：从仍在群内的已加入成员中随机抽出一位。"""
    members = list(group_members or [])
    if username not in members:
        return {"ok": False, "message": "您不是该群成员"}

    now = time.time()
    with _lock:
        store = _load_store()
        _cleanup(store, now)
        room = store.get("rooms", {}).get(group_id)
        if not room:
            return {"ok": False, "message": "当前没有进行中的抽奖"}
        if room.get("mode") != MODE_DRAW:
            return {"ok": False, "message": "本局不是抽取模式"}
        if room.get("status") != STATUS_OPEN:
            return {"ok": False, "message": "本局已经抽奖过了", "lottery": _public(room, members)}
        if room.get("creator") != username:
            return {"ok": False, "message": "只有发起者可以开奖"}
        if username not in (room.get("participants") or []):
            return {"ok": False, "message": "您尚未加入本局抽奖"}

        eligible = [p for p in room.get("participants", []) if p in members]
        if not eligible:
            return {"ok": False, "message": "当前没有可抽取的成员"}
        winner = secrets.choice(eligible)
        room["status"] = STATUS_FINISHED
        room["winner"] = winner
        room["winner_value"] = None
        room["finished_at"] = now
        room["updated_at"] = now
        _save_store(store)
        return {"ok": True, "winner": winner, "lottery": _public(room, members)}


def finish_dice(group_id: str, actor: str, group_members) -> dict:
    """结束骰子模式：冻结当前点数，最高分（允许并列）作为本局结果。"""
    members = list(group_members or [])
    if actor not in members:
        return {"ok": False, "message": "您不是该群成员"}

    now = time.time()
    with _lock:
        store = _load_store()
        _cleanup(store, now)
        room = store.get("rooms", {}).get(group_id)
        if not room:
            return {"ok": False, "message": "当前没有进行中的抽奖"}
        if room.get("creator") != actor and room.get("creator") in members:
            return {"ok": False, "message": "只有发起者可以结束抽奖"}
        if room.get("mode") != MODE_DICE:
            return {"ok": False, "message": "本局不是骰子模式"}
        if room.get("status") != STATUS_ROLLING:
            return {"ok": False, "message": "本局已经结束", "lottery": _public(room, members)}

        rolled = []
        for name in room.get("participants", []):
            if name not in members:
                continue   # 已退群成员不参与最终结算
            rec = _roll_record(room, name) or {}
            if rec.get("value") is not None:
                rolled.append((name, rec.get("value")))
        winner_value = max((v for _, v in rolled), default=None)

        room["status"] = STATUS_FINISHED
        # 骰子模式不随机指定单一赢家：并列最高由前端按 top 列表共同展示
        room["winner"] = ""
        room["winner_value"] = winner_value
        room["finished_at"] = now
        room["updated_at"] = now
        _save_store(store)
        return {"ok": True, "lottery": _public(room, members)}


def cancel_lottery(group_id: str, actor: str, group_members) -> dict:
    """取消当前抽奖（仅发起者），直接移除房间。"""
    members = list(group_members or [])
    if actor not in members:
        return {"ok": False, "message": "您不是该群成员"}

    with _lock:
        store = _load_store()
        room = store.get("rooms", {}).get(group_id)
        if not room:
            return {"ok": False, "message": "当前没有进行中的抽奖"}
        if room.get("creator") != actor and room.get("creator") in members:
            return {"ok": False, "message": "只有发起者可以取消抽奖"}
        store["rooms"].pop(group_id, None)
        _save_store(store)
        return {"ok": True, "message": "本局抽奖已取消"}
