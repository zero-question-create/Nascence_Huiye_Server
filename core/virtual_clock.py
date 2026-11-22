# core/virtual_clock.py
import time         # 主要运用：获取当前时间戳
import os           # 主要运用：确保文件存在及操作安全
import threading    # 主要运用：发散线程
import random
import datetime

from core import runtime_paths

STATE_FILE = "data/test/clock_state.txt"     # 持久化保存路径


def _refresh_paths():
    global STATE_FILE
    STATE_FILE = runtime_paths.file("clock_state.txt")


runtime_paths.register(_refresh_paths)


def _parse_hhmm(value: str, default_min: int = 0) -> int:
    """把 "HH:MM" 解析为从 0 点起的分钟数；解析失败返回 default_min。"""
    try:
        parts = str(value).strip().split(":")
        h = int(parts[0]); m = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return default_min


class VirtualClock:
    def __init__(self, speed=1.0):
        self.session_start = time.time()    # 先给临时值，避免 self.now() 报错
        self.qq_mode = False                # qq模式开关，先给临时值，避免 self.now() 报错
        self._qq_real_offset = None              # qq模式时间基准
        self._last_save_real_time = None    # 上次保存时的真实时间戳
        saved = self._load_state()
        if saved is not None:
            self.speed = saved['speed']
            self.total_runtime = saved['total_runtime']
            total_virtual = saved['total_virtual']
            self._session_virtual_base = total_virtual - self.total_runtime * self.speed
            # 恢复状态机字段（如果存在，否则使用默认值）
            self._sleep_done_today = saved.get('sleep_done_today', False)
            self._last_sleep_day = saved.get('last_sleep_day', -1)
            self.state = saved.get('state', 'interactive')
            self.last_input_time = saved.get('last_input_time', self.now())
            self.qq_mode = saved.get('qq_mode', False)
            self._qq_real_offset = saved.get('_qq_real_offset', None)
        else:
            self.speed = speed
            self.total_runtime = 0.0
            self._session_virtual_base = 0.0
            self._sleep_done_today = False
            self._last_sleep_day = -1
            self.state = 'interactive'
            self.last_input_time = self.now()

        self.session_start = time.time()
        self.drift_threshold = 15 * 60
        # 宵禁（睡眠）时间窗口：从 config 读取（HH:MM 字符串），支持跨天（如 23:00–07:00）
        self._apply_sleep_config()
        self._last_maintenance_day = -1   # 上次执行每日睡眠维护的真实日期序号（避免一天重复执行）
        self._drift_thread = None
        self._stop_drift = threading.Event()

    def _apply_sleep_config(self):
        """从全局 config 读取宵禁开始/结束时间（HH:MM），解析失败回退 23:00–07:00。"""
        try:
            from config.api_config import config
            start = str(config.get("sleep_start", "23:00") or "23:00")
            end = str(config.get("sleep_end", "07:00") or "07:00")
        except Exception:
            start, end = "23:00", "07:00"
        self.sleep_start_min, self.sleep_end_min = _parse_hhmm(start), _parse_hhmm(end)

    def reload_sleep_config(self):
        """运行中重读宵禁配置（控制面板保存后调用，无需重启）。"""
        self._apply_sleep_config()

    def sleep_disabled(self) -> bool:
        """起止时间相同 → 不宵禁（认知循环不暂停），但仍会在入睡点执行睡眠维护。"""
        return self.sleep_start_min == self.sleep_end_min

    def due_daily_sleep_maintenance(self, when: datetime.datetime = None) -> bool:
        """判断是否到了"入睡时间点"需要执行每日睡眠维护（幂等：同一天只返回一次 True）。

        用于"不宵禁（起止相同）"场景：不暂停认知循环，但仍要在入睡点做一次
        sleep_cleanup 睡眠巩固（链接剪枝/字词整理/衰变清理/全量持久化）。
        以真实日期为界，跨天窗口用当前 24h 内"已过入睡点"判定。
        """
        if when is None:
            when = datetime.datetime.now()
        cur = when.hour * 60 + when.minute
        day = when.toordinal()  # 真实日期序号
        if self._last_maintenance_day == day:
            return False
        # 到达/越过今天的入睡时间点，或当前正处于睡眠窗口内（跨天凌晨启动可补做），
        # 均视为应执行当天一次的睡眠维护。
        if cur >= self.sleep_start_min or self.in_sleep_window(when):
            self._last_maintenance_day = day
            return True
        return False

    def run_sleep_maintenance(self):
        """执行一次睡眠维护工作（记忆巩固），并保存时钟状态。不改变交互/睡眠暂停状态。"""
        try:
            from utils.persistence import sleep_cleanup
            sleep_cleanup()
        except Exception as e:
            try:
                from core.cognition import append_log as _al
                _al(f"[睡眠维护] 执行失败: {e}")
            except Exception:
                pass
        try:
            self.save_state()
        except Exception:
            pass

    def enable_qq_mode(self):
        self.qq_mode = True
        self.speed = 1.0
        if not self._qq_real_offset:
            self._qq_real_offset = time.time()
            self.save_state()
            self._compensate_downtime()
        else:
            self._session_virtual_base = time.time() - self._qq_real_offset - self.get_real_runtime()
            self.save_state()

    def to_real_time(self, virtual_timestamp: float) -> float:
        """将虚拟时间戳转换为真实Unix时间戳（仅QQ模式有效）"""
        if self._qq_real_offset is not None:
            return virtual_timestamp + self._qq_real_offset
        # 非QQ模式，如果虚拟时间已经是真实时间戳（例如基准为 time.time()），直接返回
        return virtual_timestamp

    def to_virtual_time(self, real_timestamp: float) -> float:
        """将真实Unix时间戳转换为虚拟时间戳（to_real_time 的逆运算）。

        用于把前端/外部传入的真实时间游标还原成存储层使用的虚拟时间，
        例如历史分页的 before 游标（历史记录里存的是虚拟时间）。
        """
        if self._qq_real_offset is not None:
            return real_timestamp - self._qq_real_offset
        return real_timestamp

    def now(self) -> float:
        """返回当前绝对虚拟时间（从基准起算，总真实时间按当前速度缩放）"""
        # 绝对虚拟时间 = 全局基准 + 累计真实运行时间 × 当前速度
        return self._session_virtual_base + self.get_real_runtime() * self.speed

    def set_speed(self, speed: float):
        """更改时间倍速，返回更新后的速度值"""
        # 结算本段真实运行时间，计入总量（冻结此刻之前的真实耗时）
        self.total_runtime += time.time() - self.session_start
        self.session_start = time.time()        # 重置会话起点

        # 计算当前虚拟时间（用旧速度，保证变速瞬间虚拟时间连续）
        virt_now = self._session_virtual_base + self.total_runtime * self.speed

        # 更新速度
        self.speed = speed

        # 逆推新基准，使得“总真实时间 × 新速度 + 新基准 = virt_now”
        # 相当于把历史运行时间无缝拼接到新速度下
        self._session_virtual_base = virt_now - self.total_runtime * self.speed

        return self.speed

    def get_real_runtime(self) -> float:
        """返回从首次启动开始累计的真实运行时间（秒）"""
        # 累计时间 = 历史已结算时间 + 本段未结算时间
        return self.total_runtime + (time.time() - self.session_start)

    @property
    def sleep_start_hour(self) -> int:
        """睡眠开始小时（0-23），由分钟数推导，供旧接口/困倦判断使用。"""
        return (self.sleep_start_min // 60) % 24

    @property
    def sleep_end_hour(self) -> int:
        """睡眠结束（起床）小时（0-23），由分钟数推导。"""
        return (self.sleep_end_min // 60) % 24

    def in_sleep_window(self, when: datetime.datetime = None) -> bool:
        """
        判断给定真实时刻（默认当前）是否处于睡眠窗口。
        窗口由 config 的 sleep_start / sleep_end（HH:MM）定义，支持跨天。
        """
        if when is None:
            when = datetime.datetime.now()
        cur = when.hour * 60 + when.minute
        start = self.sleep_start_min
        end = self.sleep_end_min
        if start == end:
            return False  # 起止相同视为不宵禁
        if start < end:
            return start <= cur < end
        # 跨午夜窗口（如 23:00–07:00）
        return cur >= start or cur < end

    def save_state(self):
        self.total_runtime = self.get_real_runtime()
        self.session_start = time.time()
        total_virtual = self._session_virtual_base + self.total_runtime * self.speed
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            f.write(f"{self.speed}\n{total_virtual}\n{self.total_runtime}\n")
            f.write(f"{self._sleep_done_today}\n{self._last_sleep_day}\n")
            f.write(f"{self.state}\n{self.last_input_time}\n")
            f.write(f"{self.qq_mode}\n")
            offset_str = str(self._qq_real_offset) if self._qq_real_offset is not None else ""
            f.write(f"{offset_str}\n")
            f.write(f"{time.time()}\n")   # 第10行：保存时的真实时间

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                lines = f.read().strip().split('\n')
                if len(lines) >= 3:
                    try:
                        speed = float(lines[0])
                        total_virtual = float(lines[1])
                        total_runtime = float(lines[2])
                        sleep_done_today = False
                        last_sleep_day = -1
                        state = 'interactive'
                        last_input_time = 0.0
                        if len(lines) >= 7:
                            sleep_done_today = lines[3].strip() == 'True'
                            last_sleep_day = int(lines[4])
                            state = lines[5].strip()
                            last_input_time = float(lines[6])
                        # 读取QQ模式及偏移量（第8、9行）
                        qq_mode = False
                        qq_real_offset = None
                        if len(lines) >= 9:
                            qq_mode = lines[7].strip() == 'True'
                            offset_str = lines[8].strip()
                            if offset_str:
                                qq_real_offset = float(offset_str)
                        if len(lines) >= 10:
                            last_save_real = float(lines[9].strip())
                            self._last_save_real_time = last_save_real + 0.9

                        return {
                            'speed': speed,
                            'total_virtual': total_virtual,
                            'total_runtime': total_runtime,
                            'sleep_done_today': sleep_done_today,
                            'last_sleep_day': last_sleep_day,
                            'state': state,
                            'last_input_time': last_input_time,
                            'qq_mode': qq_mode,
                            '_qq_real_offset': qq_real_offset
                        }
                    except (ValueError, IndexError):
                        pass
        return None
    
    def _compensate_downtime(self):
        """QQ模式下，补偿停机期间的真实时间流逝"""
        if self.qq_mode and self._last_save_real_time is not None:
            elapsed_real = time.time() - self._last_save_real_time
            if elapsed_real > 0:
                self._session_virtual_base += elapsed_real
                # 同时也更新 last_input_time，避免因停机导致立即进入睡眠
                self.last_input_time = self.now()

    # === 状态机控制方法 ===
    def update_state(self):
        """每个认知周期调用，根据宵禁窗口与空闲时长决定状态转换。

        宵禁判定统一用真实时间（与 in_sleep_window 一致，支持跨天窗口）：
        - 进入睡眠窗口且本周期尚未入睡 → 入睡（执行一次睡眠巩固）；
        - 离开睡眠窗口（醒来）→ 清除"本周期已入睡"标记，允许下一个窗口再次入睡。
        这样跨午夜（如 23:00–07:00）不会被"自然日切换"误判为重复入睡。
        """
        virt_now = self.now()

        # 睡眠窗口判断（真实时间，支持跨天）
        in_window = self.in_sleep_window()

        if in_window and not self._sleep_done_today:
            self._enter_sleep()
            self._sleep_done_today = True
            return

        # 离开睡眠窗口：唤醒并清除本周期标记（下次进入窗口可再次入睡）
        if not in_window:
            if self.state == "sleeping":
                self._wake_up()
            self._sleep_done_today = False
            self._last_sleep_day = -1

        # 正常交互/发散逻辑（仅在非睡眠窗口内推进）
        if self.state == "interactive" and not in_window:
            if virt_now - self.last_input_time > self.drift_threshold:
                self._enter_drifting()
        # drifting 状态会一直保持，直到用户输入时通过 on_user_input 切换回 interactive

    def on_user_input(self):
        """收到用户输入时调用，重置空闲计时并切回交互状态"""
        self.last_input_time = self.now()
        if self.state != "interactive":
            self._enter_interactive()

    def _enter_interactive(self):
        self.state = "interactive"
        self._stop_drift.set()   # 停止发散线程

    def _enter_drifting(self):
        self.state = "drifting"
        self._stop_drift.clear()
        # 启动发散线程（如果尚未启动）
        if self._drift_thread is None or not self._drift_thread.is_alive():
            self._drift_thread = threading.Thread(target=self._drifting_loop, daemon=True)
            self._drift_thread.start()

    def _enter_sleep(self):
        self.state = "sleeping"
        self._stop_drift.set()
        self._sleep_done_today = True
        self._last_sleep_day = int(self.now() // 86400)
        # 持久化当前状态（包括睡眠标记）
        self.save_state()
        # 执行全量睡眠巩固
        from utils.persistence import sleep_cleanup
        sleep_cleanup()

    def _wake_up(self):
        self.state = "interactive"
        self.last_input_time = self.now()

# 全局虚拟时钟实例，默认 1 倍速
clock = VirtualClock(speed=1)