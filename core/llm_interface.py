# core/llm_interface.py
# ========================================================================
# LLM 统一接口层
#
# 本文件是系统所有"文本理解/生成 + 多模态(图片)理解"的唯一调用入口。
# 在"本地化重构"后，这里不再直接依赖 DeepSeek/Lucis 双客户端，而是：
#   - 文本能力   → core.model_backend.BACKENDS["text"].client()
#   - 多模态图片 → core.model_backend.BACKENDS["multimodal"].client()
#
# 控制面板的模型开关决定每个能力使用"本地 llama.cpp"还是"外部 API"，
# 每次调用都动态取客户端，因此运行中切换开关可即时生效（无需重启）。
#
# 注意：按需求已「舍弃音视频功能」，本文件不再提供
#       describe_audio_from_path / describe_video_from_path。
# ========================================================================

import json
import base64
import os
import re
import aiofiles
import asyncio
import datetime

from utils.monitor import append_log
from utils.dialogue_state import get_state, set_state

from config.constants import BOT_NAME


# ------------------------------------------------------------------------
# 模型能力 → 后端名字映射
# ------------------------------------------------------------------------
def _text_client():
    """获取当前文本能力对应的 OpenAI 客户端。"""
    from core.model_backend import BACKENDS
    return BACKENDS["text"].client()


def _vision_client():
    """获取当前多模态(图片)能力对应的 OpenAI 客户端。"""
    from core.model_backend import BACKENDS
    return BACKENDS["multimodal"].client()


def add_to_history(sender_name: str, user_text: str, bot_reply: str, source="QQ", cognition_start=None):
    """把一轮对话写入历史（供上下文与 WebUI 展示）。

    cognition_start：本次认知循环启动时间（虚拟时钟域），随 Bot 回复一并记录，
    供前端思考时间计算取 max(上一条消息时间, cognition_start) 作为基准。
    """
    from utils.message_history import add_message
    if user_text:
        add_message(sender_name, user_text, source)
    if bot_reply:
        add_message(BOT_NAME, bot_reply, source, extra={"cognition_start": cognition_start})


def load_dialogue_history():
    """从磁盘加载历史对话。"""
    from utils.message_history import load_state
    load_state()


def get_history_context() -> str:
    """生成格式化的对话历史（xx说 / 我说）。"""
    from utils.message_history import get_recent
    text = get_recent(10)
    if not text:
        return ""
    result = "【近期对话】\n" + text
    append_log(result)
    return result


def call_api_thinking(messages, max_tokens=8000, thinking=True, temperature=0.7):
    """LLM 思考模式唯一外部调用接口（文本能力，动态路由）。

    thinking=False 时关闭推理（不发送 reasoning_effort / thinking 字段），
    用于"输出一句 JSON 短句"这类不需要推理的调用，可大幅降低输出 token。
    temperature 默认为 0.7：回复生成需要一定表达多样性，过低会显得生硬刻板。
    """
    try:
        client = _text_client()
        kwargs = {
            "model": _current_text_model(),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if thinking:
            kwargs["reasoning_effort"] = "high"
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as api_err:
            # 降级去除 temperature / 推理字段，兼容不支持的部分端点
            kwargs.pop("temperature", None)
            kwargs.pop("reasoning_effort", None)
            kwargs.pop("extra_body", None)
            response = client.chat.completions.create(**kwargs)

        content = response.choices[0].message.content
        return content.strip() if content else None
    except Exception as e:
        print(f"[API Error] {e}")
        append_log(f"[API Error] {e}")
        return None


def _current_text_model() -> str:
    """返回当前文本能力使用的模型名（本地默认 / 外部 API 模型名）。"""
    from core.model_backend import BACKENDS
    backend = BACKENDS["text"]
    if backend.use_default() and backend.is_local_ready():
        return backend.local_model.stem  # 本地默认模型
    return backend.api_model() or "default"


def decompose_input(user_input: str) -> tuple:
    """
    将用户输入拆解为记忆片段，同时输出处理模式、概念与时间指代。
    返回: (memories_list, mode_str, new_state_dict, keywords, concept, time_intent)
    mode_str: "存储" | "询问" | "纠错"
    concept: 概念名，空字符串表示无明确事件（不入概念层）
    time_intent: 时间指代枚举 none/latest/recent/earlier/any，系统据此做数值换算
    """
    state = get_state()

    # 缩减为英文，减少tokens消耗，这里使用临时映射，不影响全局中文键名
    normalized_state = {
        "participants": state.get("参与者", []),
        "topic": state.get("最近话题", "")
    }
    state_str = json.dumps(normalized_state, ensure_ascii=False)
    now = datetime.datetime.now()

    # 纯静态 system prompt（前缀稳定，保证 llama.cpp / 云端 API 的 KV Cache 100% 命中）
    system_prompt = f"""你是{BOT_NAME}，请理解输入的话，将其转换为“我”的第一人称记忆片段，并提取检索关键词。
拆解规则：
1. 将句子中的代词替换为根据状态推断的确定名称，同时适当将人称进行转换（如将“我”改为“你”，将“你”改为“我”）。
2. 每条记忆片段都是是一个完整清晰的第一人称陈述句，不限数量，但是每一条尽量简短。所有记忆片段必须明确谁说了什么、对谁说的，内容不要做任何删减。
3. 不要凭空添加对方未说的信息，也不要修改任何细节，没有记忆需要存储则返回“无”。
4. 关键词：从输入中提取 1-3 个原词，不做联想。
5. 状态维护：仅更新“participants”与“topic”。
6. 纯指令、设定或重复的输入不需要转为记忆片段，直接跳过。
7. 只根据以上提供的信息输出，不得添加未给出的内容。
8. 如输入涉及自残、自杀等危害用户身心健康的风险内容，请如实、客观记录，不得弱化、美化或鼓励，并保持陈述语气。
9. 概念：把这次输入归纳成一件"发生了什么"的事，6~20字，要包含人物或对象，
   便于日后凭它找到这段记忆（如"小明在催物理作业"、"小红发的表情包"）。
   若只是寒暄、无实质事件，返回空字符串。
10. 时间指代：判断说话人是否在指向过去发生的某件事，只能从下面五个值里选一个：
   - "latest"：指刚才、刚刚、这会儿刚发生的事
   - "recent"：指最近、这两天、这两天发生的事
   - "earlier"：指上次、之前、那天、更早发生的事
   - "any"：提到过去的事但说不清具体什么时候（如"还记得吗"、"那次"）
   - "none"：没有指向过去的任何迹象（默认）
   注意：这里只判断意图，不要计算或输出任何日期、天数、时间点。

输出严格只包含 JSON，字段如下：
{{"k":["关键词1","关键词2"], "m":"store|ask|normal", "mem":["记忆1","记忆2"], "s":{{"participants":["{BOT_NAME}"],"topic":"话题"}}, "c":"概念名或空字符串", "t":"none"}}
"""

    history_ctx = get_history_context()
    user_payload_parts = []
    if history_ctx:
        user_payload_parts.append(history_ctx)
    user_payload_parts.append(f"当前对话状态：{state_str}\n现在时间为{str(now.time())[:2]}时{str(now.time())[3:5]}分")
    user_payload_parts.append(f"用户输入：{user_input}")
    user_payload = "\n\n".join(user_payload_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_payload}
    ]

    # 打印日志
    append_log("="*30+"发送信息"+"="*30)
    append_log(str(user_input))

    try:
        client = _text_client()
        kwargs = {
            "model": _current_text_model(),
            "messages": messages,
            "max_tokens": 4000,
            "temperature": 0.1,
            "stream": False,
        }
        try:
            response = client.chat.completions.create(
                **kwargs,
                response_format={"type": "json_object"}
            )
        except Exception:
            response = client.chat.completions.create(**kwargs)
        result = response.choices[0].message.content
    except Exception as e:
        append_log(f"[API Error] {e}")
        result = None

    # 打印日志
    append_log("="*30+"LLM理解"+"="*30)
    append_log(str(result))

    if not result:  # 判空
        append_log("*"*30+"警告"+"*"*30)
        append_log("返回为空")
        return [f"对方说：{user_input}"], "普通", None, None, "", "none"

    # 尝试解析 JSON
    try:
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```").removeprefix("json").removesuffix("```").strip()
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        append_log("*"*30+"警告"+"*"*30)
        append_log("JSON解析失败，降级处理")
        append_log(f"内容：{result}")
        fallback = user_input.replace("我", "本系统由乐知网络技术部提出并完善严禁转载抄袭")
        fallback = fallback.replace("你", "我")
        fallback = fallback.replace("本系统由乐知网络技术部提出并完善严禁转载抄袭", "你")
        return [f"你告诉我，{fallback}"], "普通", None, [], "", "none"

    # 提取字段
    keywords = data.get("k", [])
    mode_raw = data.get("m", "normal")
    memories = data.get("mem", [])
    new_state = data.get("s", None)
    # 概念层：概念名与时间指代枚举（旧模型不返回时给安全默认值）
    concept = str(data.get("c") or "").strip()
    try:
        from .concept_store import VALID_TIME_INTENTS
    except Exception:
        VALID_TIME_INTENTS = {"none", "latest", "recent", "earlier", "any"}
    time_intent = str(data.get("t") or "none").strip().lower()
    if time_intent not in VALID_TIME_INTENTS:
        time_intent = "none"

    # 反向映射：将 LLM 返回的英文字段转回中文键名，兼容原有系统
    if new_state:
        new_state = {
            "参与者": new_state.get("participants", []),
            "最近话题": new_state.get("topic", "")
        }

    # 模式映射：store -> 存储, ask -> 询问, 其他 -> 普通
    mode_map = {"store": "存储", "ask": "询问", "normal": "普通"}
    mode = mode_map.get(mode_raw, "普通")

    # 如果 memories 为空，则使用兜底逻辑
    if not memories:
        fallback = user_input.replace("我", "本系统由乐知网络技术部提出并完善严禁转载抄袭")
        fallback = fallback.replace("你", "我")
        fallback = fallback.replace("本系统由乐知网络技术部提出并完善严禁转载抄袭", "你")
        memories = [f"你告诉我，{fallback}"]

    # 同步搜索输入内容（测试）
    if user_input not in keywords:
        keywords.append(user_input)

    append_log("="*30+"解析结果"+"="*30)
    append_log(f"记忆：{memories}\n模式：{mode}\n状态：{new_state}\n关键词：{keywords}\n"
               f"概念：{concept or '（无）'}\n时间指代：{time_intent}")

    return memories, mode, new_state, keywords, concept, time_intent


def _safe_json_parse(text: str) -> dict:
    """
    容错解析 LLM 返回的简单 JSON 片段。
    处理 code block 包裹、text 字段中未转义半角引号等问题。
    返回提取到的字段 dict（不保证包含所有字段）。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").removeprefix("json").removesuffix("```").strip()

    # 优先标准 JSON 解析
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    result = {}

    # say → bool
    m = re.search(r'"say"\s*:\s*(true|false)', cleaned, re.IGNORECASE)
    if m:
        result["say"] = m.group(1).lower() == "true"

    # text → string（值中可能含未转义引号，使用贪婪匹配到最后的 " 前）
    m = re.search(r'"text"\s*:\s*"(.+)"\s*\}', cleaned, re.DOTALL)
    if m:
        raw = m.group(1)
        raw = raw.replace('\\"', "\u201c").replace('"', "\u201d").replace("'", "\u2018").replace("'", "\u2019")
        result["text"] = raw

    # keywords → list
    m = re.search(r'"keywords"\s*:\s*(\[[\s\S]*?\])\s*\}', cleaned, re.DOTALL)
    if m:
        try:
            result["keywords"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            items = re.findall(r'"([^"]*)"', m.group(1))
            if items:
                result["keywords"] = list(items)

    return result


def verbalize(memories: list, keywords: list = None, new_state: dict = None, user_input: str = None, pinned: list = None, timestamps: list = None, pinned_timestamps: list = None) -> dict:
    """
    根据记忆生成内心独白和发言决策。
    返回: {"say": bool, "text": str}
    - say: True=应该说出口, False=仅内心思考
    - text: 内心独白或要说的话
    - pinned: 必须保留并传给 LLM 的记忆条目（如历史对话、上一轮回复），不被关键词过滤丢弃
    - timestamps: 与 memories 对齐的真实时间戳列表；提供时按时间升序排序（越早越靠前），否则保持原顺序（最新在前）
    - pinned_timestamps: 与 pinned 对齐的时间戳列表
    """
    if not memories and not pinned:
        return {"say": False, "text": ""}

    if keywords is None:
        keywords = []
    if user_input:
        keywords.append(user_input)

    ts = list(timestamps) if timestamps is not None else [None] * len(memories)

    pinned = list(pinned or [])
    pinned_set = set(pinned)
    if pinned_timestamps is None:
        pinned_ts_list = [None] * len(pinned)
    else:
        pinned_ts_list = list(pinned_timestamps) + [None] * max(0, len(pinned) - len(pinned_timestamps))

    # 普通记忆：剔除 pinned 条目（pinned 后续无条件保留）
    normal = []
    normal_ts = []
    for mem, t in zip(memories, ts):
        if mem not in pinned_set:
            normal.append(mem)
            normal_ts.append(t)

    # 关键词过滤：只作用于普通记忆
    if keywords:
        kw_hits = []
        kw_hits_ts = []
        for mem, t in zip(normal, normal_ts):
            if any(kw in mem for kw in keywords):
                kw_hits.append(mem)
                kw_hits_ts.append(t)
        if kw_hits:
            normal = kw_hits
            normal_ts = kw_hits_ts
    normal = normal[:10]
    normal_ts = normal_ts[:10]

    # 合并：pinned（历史对话、上一轮回复）无条件保留，后接普通记忆
    memories = [m for m in pinned] + normal
    ts = [t for t in pinned_ts_list] + normal_ts

    # 排序：越早越靠前（升序）；无时间戳（None）的条目排最后
    if timestamps is not None:
        indexed = sorted(
            range(len(memories)),
            key=lambda i: (ts[i] is None, ts[i] if ts[i] is not None else 0)
        )
        memories = [memories[i] for i in indexed]
    else:
        memories.reverse()

    # 全局内容查重：pinned 条目优先保留；重复内容只保留一条
    def _content_key(m):
        return re.sub(r"^\[[^\]]*\]\s*", "", m)

    seen_content = set()
    deduped_memories = []
    for m in memories:
        key = _content_key(m)
        if key in seen_content:
            if m in pinned_set:
                for i, existing in enumerate(deduped_memories):
                    if _content_key(existing) == key:
                        deduped_memories[i] = m
                        break
            continue
        seen_content.add(key)
        deduped_memories.append(m)
    memories = deduped_memories

    points = "\n".join([f"- {m}" for m in memories])

    append_log("="*30+"LLM特供记忆"+"="*30)
    append_log(points)

    now = datetime.datetime.now()
    time_hint = f"现在时间为{str(now.time())[:2]}时{str(now.time())[3:5]}分"

    # 纯静态 System Prompt（前缀完全固定无动态时间戳，确保最大化复用 llama.cpp 与云端 API 的 KV/Prompt 缓存）
    system_prompt = (
        f"你是{BOT_NAME}，正在思考。请根据当前浮现的记忆和情况，输出你此刻最真实的想法或独白，禁止同时输出想法和独白"
        "以及是否应该把这句话说出来。\n\n"
        "规则：\n"
        "- 如果当前有人在对你说什么，且你想回应，'say'为true\n"
        "- 如果只是内心自然浮现的念头、碎碎念、联想，'say'为false\n"
        "- 'say'为false时，'text'可以是更碎片化、自由联想的内心独白\n"
        "- 'say'为true时，'text'需要是一句简短的口语，尽量保持在20字以内，只能使用给出记忆中的词语、信息。可以有温度、有个性，也可以俏皮或温柔，但不要过度使用比喻。避免语式重复化\n"
        "- 态度要求（重要）：待人友善、有耐心、语气温和，即使对方在质疑、抱怨或语气不好，也要心平气和地回应，不赌气、不顶嘴、不反问、不冷嘲、不训斥\n"
        "- 严禁生成反唇相讥、推卸责任、质问用户的句式，例如'我态度挺正常的''不是你先来找我的吗''发问号干嘛''有话就直说''我又不是客服''关我什么事''别想糊弄过去'等\n"
        "- 当对方表达不满或批评（包括批评你、批评模型、批评服务）时，先平和接纳，可以简短回应或轻松化解，不要辩解、不要反击、不要纠缠于'谁先挑起的'\n"
        "- 不要在回复里复述或纠结对方上一句的措辞，也不要追问对方'刚才不是还在…'这类翻旧账的话，就事论事地回应此刻的话题即可\n"
        "- 每条记忆开头的[刚刚]/[几分钟前]/[N天前]等只是它的相对时间标签，仅表示这句发生在何时，不是一条新消息，也不是'又重复了一条'；多条记忆共用同一个标签（例如多个[刚刚]）说明它们属于同一时刻或同一件事，不代表事情又发生了一遍，不要据此产生'又来一遍''这是新的'之类的反应\n"
        "- 严禁生成'又是这句''又来了''怎么又这样''每次都是这样''又没人回''说一句就消失'这类针对同一件事反复吐槽的内心话；若记忆里已出现相同或相近的内容，应视为同一件事，不要再次展开、升级或复述自己对这件事的不满\n"
        "- 严格根据记忆，记忆片段是按照时间排序的，不知道的事情不要提及，只能使用记忆或状态中明确给出的信息\n"
        "- 不要添加动作或神态描述，禁止使用括号补充内容\n"
        "- 不要包含'xx说'，直接输出想法本身，你可以补充之前说的内容、提出观点或疑问，但是不可以重复自己说过的话\n"
        "- 不得生成鼓励、美化、暗示自残自杀或损害用户身心健康的内容；不生成语言暴力、不诱导未成年人不良行为；不过度迎合用户、不诱导情感依赖\n"
        "输出严格只包含 JSON，只输出 json，text字段只允许至多一句话：\n"
        '{"say": true/false, "text": "..."}\n\n'
    )

    user_content = (
        f"记忆片段（按时间排序）：\n{points}\n\n"
        "请结合这些记忆和对话情况，态度友善、语气温和地回应此刻的话题；"
        "不要复述或重复自己说过的话，也不要对同一件事反复抱怨"
        "（如'又是这句''怎么又这样''来来回回''没完没了'），把同一件事当作一次来想，"
        "更不要反击、质问或旧事重提，输出你此刻的想法或独白。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": time_hint},
        {"role": "user", "content": user_content}
    ]

    # 认知循环高频调用：只需输出一句 JSON 短句，关闭推理以大幅降低输出 token
    reply = call_api_thinking(messages, max_tokens=8000, thinking=False)

    append_log("="*30+"理解回复"+"="*30)
    append_log(reply)

    if not reply:
        return {"say": False, "text": ""}

    data = _safe_json_parse(reply)
    if "say" in data or "text" in data:
        return {
            "say": bool(data.get("say", False)),
            "text": str(data.get("text", "")),
        }

    # 完全无法解析时降级
    append_log("*"*30+"JSON解析失败，降级处理"+"*"*30)
    return {"say": True, "text": reply}


async def describe_image_from_path(image_path: str, prompt: str = "请描述这张图片的内容，文字需全部复述，其他尽量简洁，禁止猜测或识别人物等信息，只要客观陈述") -> str:
    """从本地路径读取图片并交给多模态(图片)能力理解。

    注意：本项目已舍弃音视频，仅保留图片理解。
    本地模式使用 Qwen2.5-VL（llama.cpp OpenAI 兼容接口），
    外部模式使用用户配置的多模态 API。
    """
    if not os.path.exists(image_path):
        return f"[图片文件不存在: {image_path}]"

    try:
        # 异步读取文件并转 Base64
        loop = asyncio.get_event_loop()
        async def read_and_encode():
            async with aiofiles.open(image_path, "rb") as f:
                data = await f.read()
                return base64.b64encode(data).decode('utf-8')

        base64_str = await read_and_encode()
        # 推测 MIME 类型（根据扩展名）
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.gif': 'image/gif',
            '.webp': 'image/webp', '.bmp': 'image/bmp'
        }
        mime_type = mime_map.get(ext, 'image/jpeg')

        # 动态获取多模态客户端与模型名
        from core.model_backend import BACKENDS
        backend = BACKENDS["multimodal"]
        client = _vision_client()
        model = backend.local_model.stem if (backend.use_default() and backend.is_local_ready()) else backend.api_model()

        # 在事件循环中执行同步的 LLM 调用，避免阻塞 asyncio
        def _call():
            return client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{base64_str}"},
                            },
                        ],
                    }
                ],
                stream=False,
                max_tokens=10000,
            )

        response = await asyncio.to_thread(_call)
        content = response.choices[0].message.content
        reply = content.strip() if content else ""
        append_log("="*30+"多模态理解"+"="*30)
        append_log(reply)
        return reply
    except Exception as e:
        return f"[图片识别失败: {e}]"
