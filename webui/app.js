/* webui/app.js —— Nascence 辉夜 控制面板前端逻辑（标签卡风格 + 多用户） */
"use strict";

// ========== 全局状态 ==========
const state = {
  activeTab: "overview",
  botName: "辉夜",
  theme: "follow",
  selectedUser: "",
  userSub: "uov",
};
let allUsers = [];

// ========== 工具函数 ==========
async function api(url, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    window.location.reload();
    throw new Error("请先登录控制面板");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || data.message || `HTTP ${resp.status}`);
  return data;
}

function $(id) { return document.getElementById(id); }

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ========== 极简 Markdown 渲染器 ==========
// 支持：标题 / 加粗 / 斜体 / 删除线 / 行内代码 / 代码块 / 链接 / 无序列表 /
// 有序列表 / 引用 / 分割线。先整体转义 HTML 再生成标签，杜绝注入。
// 客户端（客户端广播弹窗）使用同一份实现，保持两端渲染一致。
function renderMarkdown(src) {
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  let text = String(src == null ? "" : src).replace(/\r\n?/g, "\n");
  const blocks = [];

  // 1. 抽出围栏代码块，先用占位符替换（避免内部被当作 Markdown 处理）
  text = text.replace(/```([\s\S]*?)```/g, (m, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/^\n/, ""))}</code></pre>`);
    return `\u0000BLOCK${blocks.length - 1}\u0000`;
  });

  // 2. 行内元素（先转义，再替换）
  const inline = (s) => {
    let out = esc(s);
    out = out.replace(/`([^`]+)`/g, (m, c) => `<code>${c}</code>`);
    out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
      (m, alt, url) => `<img src="${url}" alt="${alt}">`);
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      (m, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return out;
  };

  const lines = text.split("\n");
  const html = [];
  let listType = null;      // "ul" | "ol"
  let para = [];

  const flushPara = () => {
    if (para.length) { html.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; }
  };
  const closeList = () => { if (listType) { html.push(`</${listType}>`); listType = null; } };

  for (const raw of lines) {
    const line = raw.trimEnd();

    // 代码块占位符
    const ph = line.match(/^\u0000BLOCK(\d+)\u0000$/);
    if (ph) { flushPara(); closeList(); html.push(blocks[Number(ph[1])]); continue; }

    // 空行：结束段落/列表
    if (!line.trim()) { flushPara(); closeList(); continue; }

    // 分割线
    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) {
      flushPara(); closeList(); html.push("<hr>"); continue;
    }

    // 标题
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      const lv = Math.min(h[1].length, 4);
      html.push(`<h${lv}>${inline(h[2])}</h${lv}>`);
      continue;
    }

    // 引用
    const q = line.match(/^>\s?(.*)$/);
    if (q) { flushPara(); closeList(); html.push(`<blockquote>${inline(q[1])}</blockquote>`); continue; }

    // 无序列表
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) {
      flushPara();
      if (listType !== "ul") { closeList(); html.push("<ul>"); listType = "ul"; }
      html.push(`<li>${inline(ul[1])}</li>`);
      continue;
    }

    // 有序列表
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) {
      flushPara();
      if (listType !== "ol") { closeList(); html.push("<ol>"); listType = "ol"; }
      html.push(`<li>${inline(ol[1])}</li>`);
      continue;
    }

    // 普通段落行
    closeList();
    para.push(line);
  }
  flushPara();
  closeList();
  return html.join("\n");
}

function fmtBytes(n) {
  if (n == null || isNaN(n)) return "--";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

function fmtTime(ts) {
  if (!ts) return "--";
  try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return "--"; }
}

function setGlobalStatus(text, isErr = false) {
  const el = $("globalStatus");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("err", isErr);
}

function showError(msg) {
  setGlobalStatus(msg, true);
  console.error(msg);
  setTimeout(() => setGlobalStatus("运行中"), 5000);
}

function bind(btnId, fn) {
  const el = $(btnId);
  if (el) el.addEventListener("click", fn);
}

async function runAction(btnId, fn, okMsg) {
  const btn = $(btnId);
  if (btn) btn.disabled = true;
  try {
    const r = await fn();
    if (okMsg) setGlobalStatus(okMsg);
    return r;
  } catch (e) {
    showError(e.message);
    return null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ========== 主题（与客户端一致的「跟随系统/浅色/深色」） ==========
const THEME_NAMES = { follow: "跟随系统", light: "浅色模式", dark: "深色模式" };
const THEME_KEY = "nascence_admin_theme";

function applyTheme() {
  const isDark = state.theme === "dark" ||
    (state.theme === "follow" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.body.classList.toggle("dark-mode", isDark);
  const btn = $("btnTheme");
  if (btn) btn.textContent = THEME_NAMES[state.theme] || "跟随系统";
}
function toggleTheme() {
  const cycle = { follow: "light", light: "dark", dark: "follow" };
  state.theme = cycle[state.theme] || "follow";
  try { localStorage.setItem(THEME_KEY, state.theme); } catch (e) {}
  applyTheme();
}
try { state.theme = localStorage.getItem(THEME_KEY) || "follow"; } catch (e) {}
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if (state.theme === "follow") applyTheme();
  });
}

// ========== Tab 切换（标签卡开合动画：旧页向上缩回，新页从上方放大打开）==========
let _switchTimer = null;
function switchTab(name) {
  if (name === state.activeTab) { closeNavDropdown(); return; }
  const cur = document.getElementById(state.activeTab);
  const next = document.getElementById(name);
  state.activeTab = name;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".dd-item").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  closeNavDropdown();
  const detailBox = $("userDetail");
  if (detailBox) detailBox.classList.remove("card-animate");

  const reveal = () => {
    document.querySelectorAll(".page").forEach(p => { p.classList.remove("active", "leaving"); });
    if (next) next.classList.add("active");
    if (name === "overview") { refreshStats(); loadUsers(); }
    if (name === "users") {
      loadUsers();
      if (state.selectedUser && state.userSub === "ulog") loadUserLogs();
    }
    if (name === "config") { loadBotName(); loadModelConfig(); refreshDimStatus(); }
    if (name === "logs") renderGlobalLog();
    if (name === "tools") { loadBroadcast(); loadIpRules(); loadBannedAccounts(); }
  };

  if (cur && cur !== next) {
    cur.classList.remove("active");
    cur.classList.add("leaving");
    if (_switchTimer) clearTimeout(_switchTimer);
    _switchTimer = setTimeout(reveal, 240);
  } else {
    reveal();
  }
}
document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => switchTab(t.dataset.tab));
});

// ========== 窄屏目录图标：点击在中间向下展开导航，再次点击隐藏 ==========
const navDropdown = $("navDropdown");
function openNavDropdown() { if (navDropdown) navDropdown.classList.add("open"); }
function closeNavDropdown() { if (navDropdown) navDropdown.classList.remove("open"); }
function toggleNavDropdown() { if (navDropdown) navDropdown.classList.toggle("open"); }
const _navToggleBtn = $("btnNavToggle");
if (_navToggleBtn) {
  _navToggleBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleNavDropdown(); });
}
document.querySelectorAll(".dd-item").forEach(t => {
  t.addEventListener("click", () => switchTab(t.dataset.tab));
});
// 点击其它地方关闭下拉导航
document.addEventListener("click", (e) => {
  if (!navDropdown || !navDropdown.classList.contains("open")) return;
  if (navDropdown.contains(e.target) || (_navToggleBtn && _navToggleBtn.contains(e.target))) return;
  closeNavDropdown();
});

// 按实际可用宽度决定：放得下四个导航项就显示导航栏（藏 ☰），否则显示 ☰
function layoutNav() {
  const nav = $("nav"), btn = $("btnNavToggle"), bar = $("topbar");
  if (!nav || !btn || !bar) return;
  const left = document.querySelector(".topbar-left");
  const right = document.querySelector(".topbar-right");
  // 测量"内容自然宽度"：左右栏 flex:1 会自适应填满，须用 scrollWidth 才能反映真实所需空间
  nav.style.display = "flex";
  btn.style.display = "none";
  const navW = nav.scrollWidth || nav.getBoundingClientRect().width;
  const leftW = left ? left.scrollWidth : 0;
  const rightW = right ? right.scrollWidth : 0;
  const w = bar.clientWidth;
  const margin = 40; // 顶栏左右 padding + 元素间隙余量
  const fits = (leftW + navW + rightW + margin) <= w;
  if (fits) {
    nav.style.display = "flex";
    btn.style.display = "none";
    closeNavDropdown();
  } else {
    nav.style.display = "none";
    btn.style.display = "inline-block";
  }
}
window.addEventListener("resize", layoutNav);
if (document.fonts && document.fonts.ready) { document.fonts.ready.then(layoutNav); }

// ========== 用户子标签 ==========
document.querySelectorAll(".subtab").forEach(t => {
  t.addEventListener("click", () => {
    state.userSub = t.dataset.sub;
    document.querySelectorAll(".subtab").forEach(x => x.classList.toggle("active", x === t));
    document.querySelectorAll(".subpage").forEach(p => p.classList.toggle("active", p.id === "sub-" + t.dataset.sub));
    if (t.dataset.sub === "ulog") loadUserLogs();
  });
});

// ========== WebSocket 实时推送 ==========
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  let pingTimer = null;
  ws.onopen = () => {
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
    }, 10000);
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "pong") return;
      if (msg.type === "log") {
        onWsLog(msg);
      } else if (msg.type === "status") {
        setGlobalStatus(msg.text);
        const b = $("qqStatusBadge");
        if (b && msg.text.includes("认知循环")) b.textContent = msg.text;
      }
      // 不再有对话测试的 message 气泡推送
    } catch (e) { /* ignore */ }
  };
  ws.onclose = () => { clearInterval(pingTimer); setTimeout(connectWs, 3000); };
  ws.onerror = () => { clearInterval(pingTimer); };
}

// ========== 总览：全局统计 ==========
let _clockBase = null;   // {ts: 服务端真实时间戳(秒), at: 本地接收时刻(ms)}
function tickClock() {
  const el = $("ovClock");
  if (!el) return;
  const base = _clockBase;
  const ts = base ? (base.ts * 1000 + (Date.now() - base.at)) : Date.now();
  const d = new Date(ts);
  const p = n => String(n).padStart(2, "0");
  el.textContent = `当前时间 ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function refreshStats() {
  try {
    const s = await api("/api/status");
    $("statMem").textContent = s.mem;
    $("statLinks").textContent = s.links;
    $("statWords").textContent = s.words;
    $("statRuntime").textContent = s.runtime;
    // 记录服务端真实时间基准，本地每秒平滑走动
    if (s.clock_ts) _clockBase = { ts: s.clock_ts, at: Date.now() };
    tickClock();
    $("qqStatusBadge").textContent = s.cognition.running ? "运行中" : "已停止";
    $("qqStatusBadge").classList.toggle("off", !s.cognition.running);
    if (s.bot_name) state.botName = s.bot_name;
    setGlobalStatus("运行中");
  } catch (e) { /* silent */ }

  try {
    const sys = await api("/api/system");
    loadSystemLoad(sys);
  } catch (e) { /* silent */ }
}

// 设置环形仪表的进度（pct: 0-100）
// opts.label: 中心显示文本（覆盖默认的百分比/--）；opts.scaleMax: 传比例时用于计算弧长
function setRing(ringId, pct, opts) {
  const ring = $(ringId);
  if (!ring) return;
  const optsObj = opts || {};
  const circle = ring.querySelector("circle.val");
  const pctEl = ring.querySelector(".pct");
  const R = 33, C = 2 * Math.PI * R;
  const p = (pct == null || isNaN(pct)) ? 0 : Math.max(0, Math.min(100, pct));
  if (circle) {
    circle.style.strokeDasharray = C.toFixed(2);
    circle.style.strokeDashoffset = (C * (1 - p / 100)).toFixed(2);
  }
  if (pctEl) {
    if (optsObj.label != null) pctEl.textContent = optsObj.label;
    else pctEl.textContent = (pct == null || isNaN(pct)) ? "--" : Math.round(p) + "%";
  }
  ring.classList.remove("ok", "warn", "err");
  if (pct != null && !isNaN(pct)) {
    if (p >= 85) ring.classList.add("err");
    else if (p >= 65) ring.classList.add("warn");
    else ring.classList.add("ok");
  }
}

function loadSystemLoad(sys) {
  const notice = $("sysNotice");
  if (sys && sys.available) {
    if (notice) notice.style.display = "none";
    const cpu = sys.cpu_percent;
    $("sysCpu").textContent = (cpu != null ? cpu.toFixed(0) + "%" : "--");
    $("sysCpuCount").textContent = sys.cpu_count ? `${sys.cpu_count} 核` : "";
    setRing("ringCpu", cpu);
    if (sys.mem) {
      $("sysMem").textContent = (sys.mem.percent != null ? sys.mem.percent.toFixed(0) + "%" : "--");
      $("sysMemSub").textContent = `${fmtBytes(sys.mem.used)} / ${fmtBytes(sys.mem.total)}`;
      setRing("ringMem", sys.mem.percent);
    } else { setRing("ringMem", null); }
    if (sys.disk) {
      $("sysDisk").textContent = (sys.disk.percent != null ? sys.disk.percent.toFixed(0) + "%" : "--");
      $("sysDiskSub").textContent = `${fmtBytes(sys.disk.used)} / ${fmtBytes(sys.disk.total)}`;
      setRing("ringDisk", sys.disk.percent);
    } else { $("sysDisk").textContent = "N/A"; setRing("ringDisk", null); }
    $("sysProc").textContent = (sys.proc_count != null ? sys.proc_count : "--");
    $("sysLoad").textContent = sys.load_avg ? `load ${sys.load_avg.map(v => v.toFixed(2)).join(" / ")}` : "";
    // 进程数：无 0-100 百分比语义，环形按软上限比例画弧，中心显示真实数值
    if (sys.proc_count != null) {
      const PROC_REF = 400;  // 软参考值：达到/超过即视为满环
      const procPct = Math.min(100, (sys.proc_count / PROC_REF) * 100);
      setRing("ringProc", procPct, { label: String(sys.proc_count) });
    } else {
      setRing("ringProc", null, { label: "--" });
    }
    $("sysPlatform").textContent = sys.platform || "--";
  } else {
    ["sysCpu", "sysMem", "sysDisk", "sysProc"].forEach(id => { const el = $(id); if (el) el.textContent = "N/A"; });
    ["sysCpuCount", "sysMemSub", "sysDiskSub", "sysLoad"].forEach(id => { const el = $(id); if (el) el.textContent = ""; });
    ["ringCpu", "ringMem", "ringDisk", "ringProc"].forEach(id => setRing(id, null));
    $("sysPlatform").textContent = (sys && sys.platform) || "--";
    if (notice) {
      notice.style.display = "";
      notice.textContent = "服务器负载不可用：" + ((sys && sys.reason) || "未安装 psutil（可在服务器 pip install psutil 后重启）");
    }
  }
}

// ========== 用户列表与跳转 ==========
let _currentDetailUser = null;

function roleLabel(role) {
  return ({ student: "学生", parent: "家长", teacher: "老师" })[role] || role || "--";
}

function triggerCardAnimation(el) {
  if (!el) return;
  el.classList.remove("card-animate");
  void el.offsetWidth; // 强制重绘以重新触发卡片开合动画
  el.classList.add("card-animate");
}

function selectUser(username, { animate = true } = {}) {
  if (!username) return;
  const detailBox = $("userDetail");
  const hint = $("userEmptyHint");
  const isHidden = !detailBox || detailBox.style.display === "none";
  const isSwitch = _currentDetailUser && _currentDetailUser !== username;
  const isExpand = isHidden;

  state.selectedUser = username;

  // 高亮用户列表并滚入可视区域
  document.querySelectorAll("#userList .user-item").forEach(x => {
    const isTarget = x.dataset.username === username;
    x.classList.toggle("active", isTarget);
    if (isTarget) x.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  if (hint) hint.style.display = "none";
  if (detailBox) {
    detailBox.style.display = "";
    // 仅展开和切换时使用动画（与其他标签卡相同的动画）
    if (animate && (isExpand || isSwitch)) {
      triggerCardAnimation(detailBox);
    }
  }

  renderUserDetail(username);
  if (state.userSub === "ulog") loadUserLogs();
}

function jumpToUser(username) {
  if (!username) return;
  if (state.activeTab === "users") {
    selectUser(username, { animate: true });
    return;
  }
  const detailBox = $("userDetail");
  const hint = $("userEmptyHint");
  if (hint) hint.style.display = "none";
  if (detailBox) detailBox.style.display = "";
  _currentDetailUser = username;
  state.selectedUser = username;

  // 提前请求详情数据，与切页动画并行
  renderUserDetail(username);
  switchTab("users");

  setTimeout(() => {
    document.querySelectorAll("#userList .user-item").forEach(x => {
      const isTarget = x.dataset.username === username;
      x.classList.toggle("active", isTarget);
      if (isTarget) x.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    if (state.userSub === "ulog") loadUserLogs();
  }, 250);
}

async function loadUsers() {
  try {
    const r = await api("/api/users");
    allUsers = r.users || [];
    // 渲染时应用当前搜索关键词（列表每秒刷新也会保持过滤状态）
    _renderFilteredUsers();
    const active = allUsers.filter(u => (u.is_online !== undefined ? u.is_online : (u.worker_alive && !u.paused))).length;
    const ov = $("ovActive"); if (ov) ov.textContent = active;
    const tot = $("ovUsers"); if (tot) tot.textContent = allUsers.length;
    if (state.selectedUser) renderUserDetail(state.selectedUser);
  } catch (e) { /* silent */ }
}

function renderUserList(targetId, users, clickable) {
  const box = $(targetId);
  if (!box) return;
  box.innerHTML = "";
  if (!users.length) {
    box.innerHTML = '<div class="empty">暂无用户</div>';
    return;
  }
  users.forEach(u => {
    const div = document.createElement("div");
    div.className = "user-item" + (u.username === state.selectedUser ? " active" : "");
    div.dataset.username = u.username;
    const isOnline = (u.is_online !== undefined ? u.is_online : (u.worker_alive && !u.paused));
    let statusBadge = "";
    if (isOnline) {
      statusBadge = `<span class="badge ok">${u.cognition_running ? "运行中" : "在线"}</span>`;
    } else if (u.worker_alive && u.pending_exit_sec != null) {
      statusBadge = `<span class="badge warn" title="下线中，倒计时结束将保存数据并释放进程">下线(${Math.ceil(u.pending_exit_sec)}s)</span>`;
    } else {
      statusBadge = `<span class="badge off">离线</span>`;
    }
    div.innerHTML = `
      <div class="user-item-head"><span class="user-name">${escapeHtml(u.username)}</span>${u.banned ? '<span class="badge err" title="' + escapeHtml(u.ban_reason || "已被管理员封禁") + '">已封禁</span>' : ""}${statusBadge}</div>
      <div class="user-meta"><span class="role-tag">${roleLabel(u.role)}</span> ${escapeHtml(u.user_id || "")}${u.minor_mode ? ' · <span class="badge warn">未成年模式</span>' : ""}</div>
      <div class="user-meta">记忆 ${u.stats ? u.stats.mem : "-"} · 链接 ${u.stats ? u.stats.links : "-"} · 消息 ${u.stats ? u.stats.msgs : "-"}</div>
    `;
    if (clickable) {
      div.addEventListener("click", () => {
        if (targetId === "ovUserList") {
          jumpToUser(u.username);
        } else {
          selectUser(u.username, { animate: true });
        }
      });
    }
    box.appendChild(div);
  });
}

// ========== 用户详情 ==========
async function renderUserDetail(username) {
  const box = $("userDetail");
  const hint = $("userEmptyHint");
  if (!username) {
    if (box) box.style.display = "none";
    if (hint) hint.style.display = "";
    _currentDetailUser = null;
    return;
  }
  if (hint) hint.style.display = "none";
  if (box) box.style.display = "";
  _currentDetailUser = username;
  $("userDetailName").textContent = username;
  try {
    const r = await api(`/api/user/overview?username=${encodeURIComponent(username)}`);
    if (!r.ok) { showError(r.message || "加载失败"); return; }
    if (state.selectedUser && state.selectedUser !== username) return;
    // 实时字段优先用列表（/api/users，每秒刷新）中的最新值，保证与列表一致
    const live = allUsers.find(u => u.username === username) || {};
    const p = r.profile || {};
    const st = (live.stats) || r.stats || {};
    $("userDetailRole").textContent = roleLabel(p.role);
    $("uovName").textContent = p.username || username;
    $("uovRole").textContent = roleLabel(p.role);
    $("uovUid").textContent = p.user_id || "--";
    $("uovMem").textContent = `${st.mem ?? "-"} / ${st.links ?? "-"}`;
    $("uovMsgs").textContent = st.msgs ?? "-";

    // 绑定学生（超链接表示，点击可跳转）
    const linkedBox = $("uovLinked");
    if (linkedBox) {
      const linked = p.linked_students || [];
      if (!linked.length) {
        linkedBox.textContent = "（无）";
      } else {
        linkedBox.innerHTML = "";
        linked.forEach((e, idx) => {
          if (idx > 0) linkedBox.appendChild(document.createTextNode("、"));
          const targetUname = e.username || (allUsers.find(u => u.user_id && u.user_id === e.user_id)?.username) || "";
          const displayName = e.student_name || targetUname || e.user_id || "--";
          const relText = e.relation ? `（${e.relation}）` : "";
          if (targetUname) {
            const a = document.createElement("a");
            a.className = "user-link";
            a.href = "javascript:void(0)";
            a.textContent = displayName;
            a.dataset.username = targetUname;
            a.title = `查看用户 ${targetUname} 详情`;
            a.addEventListener("click", (ev) => {
              ev.preventDefault();
              selectUser(targetUname, { animate: true });
            });
            linkedBox.appendChild(a);
            if (relText) linkedBox.appendChild(document.createTextNode(relText));
          } else {
            linkedBox.appendChild(document.createTextNode(displayName + relText));
          }
        });
      }
    }

    // 被绑定信息（超链接表示，点击可跳转）
    const boundByBox = $("uovBoundBy");
    if (boundByBox) {
      const boundBy = p.bound_by || [];
      if (!boundBy.length) {
        boundByBox.textContent = "（无）";
      } else {
        boundByBox.innerHTML = "";
        boundBy.forEach((b, idx) => {
          if (idx > 0) boundByBox.appendChild(document.createTextNode("、"));
          const targetUname = b.username || "";
          const displayName = b.username || b.user_id || "--";
          let relText = "";
          if (b.relation) relText = `（${b.relation}）`;
          else if (b.role) relText = `（${roleLabel(b.role)}）`;
          if (targetUname) {
            const a = document.createElement("a");
            a.className = "user-link";
            a.href = "javascript:void(0)";
            a.textContent = displayName;
            a.dataset.username = targetUname;
            a.title = `查看用户 ${targetUname} 详情`;
            a.addEventListener("click", (ev) => {
              ev.preventDefault();
              selectUser(targetUname, { animate: true });
            });
            boundByBox.appendChild(a);
            if (relText) boundByBox.appendChild(document.createTextNode(relText));
          } else {
            boundByBox.appendChild(document.createTextNode(displayName + relText));
          }
        });
      }
    }

    const lv = (Object.keys(live).length ? live : r);
    $("uovWorker").textContent = lv.worker_alive
      ? `存活${lv.pid ? `（PID ${lv.pid}）` : ""}${lv.pending_exit_sec != null ? ` · 离线退出倒计时 ${Math.ceil(lv.pending_exit_sec)}s` : ""}`
      : "未启动";
    $("uovHeartbeat").textContent = lv.last_heartbeat
      ? `${fmtTime(lv.last_heartbeat)}（${lv.idle_sec != null ? lv.idle_sec + "s 前" : ""}）`
      : "无";
    $("uovCog").textContent = lv.worker_alive
      ? (lv.cognition_running ? "运行中" : (lv.paused ? "已暂停（空闲）" : "已停止"))
      : "未启动";
    // 账号安全区（维护子页）随选中的用户同步封禁状态
    loadUserBanState();
  } catch (e) { showError(e.message); }
}

// ========== 用户日志 ==========
// 用户日志仅在用户主动打开'日志'子标签（state.activeTab === "users" && state.userSub === "ulog"）时向服务器拉取
async function loadUserLogs() {
  if (!state.selectedUser) return;
  // 严格检查：只有当前处于用户页且子标签为'日志'时才向服务器申请并拉取
  if (state.activeTab !== "users" || state.userSub !== "ulog") return;
  try {
    const r = await api(`/api/user_logs?username=${encodeURIComponent(state.selectedUser)}`);
    const view = $("userLogView");
    if (!view) return;
    view.innerHTML = "";
    (r.logs || []).forEach(e => appendLogLineTo(view, e));
    const auto = $("userLogAuto");
    if (!auto || auto.checked) view.scrollTop = view.scrollHeight;
  } catch (e) { /* silent */ }
}
bind("btnUserLogRefresh", () => loadUserLogs());

async function userMaintenance(action) {
  if (!state.selectedUser) return;
  const resEl = $("userMaintResult");
  if (resEl) resEl.textContent = `正在执行：${action} …（首次可能需拉起进程，请稍候）`;
  try {
    const r = await api("/api/user/maintenance", "POST", { username: state.selectedUser, action });
    if (resEl) resEl.textContent = `[${new Date().toLocaleTimeString()}] ${action}：${r.message || "完成"}`;
    loadUsers();
    loadUserLogs();
  } catch (e) {
    if (resEl) resEl.textContent = `[${new Date().toLocaleTimeString()}] ${action} 失败：${e.message}`;
  }
}
document.querySelectorAll("[data-umaint]").forEach(btn => {
  btn.addEventListener("click", () => {
    const act = btn.dataset.umaint;
    if ((act === "purge_expired" || act === "sleep_maintenance") &&
        !confirm(`确认对该用户执行「${btn.textContent}」？`)) return;
    userMaintenance(act);
  });
});

// ========== 账号安全：修改密码 / 封禁解封（用户 → 维护） ==========
// 同步当前选中用户的封禁状态到维护页按钮与徽章
function syncUserBanUI(banned, reason) {
  const badge = $("userBanBadge");
  const btnBan = $("btnUserBan");
  const btnUnban = $("btnUserUnban");
  const reasonInput = $("userBanReason");
  if (badge) {
    badge.className = "badge " + (banned ? "err" : "ok");
    badge.textContent = banned
      ? ("已封禁" + (reason ? `（${reason}）` : ""))
      : "正常";
  }
  if (btnBan) btnBan.style.display = banned ? "none" : "";
  if (btnUnban) btnUnban.style.display = banned ? "" : "none";
  // 已封禁时把原因回填到输入框，便于查看/修改
  if (reasonInput && banned && !reasonInput.value) reasonInput.value = reason || "";
}

async function loadUserBanState() {
  if (!state.selectedUser) return;
  // 优先用列表里已下发的 banned 字段（每秒刷新，最新）
  const live = allUsers.find(u => u.username === state.selectedUser);
  if (live && live.banned !== undefined) {
    syncUserBanUI(!!live.banned, live.ban_reason || "");
    return;
  }
  try {
    const r = await api(`/api/user/overview?username=${encodeURIComponent(state.selectedUser)}`);
    const p = r.profile || {};
    syncUserBanUI(!!p.banned, p.ban_reason || "");
  } catch (e) { /* 静默：状态显示失败不影响其他操作 */ }
}

async function setUserPassword() {
  const uname = state.selectedUser;
  if (!uname) { showError("请先选择一个用户"); return; }
  const inp = $("userNewPassword");
  const pwd = (inp && inp.value || "").trim();
  const msg = $("userPasswordMsg");
  if (pwd.length < 4) { showError("新密码至少 4 位"); return; }
  if (!confirm(`确认修改「${uname}」的密码？\n该用户所有已登录设备会立即被强制下线。`)) return;
  try {
    const r = await api("/api/user/password", "POST", { username: uname, new_password: pwd });
    if (inp) inp.value = "";
    if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] ${r.message || "已修改"}`;
    loadUsers();
  } catch (e) {
    if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] 修改失败：${e.message}`;
    showError(e.message);
  }
}

async function setUserBan(banned) {
  const uname = state.selectedUser;
  if (!uname) { showError("请先选择一个用户"); return; }
  const reasonInput = $("userBanReason");
  const reason = (reasonInput && reasonInput.value || "").trim();
  const msg = $("userBanMsg");
  const tip = banned
    ? `确认封禁「${uname}」？\n该用户将无法登录，已登录会话会被立即强制下线。`
    : `确认解除「${uname}」的封禁？`;
  if (!confirm(tip)) return;
  try {
    const r = await api("/api/user/ban", "POST", { username: uname, banned, reason });
    if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] ${r.message || "完成"}`;
    if (reasonInput && !banned) reasonInput.value = "";
    syncUserBanUI(banned, banned ? reason : "");
    loadUsers();
    loadUserLogs();
    if (typeof loadBannedAccounts === "function") loadBannedAccounts();
  } catch (e) {
    if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] 操作失败：${e.message}`;
    showError(e.message);
  }
}

bind("btnUserSetPassword", setUserPassword);
bind("btnUserBan", () => setUserBan(true));
bind("btnUserUnban", () => setUserBan(false));
const _pwdInput = $("userNewPassword");
if (_pwdInput) _pwdInput.addEventListener("keydown", (e) => { if (e.key === "Enter") setUserPassword(); });

// ========== 工具页：账号封禁名单（黑名单=已封禁 / 白名单=正常） ==========
let _bannedAccounts = [];
let _normalAccounts = [];

function renderAccountList(boxId, list, mode) {
  const box = $(boxId);
  if (!box) return;
  box.innerHTML = "";
  if (!list.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = mode === "banned" ? "暂无已封禁账号" : "暂无用户";
    box.appendChild(d);
    return;
  }
  list.forEach(item => {
    const row = document.createElement("div");
    row.className = "ip-tag-item";
    const info = document.createElement("span");
    const when = (mode === "banned" && item.banned_at)
      ? ` · ${new Date(item.banned_at * 1000).toLocaleString()}`
      : "";
    const reason = (mode === "banned" && item.reason) ? ` · 原因：${item.reason}` : "";
    info.textContent = `${item.username}（${roleLabel(item.role)} · ${item.user_id || "--"}）${reason}${when}`;
    info.title = info.textContent;
    info.style.overflow = "hidden";
    info.style.textOverflow = "ellipsis";
    info.style.whiteSpace = "nowrap";
    row.appendChild(info);
    const btn = document.createElement("button");
    btn.className = "btn-remove-ip";
    btn.textContent = mode === "banned" ? "解封" : "封禁";
    btn.title = mode === "banned" ? "解除封禁，恢复登录权限" : "封禁该账号";
    btn.style.fontSize = "12px";
    btn.addEventListener("click", () => quickBan(item.username, mode !== "banned"));
    row.appendChild(btn);
    box.appendChild(row);
  });
}

async function loadBannedAccounts() {
  try {
    const r = await api("/api/user/bans");
    _bannedAccounts = r.banned || [];
    _normalAccounts = r.normal || [];
    const kw = ($("inputAccountSearch") && $("inputAccountSearch").value || "").trim().toLowerCase();
    const normal = kw
      ? _normalAccounts.filter(x => x.username.toLowerCase().includes(kw) || (x.user_id || "").toLowerCase().includes(kw))
      : _normalAccounts;
    renderAccountList("bannedAccountBox", _bannedAccounts, "banned");
    renderAccountList("normalAccountBox", normal, "normal");
    const bc = $("bannedAccountCount"); if (bc) bc.textContent = _bannedAccounts.length;
    const nc = $("normalAccountCount"); if (nc) nc.textContent = _normalAccounts.length;
    const sum = $("banSummary");
    // 与列顺序保持一致：白名单在前（左列），黑名单在后（右列）
    if (sum) sum.textContent = `白名单 ${_normalAccounts.length} 个 · 黑名单 ${_bannedAccounts.length} 个`;
  } catch (e) { showError(e.message); }
}

async function quickBan(username, banned) {
  const reasonInput = $("inputBanReason");
  const reason = (reasonInput && reasonInput.value || "").trim();
  const tip = banned
    ? `确认封禁「${username}」？\n该用户将无法登录，已登录会话会被立即强制下线。`
    : `确认解除「${username}」的封禁？`;
  if (!confirm(tip)) return;
  try {
    const r = await api("/api/user/ban", "POST", { username, banned, reason });
    if (banned && reasonInput) reasonInput.value = "";
    const msg = $("banActionMsg");
    if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] ${r.message || "完成"}`;
    loadBannedAccounts();
    loadUsers();
    if (state.selectedUser === username) syncUserBanUI(banned, banned ? reason : "");
  } catch (e) { showError(e.message); }
}

bind("btnReloadBans", () => loadBannedAccounts());
bind("btnBanAccount", () => {
  const inp = $("inputBanUser");
  const uname = (inp && inp.value || "").trim();
  if (!uname) { showError("请输入要封禁的用户名"); return; }
  quickBan(uname, true).then(() => { if (inp) inp.value = ""; });
});
const _inputBanUser = $("inputBanUser");
if (_inputBanUser) _inputBanUser.addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnBanAccount").click(); });
const _inputAcctSearch = $("inputAccountSearch");
if (_inputAcctSearch) _inputAcctSearch.addEventListener("input", () => {
  const kw = _inputAcctSearch.value.trim().toLowerCase();
  const normal = kw
    ? _normalAccounts.filter(x => x.username.toLowerCase().includes(kw) || (x.user_id || "").toLowerCase().includes(kw))
    : _normalAccounts;
  renderAccountList("normalAccountBox", normal, "normal");
});

// ========== 全局日志 ==========
let globalLogs = [];          // 结构化条目 {cat,line,level,user}
const LOG_LIMIT = 1500;
let globalLogAtBottom = true;

function onWsLog(msg) {
  const entry = { cat: msg.cat, line: msg.line, level: msg.level || "INFO", user: (msg.user || "").trim() };
  
  // 仅在属于全局（非用户专属）且非思考类日志时才纳入全局日志缓冲
  const isUserThinking = entry.cat === "thinking" || !!entry.user;
  if (!isUserThinking) {
    globalLogs.push(entry);
    if (globalLogs.length > LOG_LIMIT) globalLogs.splice(0, globalLogs.length - LOG_LIMIT);
    if (state.activeTab === "logs") appendLogEntry(entry);
  }

  // 若正打开某用户的日志子标签，实时将该用户的新日志（含其专属思考日志）动态追加
  if (state.activeTab === "users" && state.userSub === "ulog" && entry.user && entry.user === state.selectedUser) {
    const view = $("userLogView");
    if (view) {
      appendLogLineTo(view, entry);
      const auto = $("userLogAuto");
      if (!auto || auto.checked) view.scrollTop = view.scrollHeight;
    }
  }
}

function appendLogEntry(entry) {
  const view = $("logView");
  if (!view) return;
  appendLogLineTo(view, entry);
  const auto = $("logAuto");
  if (!auto || auto.checked) view.scrollTop = view.scrollHeight;
}

function appendLogLineTo(view, entry) {
  const div = document.createElement("div");
  div.className = "log-line";
  const lvl = (entry.level || "INFO").toUpperCase();
  if (lvl === "WARNING" || lvl === "WARN") div.classList.add("warn");
  else if (lvl === "ERROR" || lvl === "CRITICAL") div.classList.add("err");
  if (entry.user) { div.classList.add("user-tag"); div.dataset.user = "[" + entry.user + "]"; }
  div.textContent = entry.line || "";
  view.appendChild(div);
  while (view.children.length > LOG_LIMIT) view.removeChild(view.firstChild);
}

function renderGlobalLog() {
  const view = $("logView");
  if (!view) return;
  view.innerHTML = "";
  globalLogs.forEach(e => appendLogLineTo(view, e));
  view.scrollTop = view.scrollHeight;
}

async function loadLogs() {
  try {
    const data = await api("/api/logs");
    globalLogs = (data.logs || []).slice(-LOG_LIMIT);
    renderGlobalLog();
  } catch (e) { /* silent */ }
}

bind("btnClearLogs", async () => {
  globalLogs = [];
  const view = $("logView"); if (view) view.innerHTML = "";
  try { await api("/api/logs/clear", "POST", {}); } catch (e) { /* silent */ }
});

// ========== 模型管理 ==========
const MODEL_LABELS = { text: "文本能力", embed: "语义向量", multimodal: "多模态图片" };

async function loadModelConfig() {
  try {
    const cfg = await api("/api/config");
    const status = await api("/api/status");
    const box = $("modelCards");
    box.innerHTML = "";
    for (const [name, label] of Object.entries(MODEL_LABELS)) {
      const b = (cfg.backends || {})[name] || {};
      const m = (status.models || {})[name] || {};
      const card = document.createElement("div");
      card.className = "menu-card";
      card.style.marginTop = "12px";
      card.innerHTML = `
        <div class="pixel-block"></div>
        <div class="card-head">
          <span class="card-title">${label}</span>
          <label class="muted" style="display:flex;align-items:center;gap:6px;">
            <input type="checkbox" data-name="${name}" ${b.use_default ? "checked" : ""}> 使用本地默认模型
          </label>
        </div>
        <div class="card-body">
          <div class="muted">当前：${b.use_default
            ? (m.local_ready ? `本地默认模型（端口 ${m.port}，已就绪）` : `本地默认模型（端口 ${m.port}，未加载）`)
            : `外部 API → ${escapeHtml(b.api_model || "（未配置模型名）")}`}</div>
          <div class="row"><span class="label">Base URL</span><input type="text" data-field="api_base_url" data-name="${name}" value="${escapeHtml(b.api_base_url || "")}" style="flex:1"></div>
          <div class="row"><span class="label">API Key</span><input type="text" data-field="api_key" data-name="${name}" value="${escapeHtml(b.api_key || "")}" placeholder="留空则保留原 Key" style="flex:1" autocomplete="off"></div>
          <div class="row"><span class="label">模型名</span><input type="text" data-field="api_model" data-name="${name}" value="${escapeHtml(b.api_model || "")}" style="flex:1"></div>
          <div class="row"><button class="btn small" data-save="${name}">保存并应用</button><span class="muted" data-hint="${name}"></span></div>
        </div>
      `;
      box.appendChild(card);
    }
  } catch (e) { showError(e.message); }
}

$("modelCards").addEventListener("click", async (ev) => {
  const saveBtn = ev.target.closest("[data-save]");
  if (!saveBtn) return;
  const name = saveBtn.dataset.save;
  const card = saveBtn.closest(".menu-card");
  const useDefault = card.querySelector(`input[type=checkbox][data-name="${name}"]`).checked;
  const apiBaseUrl = card.querySelector(`input[data-field=api_base_url][data-name="${name}"]`).value;
  const apiKey = card.querySelector(`input[data-field=api_key][data-name="${name}"]`).value;
  const apiModel = card.querySelector(`input[data-field=api_model][data-name="${name}"]`).value;
  try {
    const r = await api("/api/model/switch", "POST", {
      name, use_default: useDefault, api_base_url: apiBaseUrl, api_key: apiKey, api_model: apiModel,
    });
    const hint = card.querySelector(`[data-hint="${name}"]`);
    hint.textContent = r.message + (r.needs_restart ? "（需要重启进程才能加载本地默认模型）" : "");
    loadModelConfig();
  } catch (e) { showError(e.message); }
});

// ========== 配置页：Bot 名字 / 端口 / 认知循环 / 宵禁 ==========
async function loadBotName() {
  try {
    const cfg = await api("/api/config");
    const set = (id, v) => { const el = $(id); if (el) el.value = v; };
    set("botNameInput", cfg.bot_name || "");
    set("serverPortInput", cfg.server_port || "");
    set("clientPortInput", cfg.client_port || "");
    set("sleepStartInput", cfg.sleep_start || "23:00");
    set("sleepEndInput", cfg.sleep_end || "07:00");
    const autoCog = $("autoCognitionCheck");
    if (autoCog) autoCog.checked = !!cfg.auto_start_cognition;
    ["botNameHint", "portHint", "clientPortHint", "autoCognitionHint", "sleepHint"].forEach(id => set(id, ""));
  } catch (e) { /* silent */ }
}

bind("btnSaveAutoCognition", async () => {
  const cb = $("autoCognitionCheck");
  const val = cb ? cb.checked : false;
  const r = await runAction("btnSaveAutoCognition", () => api("/api/config", "POST", { auto_start_cognition: val }));
  if (r) { $("autoCognitionHint").textContent = "已保存，重启进程后生效。"; }
});

bind("btnSaveSleep", async () => {
  const start = ($("sleepStartInput") || {}).value || "";
  const end = ($("sleepEndInput") || {}).value || "";
  if (!start || !end) { showError("请填写入睡与起床时间"); return; }
  const r = await runAction("btnSaveSleep", () => api("/api/config", "POST", { sleep_start: start, sleep_end: end }));
  if (r) {
    $("sleepHint").textContent = "已保存，即时生效。";
    alert(start === end
      ? `入睡与起床时间相同（${start}），已取消宵禁（不再睡眠暂停，但仍会在入睡点执行睡眠维护）。`
      : `宵禁时间已更新为 ${start} – ${end}（即时生效）。`);
  }
});

bind("btnSaveBotName", async () => {
  const name = ($("botNameInput") || {}).value?.trim() || "";
  if (!name) { showError("Bot 名字不能为空"); return; }
  const r = await runAction("btnSaveBotName", () => api("/api/config", "POST", { bot_name: name }));
  if (r) { $("botNameHint").textContent = "已保存，需重启进程后生效。"; }
});

bind("btnUploadBotAvatar", async () => {
  const fileInput = $("botAvatarFile");
  const hint = $("botAvatarHint");
  const preview = $("botAvatarPreview");
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file) { if (hint) hint.textContent = "请先选择图片文件。"; return; }
  if (file.size > 5 * 1024 * 1024) { if (hint) hint.textContent = "图片过大（限制 5MB）。"; return; }
  if (preview) {
    const reader = new FileReader();
    reader.onload = () => { preview.src = reader.result; preview.style.display = "inline-block"; };
    reader.readAsDataURL(file);
  }
  if (hint) hint.textContent = "上传中…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const resp = await fetch("/api/bot_avatar", { method: "POST", body: fd });
    const data = await resp.json().catch(() => ({ ok: false, message: "解析响应失败" }));
    if (!resp.ok || !data.ok) { if (hint) hint.textContent = `上传失败：${data.message || ""}`; return; }
    if (hint) hint.textContent = "已上传，客户端刷新后可见。";
  } catch (e) { if (hint) hint.textContent = `上传失败：${e.message || String(e)}`; }
});

bind("btnSavePort", async () => {
  const port = parseInt((($("serverPortInput") || {}).value || "").trim(), 10);
  if (!port || port < 1 || port > 65535) { showError("端口需为 1-65535 的整数"); return; }
  const r = await runAction("btnSavePort", () => api("/api/config", "POST", { server_port: port }));
  if (r) { $("portHint").textContent = r.message || "端口已保存。"; }
});

bind("btnSaveClientPort", async () => {
  const port = parseInt((($("clientPortInput") || {}).value || "").trim(), 10);
  if (!port || port < 1 || port > 65535) { showError("网页版客户端端口需为 1-65535 的整数"); return; }
  const r = await runAction("btnSaveClientPort", () => api("/api/config", "POST", { client_port: port }));
  if (r) { $("clientPortHint").textContent = r.message || "端口已保存。"; }
});

// ========== 全局维护：语义向量维度重建 ==========
let dimPollTimer = null;
async function refreshDimStatus() {
  try {
    const s = await api("/api/maintenance/embed_dim");
    const setText = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    setText("dimStored", s.stored_dim ?? "--");
    setText("dimModel", s.model_dim ?? "--");
    setText("dimCount", s.memory_count ?? "--");
    const btn = $("btnRebuildDim");
    const progress = $("dimProgress");
    if (s.running) {
      if (btn) btn.disabled = true;
      if (progress) progress.textContent = `重建中… ${s.done}/${s.total}${s.target_dim ? ` → ${s.target_dim} 维` : ""}`;
      if (!dimPollTimer) dimPollTimer = setInterval(refreshDimStatus, 2000);
    } else {
      if (btn) btn.disabled = false;
      if (progress) progress.textContent = s.error ? `任务失败：${s.error}` : "（空闲）";
      if (dimPollTimer) { clearInterval(dimPollTimer); dimPollTimer = null; }
    }
    const logEl = $("dimLog");
    if (logEl && Array.isArray(s.log) && s.log.length) {
      logEl.innerHTML = s.log.map(l => escapeHtml(l)).join("<br>");
      logEl.scrollTop = logEl.scrollHeight;
    }
  } catch (e) { /* silent */ }
}
bind("btnRebuildDim", async () => {
  const raw = ($("dimTarget") || {}).value?.trim() || "";
  const target_dim = raw ? (parseInt(raw, 10) || null) : null;
  if (!confirm("确认开始全量维度重建？\n该操作会重新生成全部记忆向量并重建索引，期间请勿进行其他记忆操作。")) return;
  const r = await runAction("btnRebuildDim", () => api("/api/maintenance/embed_rebuild", "POST", { target_dim }));
  if (r && r.ok) { setGlobalStatus("维度重建已开始"); refreshDimStatus(); }
  else if (r) { showError(r.message || "启动失败"); }
});

// ========== 全局操作 ==========
bind("btnQQStart", () => runAction("btnQQStart", () => api("/api/cognition/start", "POST", {}), "认知循环启动中"));
bind("btnQQStop", () => runAction("btnQQStop", () => api("/api/cognition/stop", "POST", {}), "认知循环已停止"));
bind("btnSave", () => runAction("btnSave", () => api("/api/save", "POST", {}), "数据已保存"));

bind("btnFakeThink", async () => {
  const text = (($("fakeThinkInput") || {}).value || "").trim();
  if (!text) return;
  const r = await runAction("btnFakeThink", () => api("/api/fake_think", "POST", { text }));
  if (r) { $("fakeThinkInput").value = ""; setGlobalStatus(`伪造思考完成：${(r.reply || "").slice(0, 40)}`); }
});

bind("btnInject", async () => {
  const content = (($("memInput") || {}).value || "").trim();
  if (!content) return;
  const r = await runAction("btnInject", () => api("/api/inject_memory", "POST", { content }));
  if (r) { $("memInput").value = ""; setGlobalStatus(`记忆注入成功 ID=${r.memory_id}`); }
});

bind("btnShutdown", async () => {
  if (!confirm("确认安全退出全部服务？")) return;
  try { await api("/api/shutdown", "POST", {}); setGlobalStatus("正在退出..."); }
  catch (e) { showError(e.message); }
});

bind("btnRestart", async () => {
  if (!confirm("确认安全重启服务？\n\n会先保存全部数据，然后重新拉起服务进程（约需 20~40 秒，页面会自动重连）。")) return;
  try {
    await api("/api/restart", "POST", {});
    setGlobalStatus("服务正在重启，请稍候…");
    // 轮询等待服务恢复，恢复后自动刷新页面
    let tries = 0;
    const timer = setInterval(async () => {
      tries++;
      try {
        const resp = await fetch("/admin/me", { method: "GET" });
        if (resp.ok) {
          clearInterval(timer);
          setGlobalStatus("服务已重启，正在刷新…");
          setTimeout(() => window.location.reload(), 600);
        }
      } catch (e) { /* 服务尚未起来，继续等待 */ }
      if (tries > 90) {   // 最多等待约 3 分钟
        clearInterval(timer);
        setGlobalStatus("重启后未能自动重连，请手动刷新页面。");
      }
    }, 2000);
  } catch (e) { showError(e.message); }
});

// ========== 在线更新（当前阶段只执行 git pull） ==========
// 结果用「工具」页里的卡片展示 git 真实输出：部署环境（反代/无远端/无权限）下排错全靠它。
// 说明：早前误用了客户端的 modal-* 类名（控制面板样式表里并没有这些类），
// 导致面板既无浮层定位、又无法收起；现改为复用本页既有的卡片样式 + display 开关。
function openUpdatePanel() {
  const p = $("update-panel");
  if (p) p.style.display = "";
}
function closeUpdatePanel() {
  const p = $("update-panel");
  if (p) p.style.display = "none";
  // 收起时顺带清空输出，避免下次误以为还是当次结果
  const out = $("updateOutput");
  if (out) out.textContent = "";
}

async function runUpdate() {
  openUpdatePanel();
  const sum = $("updateSummary");
  const out = $("updateOutput");
  if (sum) { sum.className = "notice"; sum.textContent = "正在执行 git pull…"; }
  if (out) out.textContent = "";
  const btn = $("btnUpdate");
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/update", "POST", {});
    if (sum) {
      sum.className = "notice " + (r.ok ? "update-ok" : "update-err");
      sum.textContent = (r.ok ? "✅ " : "❌ ") + (r.message || (r.ok ? "更新完成" : "更新失败"));
    }
    if (out) {
      const parts = [];
      if (r.remote) parts.push(`远端: ${r.remote}`);
      if (r.before) parts.push(`更新前: ${r.before}`);
      if (r.after) parts.push(`更新后: ${r.after}`);
      const head = parts.join("    ");
      out.textContent = (head ? head + "\n\n" : "") + (r.output || "（无输出）");
    }
    if (r.ok) setGlobalStatus(r.message || "更新完成");
  } catch (e) {
    if (sum) { sum.className = "notice update-err"; sum.textContent = "❌ 更新失败：" + e.message; }
    if (out) out.textContent = String(e.message || e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

bind("btnUpdate", async () => {
  const p = $("update-panel");
  // 已展开时再点一次即为收起（按钮兼作开关，避免出现"关不掉"）
  if (p && p.style.display !== "none") { closeUpdatePanel(); return; }
  if (!confirm("确认执行在线更新（git pull）？\n\n只拉取代码，不会自动重启服务；如需生效请随后点「安全重启服务」。")) return;
  // 面板在「工具」页里：先切到工具页并等渲染完成，再执行，保证用户能看到输出
  switchTab("tools");
  await runUpdate();
});
bind("btnUpdateAgain", () => runUpdate());
bind("btnCloseUpdateModal", () => closeUpdatePanel());

bind("btnAdminLogout", async () => {
  if (!confirm("确认退出管理员登录？")) return;
  try { await fetch("/admin/logout", { method: "POST" }); } catch (e) {}
  window.location.reload();
});

// ========== 用户搜索 ==========
// 输入即过滤（本地过滤已加载的列表，避免额外请求）；
// 匹配用户名 / user_id / 角色，大小写不敏感。
let userFilterKeyword = "";

function _userMatches(u, kw) {
  if (!kw) return true;
  const hay = [
    u.username || "",
    u.user_id || "",
    roleLabel(u.role),
    u.role || "",
  ].join(" ").toLowerCase();
  return hay.includes(kw);
}

function _renderFilteredUsers() {
  const kw = userFilterKeyword.trim().toLowerCase();
  const list = kw ? allUsers.filter(u => _userMatches(u, kw)) : allUsers;
  renderUserList("ovUserList", list, true);
  renderUserList("userList", list, true);
  const cnt = $("userListCount");
  if (cnt) {
    cnt.textContent = kw
      ? `${list.length} / ${allUsers.length} 个用户（已筛选）`
      : `${allUsers.length} 个用户`;
  }
  const emptyHint = $("userEmptyHint");
  if (emptyHint && kw && !list.length) {
    emptyHint.textContent = `没有匹配「${userFilterKeyword.trim()}」的用户`;
  } else if (emptyHint) {
    emptyHint.textContent = "请选择上方的一个用户";
  }
}

const _userSearchInput = $("userSearchInput");
if (_userSearchInput) {
  _userSearchInput.addEventListener("input", () => {
    userFilterKeyword = _userSearchInput.value || "";
    _renderFilteredUsers();
  });
}
bind("btnUserSearchClear", () => {
  if (_userSearchInput) _userSearchInput.value = "";
  userFilterKeyword = "";
  _renderFilteredUsers();
});

// ========== 工具页：全站广播（Markdown 编辑器） ==========
const _bcEditor = $("broadcastEditor");
const _bcPreview = $("broadcastPreview");

function refreshBroadcastPreview() {
  if (_bcPreview && _bcEditor) _bcPreview.innerHTML = renderMarkdown(_bcEditor.value || "");
}
// 编辑器输入时实时刷新预览（轻量节流，避免长文本频繁重排）
let _bcPreviewTimer = null;
if (_bcEditor) {
  _bcEditor.addEventListener("input", () => {
    if (_bcPreviewTimer) clearTimeout(_bcPreviewTimer);
    _bcPreviewTimer = setTimeout(refreshBroadcastPreview, 120);
  });
}

// 工具栏：在光标处插入 Markdown 片段
const _MD_TOOLS = {
  bold:   { wrap: ["**", "**"], ph: "加粗文字" },
  italic: { wrap: ["*", "*"],   ph: "斜体文字" },
  strike: { wrap: ["~~", "~~"], ph: "删除线" },
  code:   { wrap: ["`", "`"],   ph: "code" },
  h2:     { prefix: "## ", ph: "标题" },
  ul:     { prefix: "- ", ph: "列表项" },
  ol:     { prefix: "1. ", ph: "列表项" },
  quote:  { prefix: "> ", ph: "引用" },
  link:   { wrap: ["[", "](https://)"], ph: "链接文字" },
  hr:     { insert: "\n---\n" },
};
document.querySelectorAll("[data-md]").forEach(btn => {
  btn.addEventListener("click", () => {
    if (!_bcEditor) return;
    const kind = btn.dataset.md;
    const rule = _MD_TOOLS[kind];
    if (!rule) return;
    const start = _bcEditor.selectionStart || 0;
    const end = _bcEditor.selectionEnd || 0;
    const value = _bcEditor.value || "";
    const selected = value.slice(start, end) || rule.ph || "";
    let insertText, caretStart, caretEnd;

    if (rule.insert != null) {
      insertText = rule.insert;
      caretStart = caretEnd = start + insertText.length;
    } else if (rule.prefix != null) {
      // 行首前缀：定位到当前行开头插入
      const lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      insertText = rule.prefix + selected;
      _bcEditor.value = value.slice(0, lineStart) + insertText + value.slice(end);
      caretStart = lineStart + rule.prefix.length;
      caretEnd = caretStart + selected.length;
      _bcEditor.focus();
      _bcEditor.setSelectionRange(caretStart, caretEnd);
      refreshBroadcastPreview();
      return;
    } else {
      const [open, close] = rule.wrap;
      insertText = open + selected + close;
      caretStart = start + open.length;
      caretEnd = caretStart + selected.length;
    }

    _bcEditor.value = value.slice(0, start) + insertText + value.slice(end);
    _bcEditor.focus();
    _bcEditor.setSelectionRange(caretStart, caretEnd);
    refreshBroadcastPreview();
  });
});

async function loadBroadcast() {
  try {
    const r = await api("/api/broadcast");
    if (_bcEditor) _bcEditor.value = r.content || "";
    const cb = $("broadcastEnabled");
    if (cb) cb.checked = !!r.enabled;
    const st = $("broadcastStatus");
    if (st) {
      st.textContent = r.enabled
        ? `已启用${r.updated_at ? " · 更新于 " + fmtTime(r.updated_at) : ""}`
        : "未启用";
    }
    const msg = $("broadcastMsg");
    if (msg) msg.textContent = "";
    refreshBroadcastPreview();
  } catch (e) { showError(e.message); }
}

bind("btnBroadcastSave", async () => {
  const content = (_bcEditor && _bcEditor.value) || "";
  const enabled = !!($("broadcastEnabled") && $("broadcastEnabled").checked);
  if (enabled && !content.trim()) { showError("启用广播前请先填写内容"); return; }
  try {
    const r = await api("/api/broadcast", "POST", { enabled, content });
    if (r.ok) {
      const msg = $("broadcastMsg");
      if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] 已保存${r.enabled ? "并已发布给所有客户端" : "（未启用）"}`;
      loadBroadcast();
    }
  } catch (e) { showError(e.message); }
});

bind("btnBroadcastDisable", async () => {
  if (!confirm("确认关闭全站广播？客户端将不再弹窗（内容会保留，便于下次修改）。")) return;
  try {
    await api("/api/broadcast/disable", "POST", {});
    const msg = $("broadcastMsg");
    if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] 已关闭广播`;
    loadBroadcast();
  } catch (e) { showError(e.message); }
});

bind("btnBroadcastReload", () => loadBroadcast());

// ========== 工具页：实时广播（即时推送到在线用户） ==========
const _instEditor = $("instantEditor");
const _instPreview = $("instantPreview");

function refreshInstantPreview() {
  if (_instPreview && _instEditor) _instPreview.innerHTML = renderMarkdown(_instEditor.value || "");
}
let _instPreviewTimer = null;
if (_instEditor) {
  _instEditor.addEventListener("input", () => {
    if (_instPreviewTimer) clearTimeout(_instPreviewTimer);
    _instPreviewTimer = setTimeout(refreshInstantPreview, 120);
  });
}
document.querySelectorAll("[data-md-inst]").forEach(btn => {
  btn.addEventListener("click", () => {
    if (!_instEditor) return;
    const kind = btn.dataset.mdInst;
    const rule = _MD_TOOLS[kind];
    if (!rule) return;
    const start = _instEditor.selectionStart || 0;
    const end = _instEditor.selectionEnd || 0;
    const value = _instEditor.value || "";
    const selected = value.slice(start, end) || rule.ph || "";
    let insertText, caretStart, caretEnd;
    if (rule.insert != null) {
      insertText = rule.insert;
      caretStart = caretEnd = start + insertText.length;
    } else if (rule.prefix != null) {
      const lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      insertText = rule.prefix + selected;
      _instEditor.value = value.slice(0, lineStart) + insertText + value.slice(end);
      caretStart = lineStart + rule.prefix.length;
      caretEnd = caretStart + selected.length;
      _instEditor.focus();
      _instEditor.setSelectionRange(caretStart, caretEnd);
      refreshInstantPreview();
      return;
    } else {
      const [open, close] = rule.wrap;
      insertText = open + selected + close;
      caretStart = start + open.length;
      caretEnd = caretStart + selected.length;
    }
    _instEditor.value = value.slice(0, start) + insertText + value.slice(end);
    _instEditor.focus();
    _instEditor.setSelectionRange(caretStart, caretEnd);
    refreshInstantPreview();
  });
});

bind("btnInstantSend", async () => {
  const content = (_instEditor && _instEditor.value || "").trim();
  if (!content) { showError("请输入要实时广播的 Markdown 内容"); return; }
  if (!confirm("确认立即向所有在线客户端发送实时广播？\n在线用户将立即弹窗收到该消息（仅限实时这一次，刷新不存）。")) return;
  try {
    const r = await api("/api/broadcast/instant", "POST", { content });
    if (r.ok) {
      const msg = $("instantMsg");
      if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] ${r.message || "已向在线客户端发送"}`;
      setGlobalStatus("实时广播已发送");
    }
  } catch (e) { showError(e.message); }
});

bind("btnInstantClear", () => {
  if (_instEditor) _instEditor.value = "";
  refreshInstantPreview();
  const msg = $("instantMsg");
  if (msg) msg.textContent = "";
});

// ========== 工具页：IP 访问控制（黑白名单） ==========
let cachedIpRules = { whitelist: [], blacklist: [], current_ip: "" };

function renderIpList(boxId, list, type) {
  const box = $(boxId);
  if (!box) return;
  box.innerHTML = "";
  if (!list || !list.length) {
    box.innerHTML = `<div class="empty">暂无${type === "whitelist" ? "白名单" : "黑名单"} IP</div>`;
    return;
  }
  list.forEach(ip => {
    const item = document.createElement("div");
    item.className = "ip-tag-item" + (ip === cachedIpRules.current_ip ? " is-current" : "");
    item.innerHTML = `
      <span>${escapeHtml(ip)}${ip === cachedIpRules.current_ip ? ' <span class="badge" style="font-size:10px;">当前</span>' : ''}</span>
      <button class="btn-remove-ip" title="移除此 IP">✕</button>
    `;
    item.querySelector(".btn-remove-ip").addEventListener("click", () => removeIp(type, ip));
    box.appendChild(item);
  });
}

async function loadIpRules() {
  try {
    const r = await api("/api/ip_rules");
    cachedIpRules = r;
    const wl = r.whitelist || [];
    const bl = r.blacklist || [];
    renderIpList("whitelistBox", wl, "whitelist");
    renderIpList("blacklistBox", bl, "blacklist");
    const wCnt = $("whitelistCount"); if (wCnt) wCnt.textContent = wl.length;
    const bCnt = $("blacklistCount"); if (bCnt) bCnt.textContent = bl.length;
    const curIpEl = $("currentAdminIp");
    if (curIpEl) {
      curIpEl.textContent = r.current_ip || "--";
      curIpEl.style.cursor = "pointer";
      curIpEl.title = "点击填入白名单输入框";
      curIpEl.onclick = () => {
        const inp = $("inputWhitelistIp");
        if (inp && r.current_ip) { inp.value = r.current_ip; inp.focus(); }
      };
    }
  } catch (e) { showError(e.message); }
}

async function addIp(type) {
  const inp = $(type === "whitelist" ? "inputWhitelistIp" : "inputBlacklistIp");
  const ip = (inp && inp.value || "").trim();
  if (!ip) { showError("请输入有效的 IP 地址"); return; }
  try {
    const r = await api("/api/ip_rules", "POST", { action: "add", type, ip });
    if (r.ok) {
      if (inp) inp.value = "";
      loadIpRules();
      const msg = $("ipRulesActionMsg");
      if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] 已将 ${ip} 加入${type === "whitelist" ? "白名单" : "黑名单"}`;
    }
  } catch (e) { showError(e.message); }
}

async function removeIp(type, ip) {
  if (!confirm(`确认将 ${ip} 从${type === "whitelist" ? "白名单" : "黑名单"}中移除？`)) return;
  try {
    const r = await api("/api/ip_rules", "POST", { action: "remove", type, ip });
    if (r.ok) {
      loadIpRules();
      const msg = $("ipRulesActionMsg");
      if (msg) msg.textContent = `[${new Date().toLocaleTimeString()}] 已移除 ${ip}`;
    }
  } catch (e) { showError(e.message); }
}

bind("btnAddWhitelistIp", () => addIp("whitelist"));
bind("btnAddBlacklistIp", () => addIp("blacklist"));
bind("btnReloadIpRules", () => loadIpRules());

const _inputWl = $("inputWhitelistIp");
if (_inputWl) _inputWl.addEventListener("keydown", (e) => { if (e.key === "Enter") addIp("whitelist"); });
const _inputBl = $("inputBlacklistIp");
if (_inputBl) _inputBl.addEventListener("keydown", (e) => { if (e.key === "Enter") addIp("blacklist"); });

// ========== 初始化 ==========
(async function init() {
  applyTheme();
  bind("btnTheme", toggleTheme);
  layoutNav();
  connectWs();
  loadLogs();
  refreshStats();
  loadUsers();
  loadBroadcast();
  loadIpRules();
  tickClock();
  // 时钟每秒本地平滑走动（不依赖接口）
  setInterval(tickClock, 1000);
  // 实时刷新：总览与用户页每秒刷新统计/用户列表（用户详情随列表一并刷新）
  setInterval(() => {
    if (state.activeTab === "overview" || state.activeTab === "users") {
      refreshStats();
      loadUsers();
    }
  }, 1000);
})();
