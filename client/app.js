/* gateway/client/app.js —— Nascence辉夜 对话客户端
   集成原导航站（nav_script.js 原封不动）+ 标签卡对话窗 + 登录门禁
   主题与导航站统一（nav_settings + body.dark-mode）
   发送用 Enter，换行用 Shift+Enter / Ctrl+Enter */
(function () {
    "use strict";

    // ========== 全局状态 ==========
    // 网关地址解析（优先级）：
    //   1. 浏览器环境（HTTP/HTTPS 访问）：默认直接使用当前源 window.location.origin，天然支持域名反代与自定义端口
    //   2. 本地文件直接打开（file://）：取 localStorage 记忆的上次网关地址 -> client_config.json -> 默认 http://127.0.0.1:8987
    const DEFAULT_GATEWAY = "http://127.0.0.1:8987";
    const LAST_GATEWAY_KEY = "gateway_last";

    function getInitialGateway() {
        if (typeof window !== "undefined" && window.location && window.location.protocol.startsWith("http")) {
            return window.location.origin;
        }
        try {
            const raw = localStorage.getItem(LAST_GATEWAY_KEY);
            if (raw) {
                const g = String(raw).trim();
                if (/^https?:\/\/.+/.test(g)) return g;
            }
        } catch (e) { /* ignore */ }
        return DEFAULT_GATEWAY;
    }

    function saveLastGateway(g) {
        try { localStorage.setItem(LAST_GATEWAY_KEY, String(g)); } catch (e) { /* ignore */ }
    }

    // 本地持久化缓存读写（防止刷新时内存被重置为 [] 导致消息列表或历史闪烁变空）
    function _loadLocalCache(key, fallback) {
        try {
            const raw = localStorage.getItem(key);
            if (raw) return JSON.parse(raw);
        } catch (e) { /* ignore */ }
        return fallback;
    }
    function _saveLocalCache(key, val) {
        try {
            localStorage.setItem(key, JSON.stringify(val));
        } catch (e) { /* ignore */ }
    }

    const state = {
        gateway: DEFAULT_GATEWAY,
        token: localStorage.getItem("gateway_token") || "",
        // 每会话签名密钥：由服务端在登录/注册响应中下发，仅对该会话 token 有效。
        // 源码中不保存任何硬编码密钥；缺失时跳过签名头（服务端兼容仅 Bearer 认证）。
        signKey: localStorage.getItem("gateway_signkey") || "",
        username: localStorage.getItem("gateway_user") || "",
        loggedIn: false, // 安全优先：默认未登录，严禁前端盲目放行，必须经服务端核验有效才置为 true
        role: localStorage.getItem("gateway_role") || "student",
        userId: localStorage.getItem("gateway_userid") || "",
        displayName: "",  // 家长/老师显示名：学生名（关系），如"小明的妈妈"；学生端为空(用账号名)
        linkedStudents: [],
        currentMemoryUser: "",  // 当前对话使用的记忆体 user_id（家长/老师切换绑定学生）
        minorMode: false,       // 学生是否处于未成年模式
        pendingChats: 0,
        botName: "辉夜",
        botAvatar: "",               // 机器人头像 URL（登录后由 /gateway/bot 提供，可为空）
        botAvatarDefault: "",        // 机器人默认头像 URL（无上传图时兜底）
        userAvatar: "",              // 当前登录用户头像 URL（未上传时为空，回退字母）
        authMode: "login",
        navOpen: false,
        theme: "follow",
        currentChat: null,           // 当前打开的会话 { chat_type: "bot"|"user"|"group", target: "...", name: "...", ... }
        // 关键防护：列表先从 localStorage 缓存恢复，页面刷新瞬间呈现，绝不白屏、绝不出现空列表！
        conversations: _loadLocalCache("cache_conversations", []),
        friends: _loadLocalCache("cache_friends", []),
        groups: _loadLocalCache("cache_groups", []),
        currentTab: "convs",         // "convs" | "friends" | "groups"
        unreadCounts: {},            // 会话 target -> 未读数（本次会话内实时累加；打开后清零）
        readPositions: _loadLocalCache("cache_read_positions", {}), // 会话 key -> 上次已读时间戳
        newDividerTs: null,          // 打开会话时记录已读位置，供渲染"以下为新消息"分隔
        pendingRequestsCount: 0,     // 好友待处理申请数
        currentQuote: null,          // 当前正在引用的消息对象 { id, sender, content, mtype, image }
        cachedGroupMembers: {},      // group_id -> members 列表缓存
    };

    // 会话请求票据（Request Ticket）：防止网络乱序或快速点击切换导致旧请求覆盖新聊天历史
    let currentChatTicket = 0;

    // 历史消息分页状态（向上滚动加载更早的 30 条，支持 bot/user/group 全类型）
    let historyHasMore = false;    // 是否还有更早的历史
    let historyOldestTime = null;  // 已加载最旧一条的 time（滚动加载的 before 游标）
    let historyLoadingMore = false; // 防止重复触发向上加载

    function $(id) { return document.getElementById(id); }

    // 复制文本到剪贴板：优先 Clipboard API，不可用（非安全上下文/旧浏览器）时回退 execCommand
    function copyText(text) {
        if (!text) return;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
                return;
            }
        } catch (e) { /* 落到回退 */ }
        fallbackCopy(text);
    }
    function fallbackCopy(text) {
        try {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.top = "-9999px";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
        } catch (e) { /* 忽略 */ }
    }
    const authOverlay = $("auth-overlay");
    const navMask = $("nav-mask");
    const navStation = $("nav-station");
    const logoContainer = $("logo-container");
    const accountOverlay = $("account-overlay");
    const privacyOverlay = $("privacy-overlay");
    const privacyCard = $("privacy-card");
    const adsOverlay = $("ads-overlay");
    const adsCard = $("ads-card");
    const ageOverlay = $("age-overlay");
    const ageCard = $("age-card");

    function escapeHtml(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br>");
    }
    function isMobile() { return window.innerWidth <= 768; }

    // ========== 引用（Quote）与 @（Mention）管理 ==========
    function setQuote(msg, rowEl) {
        if (!msg) return;
        // ID 以 DOM 上的权威值为准：本地乐观气泡发送后会由临时 ID 转正为服务端真实 ID，
        // 消息对象里的 id 不会同步更新，直接取 msg.id 会导致引用卡片跳转失效。
        const domId = (rowEl && rowEl.dataset && rowEl.dataset.msgId)
            || (typeof msg.closest === "function" && msg.closest(".msg-row") && msg.closest(".msg-row").dataset.msgId)
            || "";
        const qId = domId || msg.id || "";
        const qSender = msg.sender || "";
        const qContent = msg.content || msg.text || "";
        const qType = msg.mtype || (msg.image ? "image" : "text");
        const qImage = msg.image || "";

        // 覆盖上次引用（原子替换，绝不叠加，符合用户多次引用直接覆盖的要求）
        state.currentQuote = {
            id: qId,
            sender: qSender,
            content: qContent,
            mtype: qType,
            image: qImage,
            sticker_name: msg.sticker_name || "",
        };

        const bar = $("chatQuoteBar");
        const sEl = $("quoteBarSender");
        const tEl = $("quoteBarText");
        if (bar && sEl && tEl) {
            sEl.textContent = `回复 ${qSender || "消息"}：`;
            if (qType === "sticker" || qType === "image" || (qImage && /^\/(stickers|chat_images)\//.test(qImage))) {
                // 交给统一helper生成，避免出现"[表情包] [表情包] 名字"这类重复前缀
                tEl.textContent = quoteMediaLabel(state.currentQuote);
            } else {
                tEl.textContent = qContent;
            }
            bar.style.display = "flex";
        }
        const ci = $("chatInput");
        if (ci) ci.focus();
    }

    // 若引用目标仍是本地临时 ID，在气泡转正后用真实 ID 回填引用（避免跳转失效）
    function syncQuoteId(oldId, newId) {
        if (!oldId || !newId || !state.currentQuote) return;
        if (state.currentQuote.id === oldId) {
            state.currentQuote.id = newId;
        }
    }

    function clearQuote() {
        state.currentQuote = null;
        const bar = $("chatQuoteBar");
        if (bar) bar.style.display = "none";
    }

    // 引用跳转：按 id 找回原文；id 失配时回退用「发送者 + 内容」定位。
    // 为什么需要回退：历史消息的 id 由服务端生成，旧版本服务端的 id 含页内下标，
    // 新增消息后会整体位移，导致老引用卡里存的 id 找不到原文。内容匹配可兜住这类历史数据。
    function findMessageRow(targetId, quoteSender, quoteContent) {
        const view = $("chatView");
        if (!view) return null;
        if (targetId) {
            // CSS.escape 在旧浏览器可能缺失，缺失时退回属性选择器的转义写法
            const esc = (window.CSS && CSS.escape)
                ? CSS.escape(String(targetId))
                : String(targetId).replace(/["\\]/g, "\\$&");
            const byId = view.querySelector(`[data-msg-id="${esc}"]`);
            if (byId) return byId;
        }
        // 回退：内容比对（优先精确匹配，其次去空白/去引用前缀后匹配）
        const want = String(quoteContent || "").trim();
        if (!want) return null;
        const rows = Array.from(view.querySelectorAll(".msg-row"));
        const norm = (s) => String(s || "").replace(/\s+/g, "");
        const wantNorm = norm(want.replace(/^\[(表情包|图片)\]\s*/, ""));
        const senderOk = (row) => {
            if (!quoteSender) return true;
            const t = row.querySelector(".msg-text");
            // 自己的消息行没有 sender-name 标签，用气泡方向（user 类）判断
            const isSelfRow = row.classList.contains("user");
            if (isSelfRow) return quoteSender === state.username || quoteSender === state.displayName;
            const tag = row.querySelector(".sender-name");
            if (tag) return tag.textContent === quoteSender;
            return true;   // 单聊/机器人没有 sender-name，放宽发送者约束
        };
        // 一轮：精确内容匹配
        for (const row of rows) {
            const t = row.querySelector(".msg-text");
            if (t && t.textContent.trim() === want && senderOk(row)) return row;
        }
        // 二轮：归一化内容匹配（忽略空白与 [表情包]/[图片] 前缀差异）
        for (const row of rows) {
            const t = row.querySelector(".msg-text");
            if (t && norm(t.textContent) === wantNorm && senderOk(row)) return row;
        }
        return null;
    }

    function jumpToQuotedMessage(targetId, quoteSender, quoteContent) {
        const view = $("chatView");
        if (!view) return;
        const targetRow = findMessageRow(targetId, quoteSender, quoteContent);
        if (targetRow) {
            targetRow.scrollIntoView({ behavior: "smooth", block: "center" });
            targetRow.classList.remove("msg-flash-highlight");
            void targetRow.offsetWidth;   // 强制重排，让动画可重复触发
            targetRow.classList.add("msg-flash-highlight");
            if (jumpToQuotedMessage._timer) clearTimeout(jumpToQuotedMessage._timer);
            jumpToQuotedMessage._timer = setTimeout(() => {
                targetRow.classList.remove("msg-flash-highlight");
            }, 2000);
        } else {
            const tip = document.createElement("div");
            tip.className = "quote-miss-tip";
            tip.textContent = "原消息在更早的历史记录中，请向上滑动查看";
            view.appendChild(tip);
            setTimeout(() => tip.remove(), 2500);
        }
    }

    function insertMention(username) {
        if (!username) return;
        const input = $("chatInput");
        if (!input) return;
        const mentionStr = `@${username} `;
        const pos = (input.selectionStart != null) ? input.selectionStart : input.value.length;
        const val = input.value;
        input.value = val.slice(0, pos) + mentionStr + val.slice(pos);
        input.focus();
        const newPos = pos + mentionStr.length;
        input.setSelectionRange(newPos, newPos);
        closeMentionPicker();
    }

    function formatMessageTextWithMentions(container, text) {
        if (!text) return;
        const regex = /(@[^\s@]+)/g;
        const parts = text.split(regex);
        const myName = state.username || "";
        const myDisp = state.displayName || "";

        parts.forEach(part => {
            if (part.startsWith("@") && part.length > 1) {
                const name = part.slice(1);
                const span = document.createElement("span");
                span.className = "mention-tag";
                if (name === "全体成员" || name === myName || (myDisp && name === myDisp)) {
                    span.classList.add("mention-me");
                }
                span.textContent = part;
                span.title = `点击@ ${name}`;
                span.addEventListener("click", (e) => {
                    e.stopPropagation();
                    insertMention(name);
                });
                container.appendChild(span);
            } else if (part) {
                container.appendChild(document.createTextNode(part));
            }
        });
    }

    let mentionCandidates = [];
    let mentionSelectedIndex = 0;
    let mentionMatchStart = -1;

    async function getCandidatesForMention() {
        if (!state.currentChat) return [];
        if (state.currentChat.chat_type === "group") {
            const gid = state.currentChat.target;
            let members = state.cachedGroupMembers[gid];
            if (!members) {
                try {
                    const r = await gatewayApi(`/gateway/groups/detail?group_id=${encodeURIComponent(gid)}`, "GET");
                    if (r.ok && r.group && r.group.members) {
                        members = r.group.members;
                        state.cachedGroupMembers[gid] = members;
                    }
                } catch (e) {}
            }
            const list = [{ username: "全体成员", isAll: true }];
            if (members && members.length) {
                members.forEach(m => {
                    if (m.username && m.username !== state.username) {
                        list.push({ username: m.username });
                    }
                });
            }
            return list;
        } else if (state.currentChat.chat_type === "bot") {
            return [{ username: state.botName }];
        } else if (state.currentChat.chat_type === "user") {
            const u = state.currentChat.name || state.currentChat.target;
            return [{ username: u }];
        }
        return [];
    }

    function renderMentionPicker(candidates) {
        const p = $("mentionPicker");
        if (!p) return;
        if (!candidates.length) {
            p.style.display = "none";
            return;
        }
        p.innerHTML = "";
        candidates.forEach((c, idx) => {
            const item = document.createElement("div");
            item.className = "mention-item" + (idx === mentionSelectedIndex ? " active" : "");
            item.innerHTML = `
                <div class="mention-item-avatar">${c.isAll ? "📢" : (c.username.slice(0, 1).toUpperCase())}</div>
                <div class="mention-item-name">${escapeHtml(c.username)}</div>
                ${c.isAll ? '<span class="mention-item-tag">全体</span>' : ""}
            `;
            item.addEventListener("mousedown", (e) => {
                e.preventDefault();
                applyMentionSelection(c.username);
            });
            p.appendChild(item);
        });
        p.style.display = "block";
    }

    function updateMentionPickerSelection() {
        const p = $("mentionPicker");
        if (!p) return;
        const items = p.querySelectorAll(".mention-item");
        items.forEach((it, idx) => {
            it.classList.toggle("active", idx === mentionSelectedIndex);
            if (idx === mentionSelectedIndex) {
                it.scrollIntoView({ block: "nearest" });
            }
        });
    }

    function closeMentionPicker() {
        mentionCandidates = [];
        mentionSelectedIndex = 0;
        mentionMatchStart = -1;
        const p = $("mentionPicker");
        if (p) p.style.display = "none";
    }

    function applyMentionSelection(name) {
        const input = $("chatInput");
        if (!input) return;
        const text = input.value;
        const pos = (input.selectionStart != null) ? input.selectionStart : text.length;
        const start = mentionMatchStart >= 0 ? mentionMatchStart : (text.slice(0, pos).lastIndexOf("@"));
        if (start === -1) {
            insertMention(name);
            return;
        }
        const before = text.slice(0, start);
        const after = text.slice(pos);
        const insertText = `@${name} `;
        input.value = before + insertText + after;
        const newPos = before.length + insertText.length;
        input.setSelectionRange(newPos, newPos);
        input.focus();
        closeMentionPicker();
    }

    async function checkMentionTrigger() {
        const input = $("chatInput");
        if (!input || !state.currentChat) {
            closeMentionPicker();
            return;
        }
        const text = input.value;
        const pos = (input.selectionStart != null) ? input.selectionStart : text.length;
        const left = text.slice(0, pos);
        const atIdx = left.lastIndexOf("@");
        if (atIdx === -1) {
            closeMentionPicker();
            return;
        }
        const query = left.slice(atIdx + 1);
        if (/[\s]/.test(query)) {
            closeMentionPicker();
            return;
        }
        if (atIdx > 0 && !/[\s\n,，.。!！?？]/.test(left[atIdx - 1])) {
            closeMentionPicker();
            return;
        }

        mentionMatchStart = atIdx;
        const allCandidates = await getCandidatesForMention();
        const filtered = allCandidates.filter(c => {
            if (!query) return true;
            return (c.username || "").toLowerCase().includes(query.toLowerCase());
        });
        mentionCandidates = filtered;
        mentionSelectedIndex = 0;
        renderMentionPicker(filtered);
    }

    // ========== 用户头像：统一走 /images/user_avatars/<username>.png，失败回退字母 ==========
    // 后端上传后即为固定路径；未上传时该 URL 返回 404，由 onerror 回退首字母。
    // _avatarVer 为本次会话的头像版本号（上传后自增），用于强制刷新缓存。
    let _avatarVer = 0;
    function userAvatarUrl(username) {
        if (!username) return "";
        const v = (username === state.username) ? _avatarVer : 0;
        const base = `/images/user_avatars/${encodeURIComponent(username)}.png`;
        return v ? `${base}?v=${v}` : base;
    }
    // 填充一个头像容器：优先图片，失败/无用户名时显示字母占位。
    // 占位字母用 .avatar-initial 绝对定位铺满，与 <img> 不并排——
    // 否则 flex 容器内"字母 + 图片"同排，图片加载完成移除字母时会产生布局抽动。
    function fillUserAvatar(el, username, fallbackText) {
        if (!el) return;
        const txt = (fallbackText || username || "?").slice(0, 1).toUpperCase();
        el.innerHTML = "";
        const initial = document.createElement("span");
        initial.className = "avatar-initial";
        initial.textContent = txt;
        el.appendChild(initial);
        if (!username) return;
        const img = document.createElement("img");
        img.className = "msg-avatar-img";
        img.alt = "";
        img.onload = () => { if (initial.parentNode) initial.remove(); };
        img.onerror = () => { img.onerror = null; if (img.parentNode) img.remove(); };
        img.src = userAvatarUrl(username);
        el.appendChild(img);
    }

    // ========== 主题：与原导航站统一（nav_settings + body.dark-mode） ==========
    const THEME_NAMES = { follow: "跟随系统", light: "浅色模式", dark: "深色模式" };
    function readThemeFromNav() {
        try {
            const raw = localStorage.getItem("nav_settings");
            if (raw) {
                const d = JSON.parse(raw);
                if (d.themeMode) state.theme = d.themeMode;
            }
        } catch (e) { /* default follow */ }
    }
    function applyTheme() {
        const isDark = state.theme === "dark" ||
            (state.theme === "follow" && window.matchMedia("(prefers-color-scheme: dark)").matches);
        document.body.classList.toggle("dark-mode", isDark);
        $("btnTheme").textContent = THEME_NAMES[state.theme] || "跟随系统";
    }
    function saveThemeToNav() {
        try {
            const raw = localStorage.getItem("nav_settings");
            const d = raw ? JSON.parse(raw) : {};
            d.themeMode = state.theme;
            localStorage.setItem("nav_settings", JSON.stringify(d));
        } catch (e) { /* ignore */ }
    }
    function toggleTheme() {
        const cycle = { follow: "light", light: "dark", dark: "follow" };
        state.theme = cycle[state.theme] || "follow";
        saveThemeToNav();
        applyTheme();
    }
    $("btnTheme").addEventListener("click", toggleTheme);

    // ========== 顶栏头像 → 账号信息卡片（导航站同款标签卡 + 同套滑入/滑出动画） ==========
    let isClosingAccount = false;
    function openAccountCard() {
        if (!state.loggedIn) { showAuthOverlay(); return; }
        if (!accountOverlay) return;
        isClosingAccount = false;
        renderAccountMenu();
        // 激活背景遮罩（淡入纯色遮罩，遮挡底层对话页）
        const navMask = $("nav-mask");
        if (navMask) navMask.classList.add("nav-open");
        // 显示导航站同款「返回对话页」按钮
        const backBtn = $("btnBackChat");
        if (backBtn) backBtn.classList.add("visible");
        const card = $("account-card");
        if (card) card.classList.remove("account-closing");
        accountOverlay.classList.add("open");
        requestAnimationFrame(() => {
            initAccountPixelGrid();
        });
    }
    function closeAccountCard() {
        if (!accountOverlay || isClosingAccount) return;
        if (!accountOverlay.classList.contains("open")) return;
        isClosingAccount = true;
        // 收起动画：向右上方弹性反向收起
        const card = $("account-card");
        if (card) card.classList.add("account-closing");
        const navMask = $("nav-mask");
        if (navMask && !state.navOpen) navMask.classList.remove("nav-open");
        const backBtn = $("btnBackChat");
        if (backBtn && !state.navOpen) backBtn.classList.remove("visible");
        setTimeout(() => {
            accountOverlay.classList.remove("open");
            if (card) card.classList.remove("account-closing");
            isClosingAccount = false;
        }, 340);
    }
    $("userAvatar").addEventListener("click", (e) => {
        e.stopPropagation();
        if (accountOverlay && accountOverlay.classList.contains("open")) {
            closeAccountCard();
        } else {
            openAccountCard();
        }
    });

    // ========== 配置：读取网关地址 ==========
    async function loadConfig() {
        if (typeof window !== "undefined" && window.location && window.location.protocol.startsWith("http")) {
            state.gateway = window.location.origin;
            return;
        }
        try {
            const cfg = await (await fetch("client_config.json")).json();
            if (cfg && cfg.gateway && !localStorage.getItem(LAST_GATEWAY_KEY)) {
                state.gateway = cfg.gateway;
            }
        } catch (e) { /* 保持当前 gateway */ }
    }

    // ========== 网关请求 ==========
    // 安全机制：
    //   - Token 绝不放入 URL，改由 Authorization: Bearer <token> Header 携带
    //   - 附带 X-Timestamp / X-Nonce / X-Sign 签名头，防止重放攻击
    async function sha256Hex(text) {
        const enc = new TextEncoder().encode(text);
        const buf = await crypto.subtle.digest("SHA-256", enc);
        return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("");
    }
    async function computeSign(username, ts, nonce) {
        // 与后端 hmac_sha256(session_sign_key, f"{username}|{ts}|{nonce}") 一致
        if (!state.signKey) throw new Error("no sign key");
        const key = await crypto.subtle.importKey(
            "raw", new TextEncoder().encode(state.signKey),
            { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
        );
        const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${username}|${ts}|${nonce}`));
        return Array.from(new Uint8Array(sig)).map(b => b.toString(16).padStart(2, "0")).join("");
    }
    async function gatewayApi(path, method = "GET", body = null) {
        const url = state.gateway.replace(/\/+$/, "") + path;
        const opts = { method, headers: {} };
        if (state.token && !path.includes("/login") && !path.includes("/register")) {
            opts.headers["Authorization"] = `Bearer ${state.token}`;
            // 仅在 username 已知时才发送签名头：刷新后 username 未恢复时若强行签名，
            // 会用错误 username 生成签名导致后端 403 拒绝，登录界面无法隐藏。
            if (state.username) {
                const ts = String(Math.floor(Date.now() / 1000));  // 秒级时间戳
                const nonce = Math.random().toString(36).slice(2) + Date.now().toString(36);
                try {
                    const sign = await computeSign(state.username, ts, nonce);
                    opts.headers["X-Timestamp"] = ts;
                    opts.headers["X-Nonce"] = nonce;
                    opts.headers["X-Sign"] = sign;
                } catch (e) { /* crypto 不可用时仅携带 token */ }
            }
        }
        if (body !== null) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(url, opts);
        const data = await resp.json().catch(() => ({}));
        // 请求成功即记住当前端口（作为"上次访问的端口号"，下次启动优先使用）
        if (resp.ok) saveLastGateway(state.gateway);
        if (!resp.ok) {
            // 仅 401（会话失效/被服务端判定未登录）才清除登录态；
            // 429 限流 / 403 签名 / 5xx 等其它错误绝不登出，避免刷新时被误登出。
            if (resp.status === 401 && state.loggedIn) {
                doLogout("登录状态已过期，请重新登录。");
            }
            if (resp.status === 403 && state.signKey) {
                // 服务端重装/轮换 sign_secret 后，旧签名密钥会失效。
                // 清除本地密钥，让后续请求回退到仅 Bearer 认证（服务端兼容该模式）。
                state.signKey = "";
                try { localStorage.removeItem("gateway_signkey"); } catch (e) {}
            }
            throw new Error(data.message || `HTTP ${resp.status}`);
        }
        return data;
    }

    // ========== 密码显示 / 隐藏 ==========
    document.querySelectorAll(".btn-pwd-eye").forEach(btn => {
        btn.addEventListener("click", () => {
            const targetId = btn.dataset.target;
            const input = $(targetId);
            if (!input) return;
            const isPwd = input.type === "password";
            input.type = isPwd ? "text" : "password";
            const icon = btn.querySelector(".eye-icon");
            if (icon) icon.textContent = isPwd ? "🙈" : "👁️";
        });
    });

    // ========== 登录门禁 ==========
    function requireAuth() {
        if (!state.loggedIn) { showAuthOverlay(); return false; }
        return true;
    }
    function showAuthOverlay() {
        closeAccountCard();
        authOverlay.classList.add("active");
        authOverlay.style.display = "flex";
        setTimeout(() => $("authUsername").focus(), 60);
    }
    function hideAuthOverlay() {
        authOverlay.classList.remove("active");
        // 双保险：内联强制隐藏，杜绝任何 CSS 残留导致登录界面卡在屏幕上
        authOverlay.style.display = "none";
    }

    $("tabLogin").addEventListener("click", () => {
        state.authMode = "login";
        $("tabLogin").classList.add("active");
        $("tabRegister").classList.remove("active");
        $("btnAuthSubmit").textContent = "登 录";
        const badge = $("authModeBadge");
        if (badge) badge.textContent = "登录";
        const confirmField = $("fieldConfirmPassword");
        if (confirmField) confirmField.style.display = "none";
        const roleField = $("fieldRole");
        if (roleField) roleField.style.display = "none";
        $("authHint").textContent = "";
    });
    $("tabRegister").addEventListener("click", () => {
        state.authMode = "register";
        $("tabRegister").classList.add("active");
        $("tabLogin").classList.remove("active");
        $("btnAuthSubmit").textContent = "注 册";
        const badge = $("authModeBadge");
        if (badge) badge.textContent = "注册";
        const confirmField = $("fieldConfirmPassword");
        if (confirmField) confirmField.style.display = "block";
        const roleField = $("fieldRole");
        if (roleField) roleField.style.display = "block";
        $("authHint").textContent = "";
    });

    function getSelectedRole() {
        const el = document.querySelector('input[name="authRole"]:checked');
        return el ? el.value : "student";
    }

    async function doAuth() {
        const gwElem = $("gwInput");
        if (gwElem && gwElem.value.trim()) state.gateway = gwElem.value.trim();
        const username = $("authUsername").value.trim();
        const password = $("authPassword").value;
        const confirmPassword = $("authConfirmPassword") ? $("authConfirmPassword").value : "";

        // 登录与注册都必须勾选同意《隐私政策》
        const consent = $("authConsent");
        if (consent && !consent.checked) {
            $("authHint").textContent = "请先勾选同意《隐私政策》";
            return;
        }

        if (!username || !password) {
            $("authHint").textContent = "请填写用户名和密码。";
            return;
        }

        if (state.authMode === "register") {
            if (!confirmPassword) {
                $("authHint").textContent = "请再次输入密码以确认。";
                return;
            }
            if (password !== confirmPassword) {
                $("authHint").textContent = "两次输入的密码不一致，请重新核对。";
                return;
            }
            // 学生注册：先弹年龄确认标签卡（中断提交，年龄卡确认后自动继续）
            if (getSelectedRole() === "student") {
                $("authHint").textContent = "";
                startAgeConfirmation({ username, password, confirmPassword, role: "student" });
                return;
            }
        }

        const btn = $("btnAuthSubmit");
        btn.disabled = true;
        try {
            const body = { username, password };
            if (state.authMode === "register") body.role = getSelectedRole();
            const r = await gatewayApi(`/gateway/${state.authMode}`, "POST", body);
            if (!r.ok) throw new Error(r.message || "操作失败");
            state.loggedIn = true;
            state.token = r.token;
            state.username = r.username;
            state.signKey = r.sign_key || "";
            state.role = r.role || "student";
            state.userId = r.user_id || "";
            state.linkedStudents = r.linked_students || [];
            state.minorMode = r.minor_mode || false;
            // 头像随登录结果重置（未上传时为空 → 回调退字母），
            // 避免沿用上一位登录者遗留在内存中的 state.userAvatar
            state.userAvatar = r.avatar || "";
            _avatarVer = 0;
            localStorage.setItem("gateway_token", r.token);
            localStorage.setItem("gateway_user", r.username);
            localStorage.setItem("gateway_role", state.role);
            localStorage.setItem("gateway_userid", state.userId);
            if (state.signKey) localStorage.setItem("gateway_signkey", state.signKey);
            else localStorage.removeItem("gateway_signkey");
            const authHint = $("authHint");
            if (authHint) authHint.textContent = "";
            await enterChat();
        } catch (e) {
            $("authHint").textContent = e.message;
        } finally {
            btn.disabled = false;
        }
    }
    $("btnAuthSubmit").addEventListener("click", doAuth);
    $("authPassword").addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth(); });
    const confirmInput = $("authConfirmPassword");
    if (confirmInput) {
        confirmInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth(); });
    }

    // ========== 对话页 ==========
    const ROLE_NAMES = { student: "学生端", parent: "家长端", teacher: "老师端" };
    // 应用头像：有图片则整体替换为单个 <img>，否则替换为单个字母文本。
    // 不叠加图层，避免容器内残留元素导致错位/露层。
    // 账号页可传入已存在的 imgEl/textEl（如 accountAvatarImg/accountAvatarText），
    // 顶栏等无子元素 id 的容器则整体重建 innerHTML。
    function applyAvatarTo(container, imgEl, textEl, initial) {
        if (!container) return;
        const hasAvatar = !!state.userAvatar;
        if (imgEl || textEl) {
            // 账号页：已有明确子元素，仅切换 display，保持结构
            if (hasAvatar) {
                if (imgEl) { imgEl.src = state.userAvatar; imgEl.style.display = "block"; }
                if (textEl) textEl.style.display = "none";
            } else {
                if (imgEl) imgEl.style.display = "none";
                if (textEl) { textEl.style.display = ""; textEl.textContent = initial; }
            }
            return;
        }
        // 顶栏等无子元素 id 的容器：整体重建，只保留一层
        if (hasAvatar) {
            const img = document.createElement("img");
            img.className = "avatar-img";
            img.alt = "";
            img.src = state.userAvatar;
            img.style.display = "block";
            container.replaceChildren(img);
        } else {
            container.replaceChildren(document.createTextNode(initial));
        }
    }

    function renderAccountMenu() {
        // 头像与用户名：家长/老师用"学生名（关系）"，学生用账号名（原账号名只用于登录）
        const display = state.displayName || state.username || "?";
        const initial = display.slice(0, 1).toUpperCase();
        applyAvatarTo($("userAvatar"), null, null, initial);
        const accAvatar = $("accountAvatar");
        if (accAvatar) applyAvatarTo(accAvatar, $("accountAvatarImg"), $("accountAvatarText"), initial);
        $("userName").textContent = display;
        const roleLabel = ROLE_NAMES[state.role] || state.role || "学生端";
        const roleEl = $("userRole");
        if (roleEl) roleEl.textContent = `当前端：${roleLabel}`;
        const accRoleTag = $("accountUserRole");
        if (accRoleTag) accRoleTag.textContent = roleLabel;
        // user_id：三端都始终显示（让学生能把 ID 给家长/老师绑定，家长老师也能看自己的 ID）
        const uidEl = $("userIdText");
        if (uidEl) {
            uidEl.style.display = "";
            const currentUid = state.userId || (state.username ? `U-${state.username.toUpperCase().slice(0, 6)}` : "");
            uidEl.textContent = currentUid ? `user_id：${currentUid}` : "";
            if (currentUid) {
                uidEl.title = "点击复制 user_id";
                uidEl.style.cursor = "pointer";
                // 用 onclick 覆盖式绑定，避免 renderAccountMenu 多次调用时重复叠加监听
                uidEl.onclick = (e) => {
                    e.stopPropagation();
                    copyText(currentUid);
                    uidEl.textContent = "已复制 ✓";
                    setTimeout(() => { uidEl.textContent = `user_id：${currentUid}`; }, 1000);
                };
            } else {
                uidEl.onclick = null;
                uidEl.title = "";
                uidEl.style.cursor = "";
            }
        }
        const linkedEl = $("linkedList");
        if (linkedEl) {
            linkedEl.innerHTML = "";
            if (state.role !== "student" && state.linkedStudents.length) {
                const label = document.createElement("div");
                label.className = "account-linked-label";
                label.textContent = "已连接学生：";
                linkedEl.appendChild(label);
                state.linkedStudents.forEach(item => {
                    const sid = item.user_id || item;
                    const labelName = (item.student_name || sid) + (item.relation ? `（${item.relation}）` : "");
                    const chip = document.createElement("span");
                    chip.className = "account-chip";
                    chip.textContent = labelName;
                    chip.dataset.sid = sid;
                    chip.title = "点击切换当前对话记忆体";
                    if (sid === state.currentMemoryUser) chip.classList.add("active");
                    chip.addEventListener("click", () => {
                        state.currentMemoryUser = sid;
                        state.displayName = labelName;
                        localStorage.setItem("gateway_memory_user", sid);
                        renderAccountMenu();
                        loadHistory();
                    });
                    linkedEl.appendChild(chip);
                });
            }
        }
        const bindArea = $("bindArea");
        if (bindArea) bindArea.style.display = state.role !== "student" ? "flex" : "none";
        // 绑定关系输入框按角色提示（老师自动补"老师"后缀由服务端处理）
        const relInput = $("bindRelationInput");
        if (relInput) {
            relInput.placeholder = state.role === "teacher" ? "你是学生的____老师" : "你是学生的：";
        }
        const sub = $("userSub");
        if (sub) sub.textContent = state.role === "student"
            ? "学生端 · 独立记忆空间"
            : (state.currentMemoryUser
                ? `正在对话 ${state.displayName || state.currentMemoryUser}`
                : "请先连接学生");
        // 对话页顶部标注当前端 + 当前身份（xxx（学生）的xxx（关系））
        const chatStatus = $("chatStatus");
        if (chatStatus) {
            if (state.role === "student") {
                chatStatus.textContent = (ROLE_NAMES[state.role] || "") + (state.minorMode ? " · 未成年模式" : "");
            } else {
                chatStatus.textContent = `${ROLE_NAMES[state.role] || ""}${state.displayName ? ` · ${state.displayName}` : ""}`;
            }
        }
        // 家长/老师端：附加未成年模式开关
        buildMinorModeToggle();
    }

    let _authRetryCount = 0;
    async function validateAndEnterSession() {
        if (!state.token) {
            doLogout();
            return false;
        }
        const authHint = $("authHint");
        if (authHint) authHint.textContent = "正在验证登录状态...";
        try {
            const p = await gatewayApi(`/gateway/profile`);
            if (p && p.ok) {
                state.loggedIn = true;
                state.role = p.role || state.role || "student";
                state.userId = p.user_id || state.userId || "";
                state.username = p.username || state.username || "";
                state.linkedStudents = p.linked_students || [];
                state.minorMode = !!p.minor_mode || false;
                state.userAvatar = p.avatar || "";
                localStorage.setItem("gateway_token", state.token);
                if (state.username) localStorage.setItem("gateway_user", state.username);
                localStorage.setItem("gateway_role", state.role);
                localStorage.setItem("gateway_userid", state.userId);
                if (authHint) authHint.textContent = "";
                _authRetryCount = 0;
                await enterChat();
                // 兜底：无论 enterChat 内部是否异常，只要验证通过就确保登录弹窗隐藏
                hideAuthOverlay();
                return true;
            } else {
                // 服务端明确返回无效（401）才登出
                doLogout("登录已过期或未登录，请重新登录。");
                return false;
            }
        } catch (e) {
            // 网络错误 / 429 限流 / 403 签名 / 5xx：保留登录态，有限次重试，绝不登出
            _authRetryCount++;
            const msg = (e && e.message) || "";
            if (authHint) {
                authHint.textContent = msg.includes("频繁")
                    ? "请求过于频繁，请稍后重试。"
                    : `网络异常，正在重试 (${_authRetryCount}/3)...`;
            }
            if (_authRetryCount < 3) {
                setTimeout(() => validateAndEnterSession(), 1500);
            } else {
                _authRetryCount = 0;
                if (authHint) authHint.textContent = "无法连接服务器，请刷新重试。";
            }
            return false;
        }
    }

    async function enterChat() {
        if (!state.loggedIn) {
            showAuthOverlay();
            return;
        }
        hideAuthOverlay();

        // 确保聊天卡隐藏，移除 chat-active，联系人列表处于中央默认位置
        const chatPage = $("chat-page");
        const chatCard = $("chat-card");
        if (chatPage) chatPage.classList.remove("chat-active");
        if (chatCard) {
            chatCard.style.display = "";              // 清除内联 display，收起态由 CSS 控制（max-width:0 + 隐藏）
            chatCard.classList.remove("open-right");
        }
        state.currentChat = null;

        if (state.role === "student") {
            // 学生端：记忆体归属自己
            state.currentMemoryUser = state.userId || state.username;
            state.displayName = state.username;
            if (state.currentMemoryUser) {
                localStorage.setItem("gateway_memory_user", state.currentMemoryUser);
            }
        } else {
            // 家长/老师端：先尝试恢复上次选中的学生；若无或失效，则默认选中第1个绑定学生
            const saved = localStorage.getItem("gateway_memory_user");
            let matched = null;
            if (saved && state.linkedStudents.length) {
                matched = state.linkedStudents.find(e => (e.user_id || e) === saved);
            }
            if (!matched && state.linkedStudents.length) {
                matched = state.linkedStudents[0];
            }
            if (matched) {
                const sid = matched.user_id || matched;
                state.currentMemoryUser = sid;
                state.displayName = (matched.student_name || sid) + (matched.relation ? `（${matched.relation}）` : "");
                localStorage.setItem("gateway_memory_user", sid);
            } else {
                state.currentMemoryUser = "";
                state.displayName = "";
                localStorage.removeItem("gateway_memory_user");
            }
        }

        renderAccountMenu();
        loadBotName();
        renderContactList(); // 关键：先基于本地缓存瞬间渲染列表，绝不白屏、绝不出现空白！
        refreshAllContacts(); // 并发安全拉取最新关系，完成后自动调度单次渲染
        connectWs();          // 确保建立带鉴权 token 的 WebSocket 实时推送
        startMessagePolling();
        startHeartbeat();
        checkBroadcast();   // 每次进入客户端检查是否需弹窗展示全站公告
    }

    // ========== 全站广播弹窗 ==========
    // 规则：管理员启用的公告在客户端每次打开后弹窗；
    //   「好的」  → 关闭弹窗，下次打开仍会提醒（不写本地记录）；
    //   「不再提醒」→ 记住该条内容，之后不再弹出；管理员更新内容后会再次提醒。
    function _broadcastKey(content) {
        // 用内容长度 + 内容本身生成轻量指纹（避免存全文，也便于内容变化时自动失效）
        let h = 0;
        for (let i = 0; i < content.length; i++) {
            h = ((h << 5) - h + content.charCodeAt(i)) | 0;
        }
        return `${content.length}:${h}`;
    }

    async function checkBroadcast() {
        try {
            const r = await gatewayApi("/gateway/broadcast");
            if (!r || !r.ok || !r.enabled || !r.content) return;
            const key = _broadcastKey(r.content);
            let muted = "";
            try { muted = localStorage.getItem("broadcast_muted_key") || ""; } catch (e) {}
            if (muted && muted === key) return;   // 该条内容已选择"不再提醒"
            showBroadcast(r.content, key, r.updated_at);
        } catch (e) { /* silent：公告拉取失败不影响正常使用 */ }
    }

    let _broadcastCurrentKey = "";
    function showBroadcast(content, key, updatedAt, isInstant = false) {
        const ov = $("broadcast-overlay");
        const body = $("broadcastBody");
        if (!ov || !body) return;
        _broadcastCurrentKey = key || _broadcastKey(content);
        body.innerHTML = renderMarkdown(content);
        const timeEl = $("broadcastTime");
        if (timeEl) {
            timeEl.textContent = updatedAt
                ? new Date(updatedAt * 1000).toLocaleString()
                : "";
        }
        const titleEl = $("broadcastCardTitle");
        const btnNever = $("btnBroadcastNever");
        const btnOk = $("btnBroadcastOk");
        if (isInstant) {
            // 实时广播：专属标题，不提供"不再提醒"（因为仅限实时这一次，刷新不存），按钮文案设为"我知道了"
            if (titleEl) titleEl.textContent = "实时广播 · Nascence辉夜";
            if (btnNever) btnNever.style.display = "none";
            if (btnOk) btnOk.textContent = "我知道了";
        } else {
            // 普通全站公告（持久化）：提供"不再提醒"，按钮文案设为"好的"
            if (titleEl) titleEl.textContent = "公告 · Nascence辉夜";
            if (btnNever) btnNever.style.display = "";
            if (btnOk) btnOk.textContent = "好的";
        }
        ov.classList.add("open");
    }

    function onIncomingInstantBroadcast(msg) {
        if (!msg || !msg.content) return;
        showBroadcast(msg.content, "", msg.time || (Date.now() / 1000), true);
    }

    function closeBroadcast() {
        const ov = $("broadcast-overlay");
        if (ov) ov.classList.remove("open");
    }

    // 「好的」：仅关闭，下次打开仍提醒
    const _btnBroadcastOk = $("btnBroadcastOk");
    if (_btnBroadcastOk) {
        _btnBroadcastOk.addEventListener("click", () => {
            closeBroadcast();
        });
    }
    // 「不再提醒」：记住该条内容，之后不再弹出（内容更新后会再次提醒）
    const _btnBroadcastNever = $("btnBroadcastNever");
    if (_btnBroadcastNever) {
        _btnBroadcastNever.addEventListener("click", () => {
            try { localStorage.setItem("broadcast_muted_key", _broadcastCurrentKey || ""); } catch (e) {}
            closeBroadcast();
        });
    }
    // 点击遮罩空白处等同「好的」（与其它模态一致）
    const _broadcastOverlay = $("broadcast-overlay");
    if (_broadcastOverlay) {
        _broadcastOverlay.addEventListener("click", (e) => {
            if (e.target === _broadcastOverlay) closeBroadcast();
        });
    }

    // ========== WebSocket 实时推送（认知循环主动发言等消息） ==========
    let ws = null;
    let wsReconnectTimer = null;
    function connectWs() {
        try {
            if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
            // WebSocket 连接到网关主机（客户端独立存放/跨源时也能收到服务端推送）
            // 携带 token 参数供服务端按用户关联心跳（仅用于活跃识别，不经过 URL 鉴权）
            const gw = new URL(state.gateway);
            const proto = gw.protocol === "https:" ? "wss" : "ws";
            const wsUrl = `${proto}://${gw.host}/ws` + (state.token ? `?token=${encodeURIComponent(state.token)}` : "");
            ws = new WebSocket(wsUrl);
            ws.onmessage = (ev) => {
                try {
                    const msg = JSON.parse(ev.data);
                    if (msg.type === "message") {
                        // 收到 WebSocket 推送时立即触发增量同步（精准对齐条数）
                        syncNewMessages();
                    } else if (msg.type === "chat_message") {
                        onIncomingChatMessage(msg);
                    } else if (msg.type === "chat_recall") {
                        onIncomingChatRecall(msg);
                    } else if (msg.type === "social_event") {
                        onIncomingSocialEvent(msg);
                    } else if (msg.type === "lottery_event") {
                        onIncomingLotteryEvent(msg);
                    } else if (msg.type === "instant_broadcast") {
                        onIncomingInstantBroadcast(msg);
                    }
                } catch (e) { /* ignore */ }
            };
            ws.onclose = () => {
                if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
                wsReconnectTimer = setTimeout(connectWs, 2000);
            };
        } catch (e) { /* ignore */ }
    }

    let pollTimer = null;

    // 本地待确认的用户消息（实时渲染但尚未被服务端历史确认）。
    // 元素结构：{ localId, sender, text, mtype, image, time }
    let localPending = [];

    function genLocalId() {
        return "local_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8);
    }

    function flushPending(serverMsgs) {
        if (!localPending.length) return;
        // 比对 serverMsgs 中的用户消息与 localPending：
        // 服务端末尾已收录的用户消息按 FIFO 剔除出 localPending
        const serverUserMsgs = (serverMsgs || []).filter(m => m.sender !== state.botName);
        while (localPending.length && serverUserMsgs.length) {
            const p = localPending[0];
            const pText = p.text;
            const pIsImg = (p.mtype === "image");
            const idx = serverUserMsgs.findIndex(sm => {
                const smIsImg = (sm.mtype === "image" || !!sm.image);
                if (pIsImg) return smIsImg;
                return (sm.content || sm.text || "") === pText;
            });
            if (idx !== -1) {
                localPending.shift();
                serverUserMsgs.splice(idx, 1);
            } else {
                break;
            }
        }
        if (!localPending.length) return;

        // 仅将真正尚未落盘的本地消息补渲在末尾（无闪烁）
        const view = $("chatView");
        const anchor = view.querySelector('.msg-row:not(.user):not(.system)');
        for (const p of localPending) {
            const row = buildBubbleRow({
                id: p.localId,
                sender: p.sender,
                content: p.text,
                source: "客户端",
                mtype: p.mtype,
                image: p.image,
                quote: p.quote,
                time: p.time
            });
            if (!row) continue;
            row.dataset.local = "1";
            row.dataset.localId = p.localId;
            row.dataset.msgId = p.localId;
            if (anchor) view.insertBefore(row, anchor);
            else view.appendChild(row);
        }
        scrollChatToBottom();
    }

    // 双向状态比对与一致性对齐：比对服务端消息序列与当前 DOM 渲染节点
    // 1. 认领与转正：服务端返回用户消息时，按 FIFO 认领本地未确认气泡并回填真实 id，绝不重复创建；
    // 2. 新消息追加：对真正新到的 Bot 回复或远端同步消息进行有序插入与揭晓动画；
    // 3. 支持连续发送相同内容的消息，条条精准对应。
    function reconcileBotMessages(view, serverMsgs) {
        if (!view || !serverMsgs || !serverMsgs.length) return;

        // 1. 收集视图中已有的 DOM 节点
        const existingRows = Array.from(view.querySelectorAll(".msg-row"));
        const rowById = new Map();
        const unconfirmedLocalRows = [];

        existingRows.forEach(row => {
            const mid = row.dataset.msgId;
            if (mid) rowById.set(mid, row);
            // 带有 data-local="1" 或 local_ 开头的行视为本地待确认行
            if (row.dataset.local === "1" || (mid && mid.startsWith("local_"))) {
                unconfirmedLocalRows.push(row);
            }
        });

        let hasNew = false;
        let prevTime = lastMsgTimeIn(view);

        // 2. 遍历服务端最新历史序列
        for (let i = 0; i < serverMsgs.length; i++) {
            const m = serverMsgs[i];
            const isBot = (m.sender === state.botName);

            // 如果该消息已经在 DOM 中存在（根据服务端唯一 ID 匹配）
            if (m.id && rowById.has(m.id)) {
                if (m.time != null) prevTime = m.time;
                continue;
            }

            if (!isBot) {
                // 【核心对齐点 1：用户消息认领与转正】
                // 优先从 unconfirmedLocalRows 中按 FIFO 寻找首个匹配的本地气泡转正
                let claimedRow = null;
                let claimedPendingIdx = -1;

                const mContent = m.content || m.text || "";
                const mIsImg = (m.mtype === "image" || !!m.image);

                for (let j = 0; j < unconfirmedLocalRows.length; j++) {
                    const lr = unconfirmedLocalRows[j];
                    if (!lr) continue;
                    const lrLocalId = lr.dataset.localId || lr.dataset.msgId;
                    const lrIsImg = !!lr.querySelector(".msg-text-img");
                    const lrText = lr.querySelector(".msg-text")?.textContent || "";

                    let match = false;
                    if (mIsImg && lrIsImg) match = true;
                    else if (!mIsImg && !lrIsImg && lrText === mContent) match = true;

                    if (match) {
                        claimedRow = lr;
                        unconfirmedLocalRows[j] = null; // 标记已被认领
                        if (lrLocalId) {
                            claimedPendingIdx = localPending.findIndex(p => p.localId === lrLocalId);
                        }
                        break;
                    }
                }

                // 兜底：若内容有细微变动但存在未认领的本地行，按 FIFO 认领最前的一条
                if (!claimedRow) {
                    const firstIdx = unconfirmedLocalRows.findIndex(Boolean);
                    if (firstIdx !== -1) {
                        claimedRow = unconfirmedLocalRows[firstIdx];
                        unconfirmedLocalRows[firstIdx] = null;
                        const lrLocalId = claimedRow.dataset.localId || claimedRow.dataset.msgId;
                        if (lrLocalId) {
                            claimedPendingIdx = localPending.findIndex(p => p.localId === lrLocalId);
                        }
                    }
                }

                if (claimedRow) {
                    // 转正本地气泡：赋给真实服务端 id，移除 local 标记，更新时间
                    const oldId = claimedRow.dataset.msgId;
                    if (oldId) rowById.delete(oldId);
                    claimedRow.dataset.msgId = m.id;
                    rowById.set(m.id, claimedRow);
                    claimedRow.removeAttribute("data-local");
                    delete claimedRow.dataset.local;
                    if (oldId) syncQuoteId(oldId, m.id);   // 引用目标若为本地临时 ID，同步为真实 ID
                    if (m.time != null) {
                        claimedRow.dataset.msgTime = String(m.time);
                        prevTime = m.time;
                    }
                    if (claimedPendingIdx !== -1) {
                        localPending.splice(claimedPendingIdx, 1);
                    }
                    // 转正完成，绝对不画第二条！
                    continue;
                }

                // 若 DOM 中确实没有任何本地未确认气泡，说明是远端/其他端同步的消息
                if (needTimeDivider(prevTime, m.time)) {
                    view.appendChild(makeTimeDivider(m.time));
                    prevTime = m.time;
                }
                const row = buildBubbleRow(m);
                if (row) {
                    row.classList.add("msg-enter");
                    view.appendChild(row);
                    rowById.set(m.id, row);
                    hasNew = true;
                }
                if (m.time != null) prevTime = m.time;
                continue;
            }

            // 【核心对齐点 2：Bot 回复消息】
            let think = null;
            if (m.time != null) {
                let base = prevTime != null ? prevTime : null;
                if (m.cognition_start != null && (base == null || m.cognition_start > base)) base = m.cognition_start;
                if (base != null && m.time >= base) think = (m.time - base).toFixed(1);
            }

            if (needTimeDivider(prevTime, m.time)) {
                view.appendChild(makeTimeDivider(m.time));
                prevTime = m.time;
            }

            appendBubble(m.sender, m.content, m.source, think, true, m, true);
            if (m.id) {
                const newRow = view.querySelector(`[data-msg-id="${m.id}"]`);
                if (newRow) rowById.set(m.id, newRow);
            }
            if (m.time != null) prevTime = m.time;
            hasNew = true;
            hideThinking();
        }

        if (hasNew) {
            scrollChatToBottom();
            if (state.currentChat && state.currentChat.chat_type === "bot") {
                markConversationRead(state.currentChat);
            }
        }
    }

    async function syncNewMessages() {
        if (!state.loggedIn || !state.token) return;
        // 关键防护：如果当前没有打开聊天，或者打开的不是辉夜(Bot)，绝不向聊天区追加辉夜历史消息！
        if (!state.currentChat || state.currentChat.chat_type !== "bot") return;
        try {
            const muid = state.currentMemoryUser ? `?memory_user_id=${encodeURIComponent(state.currentMemoryUser)}` : "";
            const r = await gatewayApi(`/gateway/history${muid}`);
            const msgs = r.messages || [];
            if (!msgs.length) return;
            $("welcome").style.display = "none";

            const view = $("chatView");
            if (!view) return;

            reconcileBotMessages(view, msgs);
        } catch (e) { /* silent */ }
    }

    async function syncCurrentSocialChat() {
        if (!state.loggedIn || !state.token || !state.currentChat) return;
        if (state.currentChat.chat_type === "bot") return;
        try {
            const cur = state.currentChat;
            const r = await gatewayApi(`/gateway/messages?chat_type=${encodeURIComponent(cur.chat_type)}&target=${encodeURIComponent(cur.target)}`, "GET");
            if (r.ok && r.messages && state.currentChat && state.currentChat.target === cur.target) {
                const view = $("chatView");
                const existingRows = Array.from(view.querySelectorAll(".msg-row"));
                const rowById = new Map();
                const unconfirmedLocalRows = [];

                existingRows.forEach(row => {
                    const mid = row.dataset.msgId;
                    if (mid) rowById.set(mid, row);
                    // 仅 data-local="1" 或 local_ 前缀才是本地未确认气泡（msg_ 前缀是服务端真实 id，不可认领）
                    if (row.dataset.local === "1" || (mid && mid.startsWith("local_"))) {
                        unconfirmedLocalRows.push(row);
                    }
                });

                let hasNew = false;
                let prevTime = lastMsgTimeIn(view);

                r.messages.forEach(m => {
                    if (rowById.has(m.id)) {
                        if (m.recalled) {
                            const row = rowById.get(m.id);
                            if (row && !row.classList.contains("system")) {
                                markMessageRecalled(m.id, m.sender);
                            }
                        }
                        if (m.time != null) prevTime = m.time;
                        return;
                    }

                    // 检查是否可以转正本地未确认气泡（自己发送的）
                    let claimedRow = null;
                    if (m.sender === state.username) {
                        for (let j = 0; j < unconfirmedLocalRows.length; j++) {
                            const lr = unconfirmedLocalRows[j];
                            if (!lr) continue;
                            const lrMid = lr.dataset.msgId;
                            if (window.__confirmedMsgId && window.__confirmedMsgId[lrMid] === m.id) {
                                claimedRow = lr;
                                unconfirmedLocalRows[j] = null;
                                break;
                            }
                            const lrText = lr.querySelector(".msg-text")?.textContent || "";
                            if (lrText === (m.content || m.text || "")) {
                                claimedRow = lr;
                                unconfirmedLocalRows[j] = null;
                                break;
                            }
                        }
                    }

                    if (claimedRow) {
                        const oldId = claimedRow.dataset.msgId;
                        if (oldId) rowById.delete(oldId);
                        claimedRow.dataset.msgId = m.id;
                        rowById.set(m.id, claimedRow);
                        claimedRow.removeAttribute("data-local");
                        delete claimedRow.dataset.local;
                        if (oldId) syncQuoteId(oldId, m.id);   // 引用目标 ID 同步
                        if (m.time != null) {
                            claimedRow.dataset.msgTime = String(m.time);
                            prevTime = m.time;
                        }
                        return;
                    }

                    if (needTimeDivider(prevTime, m.time)) {
                        view.appendChild(makeTimeDivider(m.time));
                        prevTime = m.time;
                    }
                    const row = buildBubbleRow(m);
                    if (row) {
                        row.classList.add("msg-enter");
                        view.appendChild(row);
                        hasNew = true;
                        rowById.set(m.id, row);
                        if (m.time != null) prevTime = m.time;
                    }
                });

                if (hasNew) {
                    scrollChatToBottom();
                    markConversationRead(cur);
                }
            }
        } catch (e) { /* silent */ }
    }

    let heartbeatTimer = null;
    function startHeartbeat() {
        if (heartbeatTimer) clearInterval(heartbeatTimer);
        // 每 15 秒向服务端报告一次客户端活跃心跳
        heartbeatTimer = setInterval(async () => {
            if (!state.loggedIn) return;
            try {
                await gatewayApi("/gateway/heartbeat", "POST", {});
            } catch (e) { /* silent */ }
        }, 15000);
    }
    function stopHeartbeat() {
        if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
    }

    function startMessagePolling() {
        if (pollTimer) clearInterval(pollTimer);
        // 增量同步 + 会话状态刷新
        pollTimer = setInterval(() => {
            if (!state.loggedIn) return;
            if (state.currentChat && state.currentChat.chat_type === "bot") {
                syncNewMessages();
            } else if (state.currentChat && (state.currentChat.chat_type === "user" || state.currentChat.chat_type === "group")) {
                syncCurrentSocialChat();
            }
            loadConversations();
        }, 2000);
        startHeartbeat();
    }
    function stopMessagePolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        stopHeartbeat();
    }

    async function loadBotName() {
        try {
            const r = await gatewayApi("/gateway/bot");
            if (r.bot_name) { state.botName = r.bot_name; $("mentionName").textContent = r.bot_name; }
            if (r.bot_avatar) state.botAvatar = r.bot_avatar;
            if (r.bot_avatar_default) state.botAvatarDefault = r.bot_avatar_default;
        } catch (e) { /* silent */ }
    }
    // 确保滚动容器绑定了向上滚动加载监听（仅绑一次）
    function ensureChatScrollBound() {
        const sc = getChatScroller();
        if (sc && !sc.dataset.historyScrollBound) {
            sc.dataset.historyScrollBound = "1";
            sc.addEventListener("scroll", onHistoryScroll);
        }
    }

    // 全量加载辉夜机器人历史（带防竞态与原子替换，失败保留已有内容不上屏白板）
    async function loadHistory() {
        if (!state.loggedIn) return;
        const thisTicket = ++currentChatTicket;
        const curChatKey = conversationKey(state.currentChat);
        try {
            const muid = state.currentMemoryUser ? `?memory_user_id=${encodeURIComponent(state.currentMemoryUser)}` : "";
            const r = await gatewayApi(`/gateway/history${muid}`);
            // 防乱序/切走覆盖
            if (thisTicket !== currentChatTicket || !state.currentChat || conversationKey(state.currentChat) !== curChatKey) {
                return;
            }
            const view = $("chatView");
            if (!view) return;
            const msgs = (r && r.messages) || [];

            if (!msgs.length) {
                view.innerHTML = "";
                $("welcome").style.display = "flex";
                historyHasMore = false;
                historyOldestTime = null;
                historyLoadingMore = false;
                return;
            }

            $("welcome").style.display = "none";
            // 离屏组装 DOM，避免先清空导致的网络未返回期间出现空白
            const frag = document.createDocumentFragment();
            let prevTime = null;
            const readTs = state.newDividerTs;
            let newDividerPlaced = false;

            msgs.forEach(m => {
                let think = null;
                if (m.sender === state.botName && m.time != null) {
                    let base = prevTime != null ? prevTime : null;
                    if (m.cognition_start != null && (base == null || m.cognition_start > base)) base = m.cognition_start;
                    if (base != null && m.time >= base) think = (m.time - base).toFixed(1);
                }
                if (needTimeDivider(prevTime, m.time)) {
                    frag.appendChild(makeTimeDivider(m.time));
                    prevTime = m.time;
                }
                if (!newDividerPlaced && readTs != null && m.time != null && m.time > readTs) {
                    frag.appendChild(makeNewMsgDivider());
                    newDividerPlaced = true;
                }
                const row = buildBubbleRow(m, think);
                if (row) frag.appendChild(row);
                if (m.time != null) prevTime = m.time;
            });

            // 原子替换：在挂载新 DOM 的同时清空旧视图，消除白屏空档
            view.innerHTML = "";
            view.appendChild(frag);

            state.newDividerTs = null;
            historyHasMore = !!r.has_more;
            historyOldestTime = msgs[0].time;
            historyLoadingMore = false;

            scrollChatToBottom();
            startMessagePolling();
            ensureChatScrollBound();
            flushPending(msgs);
            if (state.currentChat && state.currentChat.chat_type === "bot") {
                markConversationRead(state.currentChat);
            }
        } catch (e) {
            // 失败保护：如果视图中已有消息，绝不清空成白板；仅在全新进入且为空时提示
            const view = $("chatView");
            if (view && !view.children.length) {
                view.innerHTML = `<div class="empty-hint" style="cursor:pointer;" onclick="loadHistory()">历史消息加载失败（${escapeHtml(e.message || "网络异常")}），点击重试</div>`;
            }
        }
    }

    // 返回实际的滚动容器（#chat-body 是 overflow-y:auto 的滚动层，#chatView 只是内部消息列表）
    function getChatScroller() {
        return $("chat-body") || $("chatView");
    }

    function scrollChatToBottom() {
        const scroller = getChatScroller();
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
    }

    // 判断是否接近底部（用户没有向上翻阅历史时自动跟随到底部）
    function isNearBottom(scroller, threshold = 80) {
        return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < threshold;
    }

    // ========== 思考中提示（仅辉夜对话） ==========
    // 发送消息后显示转圈动画；收到回复或超时后隐藏。
    // 用引用计数支持连发多条：每发一条 +1，每收到一条回复 -1，归零时隐藏。
    let thinkingCount = 0;
    let thinkingTimer = null;
    const THINKING_TIMEOUT_MS = 180000;   // 兜底上限 3 分钟，避免异常时永久转圈

    function showThinking() {
        thinkingCount++;
        const el = $("thinkingIndicator");
        if (!el) return;
        el.style.display = "flex";
        // 滚动后可见（思考提示在输入框上方，通常已可见；稳妥起见贴近底部）
        if (isNearBottom(getChatScroller())) scrollChatToBottom();
        if (thinkingTimer) clearTimeout(thinkingTimer);
        thinkingTimer = setTimeout(() => {
            thinkingCount = 0;
            hideThinking(true);
        }, THINKING_TIMEOUT_MS);
    }
    function hideThinking(force = false) {
        if (!force && thinkingCount > 0) thinkingCount--;
        if (force) thinkingCount = 0;
        if (thinkingCount > 0) return;
        const el = $("thinkingIndicator");
        if (el) el.style.display = "none";
        if (thinkingTimer) { clearTimeout(thinkingTimer); thinkingTimer = null; }
    }

    // 通用向上滚动加载更早的 30 条历史（支持 bot / user / group 全类型，平滑补偿高度不跳动）
    async function loadOlderHistory() {
        if (!state.currentChat || !historyHasMore || historyLoadingMore || historyOldestTime == null) return;
        historyLoadingMore = true;
        const cur = state.currentChat;
        const curKey = conversationKey(cur);
        const thisTicket = currentChatTicket;
        const scroller = getChatScroller();
        const prevScrollTop = scroller ? scroller.scrollTop : 0;
        const prevHeight = scroller ? scroller.scrollHeight : 0;
        try {
            let r;
            if (cur.chat_type === "bot") {
                const muid = state.currentMemoryUser ? `&memory_user_id=${encodeURIComponent(state.currentMemoryUser)}` : "";
                r = await gatewayApi(`/gateway/history?before=${encodeURIComponent(historyOldestTime)}${muid}`);
            } else {
                r = await gatewayApi(`/gateway/messages?chat_type=${encodeURIComponent(cur.chat_type)}&target=${encodeURIComponent(cur.target)}&before=${encodeURIComponent(historyOldestTime)}&limit=30`);
            }
            // 防乱序/切走覆盖
            if (thisTicket !== currentChatTicket || !state.currentChat || conversationKey(state.currentChat) !== curKey) {
                return;
            }
            const msgs = (r && r.messages) || [];
            if (msgs.length) {
                const view = $("chatView");
                const frag = document.createDocumentFragment();
                let prevTime = null;
                msgs.forEach(m => {
                    let think = null;
                    if (m.sender === state.botName && m.time != null) {
                        let base = prevTime != null ? prevTime : null;
                        if (m.cognition_start != null && (base == null || m.cognition_start > base)) base = m.cognition_start;
                        if (base != null && m.time >= base) think = (m.time - base).toFixed(1);
                    }
                    if (needTimeDivider(prevTime, m.time)) frag.appendChild(makeTimeDivider(m.time));
                    const row = buildBubbleRow(m, think);
                    if (row) frag.appendChild(row);
                    if (m.time != null) prevTime = m.time;
                });

                // 检查前插最晚一条与已有最旧一条之间的时间分隔
                const firstCurTime = lastMsgTimeIn(frag);
                const firstOldTime = firstMsgTimeIn(view);
                if (firstCurTime != null && firstOldTime != null && needTimeDivider(firstCurTime, firstOldTime)) {
                    frag.appendChild(makeTimeDivider(firstOldTime));
                }

                view.insertBefore(frag, view.firstChild);
                // 推进游标：新一批最旧一条的真实时间戳
                historyOldestTime = msgs[0].time;
                historyHasMore = !!r.has_more;
                // 补偿前插高度，保持用户当前滚动视线完全静止
                if (scroller) {
                    const added = scroller.scrollHeight - prevHeight;
                    scroller.scrollTop = prevScrollTop + added;
                }
            } else {
                historyHasMore = false;
            }
        } catch (e) {
            // 失败时保持现态，允许用户重新向上微滑重试，绝不将 hasMore 错置为 false
        } finally {
            historyLoadingMore = false;
        }
    }

    // 滚动到顶部附近时触发加载更早历史（支持机器人、好友私聊、群聊全类型）
    function onHistoryScroll() {
        if (!state.currentChat) return;
        const scroller = getChatScroller();
        if (!scroller || !historyHasMore || historyLoadingMore || historyOldestTime == null) return;
        if (scroller.scrollTop <= 60) {
            loadOlderHistory();
        }
    }

    // 构建单条消息的 DOM row（供 loadHistory / loadOlderHistory / 发送共用）
    function buildBubbleRow(m, think) {
        if (!m) return null;
        // 撤回消息：以灰色小字居中呈现
        if (m.recalled || m.content === "此消息已撤回") {
            const row = document.createElement("div");
            row.className = "msg-row system";
            row.dataset.msgId = m.id || "";
            if (m.time != null) row.dataset.msgTime = String(m.time);
            const isSelf = m.sender === state.username;
            row.innerHTML = `<div class="msg-recalled">${isSelf ? "你" : escapeHtml(m.sender || "对方")} 撤回了一条消息</div>`;
            return row;
        }

        const isBot = m.sender === state.botName;
        const isSelf = m.sender === state.username || m.sender === state.displayName;
        const isSystem = (m.sender === "系统" || m.source === "错误" || m.source === "系统" || (m.content || "").includes("请联系管理员"));
        const text = m.content || m.text || "";
        const row = document.createElement("div");
        row.className = "msg-row" + (isSystem ? " system" : isSelf ? " user" : "");
        row.dataset.msgId = m.id || "";
        if (m.time != null) row.dataset.msgTime = String(m.time);   // 供时间分隔/已读定位

        if (!isSystem) {
            const avatar = document.createElement("div");
            avatar.className = "msg-avatar";
            // 统一用"字母占位层(绝对定位) + <img> 在流内"的结构，避免加载前后布局抽动。
            const addInitial = (txt) => {
                const ini = document.createElement("span");
                ini.className = "avatar-initial";
                ini.textContent = txt;
                avatar.appendChild(ini);
                return ini;
            };
            if (isBot) {
                const primary = state.botAvatar, fallback = state.botAvatarDefault;
                const showPrimary = primary && primary !== fallback;
                const src = showPrimary ? primary : fallback;
                const ini = addInitial("♾");
                if (src) {
                    const img = document.createElement("img");
                    img.className = "msg-avatar-img"; img.alt = ""; img.src = src;
                    img.onload = () => { if (ini.parentNode) ini.remove(); };
                    img.onerror = () => {
                        img.onerror = null;
                        if (showPrimary && fallback) { img.src = fallback; }
                        else img.remove();
                    };
                    avatar.appendChild(img);
                }
            } else if (isSelf) {
                // 自己：使用本地已知头像 URL（上传后即更新 state.userAvatar），失败回退字母
                const ini = addInitial((state.username || "我").slice(0, 1).toUpperCase());
                if (state.userAvatar) {
                    const uimg = document.createElement("img");
                    uimg.className = "msg-avatar-img"; uimg.alt = ""; uimg.src = state.userAvatar;
                    uimg.onload = () => { if (ini.parentNode) ini.remove(); };
                    uimg.onerror = () => { uimg.onerror = null; uimg.remove(); };
                    avatar.appendChild(uimg);
                }
            } else {
                // 他人（私聊对方 / 群聊成员）：按用户名加载其头像，未上传时回退首字母
                fillUserAvatar(avatar, m.sender);
            }
            if (!isSelf && !isSystem && m.sender) {
                avatar.title = `点击@ ${m.sender}`;
                avatar.addEventListener("click", (e) => {
                    e.stopPropagation();
                    insertMention(m.sender);
                });
            }
            row.appendChild(avatar);
        }

        // 宽度上限统一由外层包裹控制（.msg-bubble-wrap）；气泡自身 100% 填充，
        // 避免"气泡 72% × 包裹 72%"双重限制造成气泡与头像间大片空白
        const bubbleWrap = document.createElement("div");
        bubbleWrap.className = "msg-bubble-wrap";

        // 群聊且非自己发送时，在气泡上方显示发送者昵称
        if (state.currentChat && state.currentChat.chat_type === "group" && !isSelf && !isSystem) {
            const senderTag = document.createElement("div");
            senderTag.className = "sender-name";
            senderTag.textContent = m.sender || "群友";
            bubbleWrap.appendChild(senderTag);
        }

        const bubble = document.createElement("div");
        bubble.className = "msg-bubble";

        // 气泡顶部内嵌引用预览卡片（对标 QQ 引用交互，点击平滑滚动并闪烁定位）
        if (m.quote && typeof m.quote === "object") {
            const qCard = document.createElement("div");
            qCard.className = "msg-quote-card";
            qCard.dataset.quoteId = m.quote.id || "";
            const senderEl = document.createElement("div");
            senderEl.className = "quote-sender";
            senderEl.textContent = `“ ${m.quote.sender || "某人"}`;
            qCard.appendChild(senderEl);

            const qIsImg = (m.quote.mtype === "image" || m.quote.mtype === "sticker" || m.quote.image);
            const textEl = document.createElement("div");
            textEl.className = "quote-text";
            if (qIsImg) {
                textEl.textContent = quoteMediaLabel(m.quote);
            } else {
                textEl.textContent = m.quote.content || "";
            }
            qCard.appendChild(textEl);

            if (qIsImg && m.quote.image) {
                // 用 DOM 赋值设置 src（而非拼接 HTML 字符串），避免引号逃逸造成的属性注入
                const thumb = document.createElement("img");
                thumb.className = "quote-thumb";
                thumb.alt = "缩略图";
                if (m.quote.mtype === "sticker") {
                    thumb.src = localStickerSrc(m.quote.image);
                    bindStickerFallback(thumb, m.quote.image);   // 本地缓存失效时回退远端
                } else {
                    thumb.src = m.quote.image;
                }
                qCard.appendChild(thumb);
            }

            qCard.addEventListener("click", (e) => {
                e.stopPropagation();
                // 传 sender/content 作为 id 失配时的兜底定位依据
                jumpToQuotedMessage(m.quote.id, m.quote.sender, m.quote.content);
            });
            bubble.appendChild(qCard);
        }

        if (isBot && !isSystem && think != null && think !== undefined) {
            const tb = document.createElement("div");
            tb.className = "think-badge";
            tb.textContent = `已深度思考（用时 ${think} 秒）`;
            bubble.appendChild(tb);
        }
        const isStickerMsg = (m.mtype === "sticker");
        const isImageMsg = !!(m.mtype === "image" || m.image);
        if (isStickerMsg && m.image) {
            // 表情包：以较小尺寸渲染（区别于普通聊天图片），点击仍可放大查看。
            // 无引用时去掉气泡底色，让贴纸成为视觉主体（对标 QQ 贴纸消息）。
            if (!(m.quote && typeof m.quote === "object")) {
                bubble.classList.add("sticker-only");
            }
            const img = document.createElement("img");
            img.className = "msg-sticker-img";
            img.alt = m.sticker_name || stickerNameFromUrl(m.image) || "表情包";
            // 不用 lazy：iOS Safari 对隐藏容器内的 lazy 图片可能不加载
            img.src = localStickerSrc(m.image);
            bindStickerFallback(img, m.image);   // 本地缓存失效时回退服务端地址
            img.addEventListener("click", () => window.open(m.image, "_blank"));
            bubble.appendChild(img);
        } else if (isImageMsg && m.image) {
            const img = document.createElement("img");
            img.className = "msg-text-img"; img.alt = "图片"; img.loading = "lazy"; img.src = m.image;
            img.addEventListener("click", () => window.open(m.image, "_blank"));
            bubble.appendChild(img);
            if (text && text !== "[图片]") {
                const et = document.createElement("div"); et.className = "msg-text";
                formatMessageTextWithMentions(et, text);
                bubble.appendChild(et);
            }
        } else {
            const tn = document.createElement("div");
            tn.className = "msg-text";
            formatMessageTextWithMentions(tn, text);
            bubble.appendChild(tn);
        }

        // 绑定右键和长按上下文菜单（复制、撤回、引用、@TA）
        bubble.addEventListener("contextmenu", (e) => {
            showMsgContextMenu(e, m, row);
        });

        // 双击同行空白快速引用（支持桌面端快速操作）
        if (!isSystem) {
            // 双击前先记录用户是否已手动选中文本：浏览器在双击空白处时会自动"选词"，
            // 若用双击后的选择内容判断，会把这种瞬时自动选中误判为复制意图而漏掉引用。
            // 仅在双击的第一击（e.detail === 1）记录：第二击时浏览器可能已完成选词。
            let selBeforeDbl = "";
            row.addEventListener("mousedown", (e) => {
                if (e.detail !== 1) return;
                const s = window.getSelection();
                selBeforeDbl = s ? s.toString().trim() : "";
            });
            row.addEventListener("dblclick", (e) => {
                if (selBeforeDbl.length > 0) return;   // 双击前已有选中：视为复制意图，不触发引用
                if (e.target.closest(".msg-bubble, .msg-avatar, button, a, img, input, textarea")) return;
                const sel = window.getSelection();
                if (sel) sel.removeAllRanges();        // 清掉双击自动选词残留的高亮
                setQuote(m, row);
            });

            // 移动端左划快速引用手势（对标手机 QQ，滑动超过阈值触发引用并平滑回弹）
            let touchStartX = 0;
            let touchStartY = 0;
            let touchMoveX = 0;
            let isSwiping = false;

            row.addEventListener("touchstart", (e) => {
                if (e.touches.length !== 1) return;
                touchStartX = e.touches[0].clientX;
                touchStartY = e.touches[0].clientY;
                touchMoveX = touchStartX;
                isSwiping = false;
            }, { passive: true });

            row.addEventListener("touchmove", (e) => {
                if (e.touches.length !== 1) return;
                touchMoveX = e.touches[0].clientX;
                const deltaX = touchMoveX - touchStartX;
                const deltaY = e.touches[0].clientY - touchStartY;
                if (Math.abs(deltaY) > Math.abs(deltaX) && !isSwiping) return;
                if (deltaX < -15) {
                    isSwiping = true;
                    const damp = Math.max(-65, deltaX * 0.55);
                    row.style.transform = `translateX(${damp}px)`;
                }
            }, { passive: true });

            row.addEventListener("touchend", () => {
                const deltaX = touchMoveX - touchStartX;
                row.style.transform = "";
                if (isSwiping && deltaX < -45) {
                    setQuote(m, row);
                }
                isSwiping = false;
            });
        }

        bubbleWrap.appendChild(bubble);
        row.appendChild(bubbleWrap);
        return row;
    }

    // 随机字符池（中英数字混合，用于占位效果）
    const _REVEAL_CHARS = "日月星海光风天云山河林川泽火电雨雪霜雾霞虹雷雪氚辉夜无限αβγδεζηθικλμνξοπρστυφχψωABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    function _randomChar() {
        return _REVEAL_CHARS[Math.floor(Math.random() * _REVEAL_CHARS.length)];
    }

    // 揭晓效果：先用随机字符占位整个文本，再从左向右每帧揭晓 1 个真实字符
    function revealText(textNode, realText) {
        // 拆分为字符数组（保留换行与空白）
        const chars = Array.from(realText);
        const len = chars.length;
        if (!len) return;
        // 生成随机占位字符序列
        const placeholder = chars.map(() => _randomChar());
        textNode.textContent = placeholder.join("");

        let revealed = 0;
        function step() {
            if (revealed >= len) {
                // 完成：显示真实完整文本并处理 @ 标签高亮
                textNode.innerHTML = "";
                formatMessageTextWithMentions(textNode, realText);
                return;
            }
            // 从左向右逐个揭晓：已揭晓部分用真实字符，未揭晓部分保持随机占位
            const next = chars.slice(0, revealed + 1).join("");
            const tail = placeholder.slice(revealed + 1).join("");
            textNode.textContent = next + tail;
            revealed++;
            requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
    }

    function appendBubble(name, text, source, thinkSeconds = null, reveal = false, msg = null, skipDivider = false) {
        const view = $("chatView");
        const scroller = getChatScroller();
        // 复用 buildBubbleRow 构建 DOM（统一头像/图片/思考标签逻辑）
        const m = (msg && typeof msg === "object") ? msg : { sender: name, content: text, source: source };
        const row = buildBubbleRow(m, thinkSeconds);
        if (!row) return;
        // appendBubble 仅用于"实时新消息"，播放轻柔的进入动画（历史全量渲染走 buildBubbleRow 不加）
        row.classList.add("msg-enter");
        // 揭晓效果（仅新到达的 Bot 回复，纯文本消息）：对气泡内 .msg-text 应用
        if (reveal === true && text && !(m.mtype === "image" || m.image)) {
            const tn = row.querySelector(".msg-text");
            if (tn) revealText(tn, text);
        }
        const shouldScroll = isNearBottom(scroller);
        // 实时消息：与前一条消息间隔超过 5 分钟时插入时间分隔（QQ 风格时间线）。
        // skipDivider=true 时由调用方自行插入（历史批量渲染已统一处理，避免重复分隔）。
        const prevT = lastMsgTimeIn(view);
        if (!skipDivider && m && m.time != null && needTimeDivider(prevT, m.time)) {
            view.appendChild(makeTimeDivider(m.time));
        }
        view.appendChild(row);
        // 仅在用户靠近底部时自动滚动到底部；向上翻阅历史时保持当前位置
        if (shouldScroll) scrollChatToBottom();
        $("welcome").style.display = "none";
    }

    // ========== 发送消息与增量同步 ==========
    // 发送用 Enter，换行用 Shift+Enter / Ctrl+Enter
    async function sendChat() {
        if (!requireAuth()) return;
        const text = $("chatInput").value.trim();
        if (!text) return;

        // 判断当前会话：私聊 / 群聊 走社交消息接口
        if (state.currentChat && state.currentChat.chat_type !== "bot") {
            const chatType = state.currentChat.chat_type;
            const target = state.currentChat.target;
            const tempId = `msg_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
            const curQuote = state.currentQuote;
            const msgObj = {
                id: tempId,
                sender: state.username,
                target: target,
                content: text,
                mtype: "text",
                time: Date.now() / 1000,
                recalled: false
            };
            if (curQuote) msgObj.quote = curQuote;
            appendBubble(state.username, text, "客户端", null, false, msgObj);
            const row = document.querySelector(`[data-msg-id="${tempId}"]`);
            if (row) row.dataset.local = "1";
            $("chatInput").value = "";
            clearQuote();   // 引用随消息发出即清空（下次引用覆盖）
            // 登记发送中 Promise 与 temp→real id 映射：供"发送后立即撤回"等待真实 id
            window.__confirmedMsgId = window.__confirmedMsgId || {};
            window.__pendingSendById = window.__pendingSendById || {};
            const sendP = (async () => {
                const body = {
                    chat_type: chatType,
                    target: target,
                    text: text,
                };
                if (curQuote) body.quote = curQuote;
                const res = await gatewayApi("/gateway/messages/send", "POST", body);
                if (res.ok && res.message) {
                    const row = document.querySelector(`[data-msg-id="${tempId}"]`);
                    if (row) {
                        row.dataset.msgId = res.message.id;
                        row.removeAttribute("data-local");
                        delete row.dataset.local;
                    }
                    window.__confirmedMsgId[tempId] = res.message.id;
                    syncQuoteId(tempId, res.message.id);   // 引用目标若为本地临时 ID，同步为真实 ID
                }
                loadConversations();
                return res;
            })();
            window.__pendingSendById[tempId] = sendP;
            try {
                await sendP;
                // 发送即视作已读：推进该会话已读位置（避免自己发的消息被算作未读）
                if (state.currentChat) markConversationRead(state.currentChat);
            } catch (err) {
                appendBubble("系统", err.message || "发送失败", "错误");
            } finally {
                delete window.__pendingSendById[tempId];
            }
            return;
        }

        // 发送者显示名：家长/老师用"学生名（关系）"，学生用账号名
        const sender = state.displayName || state.username || "群友";

        // 家长/老师没有自己的记忆体：未绑定学生时不得发起辉夜对话
        if (state.role !== "student" && !state.currentMemoryUser) {
            alert("请先在账号页绑定学生，再与该学生的记忆体对话。");
            return;
        }

        // 实时渲染：用户消息立即上屏，携带全局唯一 localId 供服务端返回时精确对齐转正
        const curQuote = state.currentQuote;
        const localId = genLocalId();
        const localMsgObj = {
            id: localId,
            localId: localId,
            sender: sender,
            content: text,
            source: "客户端",
            time: Date.now() / 1000
        };
        if (curQuote) localMsgObj.quote = curQuote;
        appendBubble(sender, text, "客户端", null, false, localMsgObj);
        const addedRow = document.querySelector(`[data-msg-id="${localId}"]`);
        if (addedRow) {
            addedRow.dataset.local = "1";
            addedRow.dataset.localId = localId;
        }
        localPending.push({ localId, sender, text, mtype: "text", quote: curQuote || null, time: localMsgObj.time });
        $("chatInput").value = "";
        clearQuote();   // 引用随消息发出即清空

        // 后台发送：不阻塞界面；Bot 回复由轮询按历史顺序比对与增量渲染
        const chatBody = {
            sender, text, mentioned: true,
        };
        if (curQuote) chatBody.quote = curQuote;
        if (state.currentMemoryUser) chatBody.memory_user_id = state.currentMemoryUser;
        // 发出后立即显示"辉夜正在思考…"转圈动画（收到回复或超时后自动隐藏）
        showThinking();
        gatewayApi("/gateway/chat", "POST", chatBody).then(() => {
            syncNewMessages();
            loadConversations();
        }).catch((e) => {
            hideThinking();   // 发送失败：撤掉本次思考提示
            appendBubble("系统", e.message || "请联系管理员启动认知循环", "错误");
        });
    }
    $("btnChatSend").addEventListener("click", sendChat);
    $("chatInput").addEventListener("keydown", (e) => {
        // @ 选人面板键盘导航优先（上下选择 / 回车确认 / ESC 关闭）
        const mp = $("mentionPicker");
        if (mp && mp.style.display !== "none" && mentionCandidates.length) {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                mentionSelectedIndex = (mentionSelectedIndex + 1) % mentionCandidates.length;
                updateMentionPickerSelection();
                return;
            }
            if (e.key === "ArrowUp") {
                e.preventDefault();
                mentionSelectedIndex = (mentionSelectedIndex - 1 + mentionCandidates.length) % mentionCandidates.length;
                updateMentionPickerSelection();
                return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                applyMentionSelection(mentionCandidates[mentionSelectedIndex].username);
                return;
            }
            if (e.key === "Escape") {
                e.preventDefault();
                closeMentionPicker();
                return;
            }
        }
        if (e.key === "Escape" && state.currentQuote) {
            e.preventDefault();
            clearQuote();
            return;
        }
        if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) {
            e.preventDefault();
            sendChat();
        }
        // Shift+Enter / Ctrl+Enter：默认换行
    });
    $("chatInput").addEventListener("input", () => {
        checkMentionTrigger();
    });
    $("chatInput").addEventListener("blur", () => {
        // 失焦稍作延迟再关闭，避免点击面板项时先触发 blur 导致选中失败
        setTimeout(() => closeMentionPicker(), 200);
    });

    // 监听输入框剪贴板图片直接粘贴发送 (Ctrl+V 截图即发)
    $("chatInput").addEventListener("paste", async (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        for (const item of items) {
            if (item.type && item.type.startsWith("image/")) {
                const file = item.getAsFile();
                if (file) {
                    e.preventDefault();
                    await uploadAndSendImage(file);
                    break;
                }
            }
        }
    });

    // ========== 图片消息：输入框左侧 "+" 上传图片 ==========
    const _chatImageInput = $("chatImageInput");
    const _btnChatImage = $("btnChatImage");

    if (_btnChatImage) {
        _btnChatImage.addEventListener("click", () => {
            if (_chatImageInput) _chatImageInput.click();
        });
    }

    async function sendImageChat(imageUrl, imageDesc = "") {
        if (!requireAuth()) return;
        const text = ($("chatInput") ? $("chatInput").value : "").trim();

        if (state.currentChat && state.currentChat.chat_type !== "bot") {
            const chatType = state.currentChat.chat_type;
            const target = state.currentChat.target;
            const tempId = `msg_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
            const curQuote = state.currentQuote;
            const msgObj = {
                id: tempId,
                sender: state.username,
                target: target,
                content: text || "[图片]",
                mtype: "image",
                image: imageUrl,
                time: Date.now() / 1000,
                recalled: false
            };
            if (curQuote) msgObj.quote = curQuote;
            appendBubble(state.username, text || "[图片]", "客户端", null, false, msgObj);
            const row = document.querySelector(`[data-msg-id="${tempId}"]`);
            if (row) row.dataset.local = "1";
            if ($("chatInput")) $("chatInput").value = "";
            clearQuote();
            try {
                const body = {
                    chat_type: chatType,
                    target: target,
                    text: text,
                    image: imageUrl,
                };
                if (curQuote) body.quote = curQuote;
                const res = await gatewayApi("/gateway/messages/send", "POST", body);
                if (res.ok && res.message) {
                    const row = document.querySelector(`[data-msg-id="${tempId}"]`);
                    if (row) {
                        row.dataset.msgId = res.message.id;
                        row.removeAttribute("data-local");
                        delete row.dataset.local;
                    }
                    if (window.__confirmedMsgId) window.__confirmedMsgId[tempId] = res.message.id;
                    syncQuoteId(tempId, res.message.id);   // 图片消息的引用目标 ID 同步
                }
                loadConversations();
            } catch (err) {
                appendBubble("系统", err.message || "图片发送失败", "错误");
            }
            return;
        }

        const sender = state.displayName || state.username || "群友";
        const curQuoteBot = state.currentQuote;
        const localId = genLocalId();
        const localMsgObj = {
            id: localId,
            localId: localId,
            sender: sender,
            content: text || "[图片]",
            source: "客户端",
            mtype: "image",
            image: imageUrl,
            time: Date.now() / 1000
        };
        if (curQuoteBot) localMsgObj.quote = curQuoteBot;
        // 实时渲染图片气泡（内容为图片，占位文本为"[图片]"）
        appendBubble(sender, text || "[图片]", "客户端", null, false, localMsgObj);
        const addedRow = document.querySelector(`[data-msg-id="${localId}"]`);
        if (addedRow) {
            addedRow.dataset.local = "1";
            addedRow.dataset.localId = localId;
        }
        localPending.push({ localId, sender, text: text || "[图片]", mtype: "image", image: imageUrl, quote: curQuoteBot || null, time: localMsgObj.time });
        if ($("chatInput")) $("chatInput").value = "";
        clearQuote();
        const chatBody = { sender, text: text || "", image: imageUrl, image_desc: imageDesc || "", mentioned: true };
        if (curQuoteBot) chatBody.quote = curQuoteBot;
        if (state.currentMemoryUser) chatBody.memory_user_id = state.currentMemoryUser;
        showThinking();   // 图片同样会走认知分析，显示思考动画
        try {
            await gatewayApi("/gateway/chat", "POST", chatBody);
            syncNewMessages();
            loadConversations();
        } catch (e) {
            hideThinking();
            appendBubble("系统", e.message || "图片发送失败", "错误");
        }
    }

    // ========== 图片压缩：上传前自动压到 200KB 以内 ==========
    // 策略：保持长宽比，先按最长边逐级下采样（1600→1280→1024→800→640），
    // 每一档内再从高质量到低质量扫描，命中 ≤上限即返回；全部试完仍超标则返回最小的那份。
    // 压缩失败（浏览器不支持该格式等）时回退原文件，保证上传不中断。
    const _IMG_MAX_BYTES = 200 * 1024;

    function _canvasToBlob(canvas, quality) {
        return new Promise((resolve) => {
            try {
                canvas.toBlob((b) => resolve(b), "image/jpeg", quality);
            } catch (e) { resolve(null); }
        });
    }

    async function compressImageUnder(file, maxBytes = _IMG_MAX_BYTES) {
        if (!file || !/^image\//.test(file.type || "")) return file;
        if (file.size <= maxBytes) return file;   // 已达标，原样上传

        const url = URL.createObjectURL(file);
        const img = await new Promise((resolve) => {
            const im = new Image();
            im.onload = () => resolve(im);
            im.onerror = () => resolve(null);
            im.src = url;
        });
        if (!img || !img.width || !img.height) {
            try { URL.revokeObjectURL(url); } catch (_) {}
            return file;   // 无法解码（如某些 GIF/WebP）：回退原图
        }

        const origW = img.width, origH = img.height;
        const edges = [1600, 1280, 1024, 800, 640];
        // 若原图比第一档还小，只按原尺寸重编码
        const candidates = edges.filter(e => e < Math.max(origW, origH));
        candidates.push(Math.min(Math.max(origW, origH), edges[edges.length - 1]));

        let best = null;   // 记录目前体积最小的结果，作为兜底
        for (const edge of candidates) {
            const scale = Math.min(1, edge / Math.max(origW, origH));
            const tw = Math.max(1, Math.round(origW * scale));
            const th = Math.max(1, Math.round(origH * scale));
            const canvas = document.createElement("canvas");
            canvas.width = tw; canvas.height = th;
            const ctx = canvas.getContext("2d");
            if (!ctx) continue;
            ctx.fillStyle = "#ffffff";   // JPEG 无透明通道，透明区域填白避免黑底
            ctx.fillRect(0, 0, tw, th);
            ctx.drawImage(img, 0, 0, tw, th);
            // 每一档内从高到低扫描质量，命中即返回
            for (const q of [0.85, 0.75, 0.65, 0.55, 0.45]) {
                const blob = await _canvasToBlob(canvas, q);
                if (!blob) continue;
                if (!best || blob.size < best.size) {
                    best = new File([blob], (file.name || "image").replace(/\.[^.]+$/, "") + ".jpg",
                                    { type: "image/jpeg", lastModified: Date.now() });
                }
                if (blob.size <= maxBytes) {
                    try { URL.revokeObjectURL(url); } catch (_) {}
                    return new File([blob], (file.name || "image").replace(/\.[^.]+$/, "") + ".jpg",
                                    { type: "image/jpeg", lastModified: Date.now() });
                }
            }
        }
        try { URL.revokeObjectURL(url); } catch (_) {}
        // 兜底：返回尝试过的最小体积结果；若还不如原图，就用原图
        if (best && best.size < file.size) return best;
        return file;
    }

    async function uploadAndSendImage(file) {
        if (!file) return;
        if (file.size > 10 * 1024 * 1024) { appendBubble("系统", "图片过大（限制 10MB）", "错误"); return; }
        try {
            // 本地先压缩到 200KB 以内再上传（明显加快上传速度、节省流量；
            // 压缩失败或结果反而更大时会自动回退原文件）
            let upFile = file;
            try {
                upFile = await compressImageUnder(file, _IMG_MAX_BYTES);
            } catch (e) { upFile = file; }
            const fd = new FormData();
            fd.append("file", upFile, upFile.name || file.name || "image.jpg");
            const resp = await fetch(state.gateway.replace(/\/+$/, "") + "/gateway/chat_image", {
                method: "POST",
                headers: { "Authorization": `Bearer ${state.token}` },
                body: fd,
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || !data.ok) {
                appendBubble("系统", data.message || "图片上传失败", "错误");
                return;
            }
            await sendImageChat(data.url, data.image_desc);
        } catch (e) {
            appendBubble("系统", e.message || "图片上传失败", "错误");
        }
    }

    if (_chatImageInput) {
        _chatImageInput.addEventListener("change", async () => {
            const file = _chatImageInput.files && _chatImageInput.files[0];
            _chatImageInput.value = "";   // 允许重复选择同一文件
            await uploadAndSendImage(file);
        });
    }

    // ========== 头像压缩：上传前用 canvas 等比缩放 + 转 JPEG，显著减小体积 ==========
    // 头像显示尺寸很小（列表 42px / 气泡 30px），无需保留原图分辨率；
    // 默认最长边 256、JPEG 质量 0.82（与服务端压缩规格一致，服务端还会再兜底一次）。
    // 压缩失败（如某些浏览器对 webp/gif 的处理差异）时回退原文件，保证上传不中断。
    function compressImageFile(file, maxEdge = 256, quality = 0.82) {
        return new Promise((resolve) => {
            if (!file || !/^image\//.test(file.type || "")) { resolve(file); return; }
            const url = URL.createObjectURL(file);
            const img = new Image();
            img.onload = () => {
                try {
                    let { width: w, height: h } = img;
                    if (!w || !h) { URL.revokeObjectURL(url); resolve(file); return; }
                    const scale = Math.min(1, maxEdge / Math.max(w, h));
                    const tw = Math.max(1, Math.round(w * scale));
                    const th = Math.max(1, Math.round(h * scale));
                    const canvas = document.createElement("canvas");
                    canvas.width = tw; canvas.height = th;
                    const ctx = canvas.getContext("2d");
                    if (!ctx) { URL.revokeObjectURL(url); resolve(file); return; }
                    // 白底：JPEG 不支持透明，PNG/透明图直接压成 JPEG 会出现黑底
                    ctx.fillStyle = "#ffffff";
                    ctx.fillRect(0, 0, tw, th);
                    ctx.drawImage(img, 0, 0, tw, th);
                    canvas.toBlob((blob) => {
                        URL.revokeObjectURL(url);
                        if (!blob || blob.size >= file.size) { resolve(file); return; }
                        resolve(new File([blob], (file.name || "avatar").replace(/\.[^.]+$/, "") + ".jpg",
                                         { type: "image/jpeg", lastModified: Date.now() }));
                    }, "image/jpeg", quality);
                } catch (e) {
                    try { URL.revokeObjectURL(url); } catch (_) {}
                    resolve(file);
                }
            };
            img.onerror = () => { try { URL.revokeObjectURL(url); } catch (_) {} resolve(file); };
            img.src = url;
        });
    }
    const _accountAvatarInput = $("accountAvatarInput");
    if (_accountAvatarInput) {
        _accountAvatarInput.addEventListener("change", async () => {
            const file = _accountAvatarInput.files && _accountAvatarInput.files[0];
            _accountAvatarInput.value = "";   // 允许重复选择同一文件
            if (!file) return;
            if (file.size > 10 * 1024 * 1024) { if ($("ageHint")) $("ageHint").textContent = "头像过大（限制 10MB）"; return; }
            try {
                // 上传前压缩（等比缩放 + JPEG），减少传输体积；失败自动回退原图
                const upFile = await compressImageFile(file, 256, 0.82);
                const fd = new FormData();
                fd.append("file", upFile);
                const resp = await fetch(state.gateway.replace(/\/+$/, "") + "/gateway/avatar", {
                    method: "POST",
                    headers: { "Authorization": `Bearer ${state.token}` },
                    body: fd,
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok || !data.ok) {
                    const hint = $("ageHint");
                    if (hint) hint.textContent = data.message || "头像上传失败";
                    return;
                }
                // 头像为固定路径，覆盖后浏览器可能仍用缓存旧图：
                // 自增版本号，令 userAvatarUrl 生成 ?v=<ts> 强制刷新
                _avatarVer = Date.now();
                state.userAvatar = userAvatarUrl(state.username) || data.url || "";
                const displayUrl = state.userAvatar;
                renderAccountMenu();   // 更新账号页头像
                // 重绘聊天区用户消息头像
                document.querySelectorAll("#chatView .msg-row.user .msg-avatar").forEach(av => {
                    const ini = (state.username || "我").slice(0, 1).toUpperCase();
                    av.innerHTML = "";
                    if (displayUrl) {
                        const im = document.createElement("img");
                        im.className = "msg-avatar-img"; im.alt = ""; im.src = displayUrl;
                        im.onerror = () => { im.onerror = null; av.textContent = ini; };
                        av.appendChild(im);
                    } else av.textContent = ini;
                });
            } catch (e) {
                const hint = $("ageHint");
                if (hint) hint.textContent = e.message || "头像上传失败";
            }
        });
    }

    // ========== 绑定学生（家长/老师） ==========
    async function doBindStudent() {
        const sid = ($("bindInput") ? $("bindInput").value : "").trim();
        if (!sid) { $("bindInput").placeholder = "请输入学生 user_id"; return; }
        const relation = ($("bindRelationInput") ? $("bindRelationInput").value : "").trim();
        try {
            const r = await gatewayApi("/gateway/bind", "POST", {
                student_user_id: sid, relation,
            });
            state.linkedStudents = r.linked_students || state.linkedStudents;
            // 绑定成功后默认切换到该学生记忆体
            if (state.linkedStudents.length) {
                const last = state.linkedStudents[state.linkedStudents.length - 1];
                const suid = last.user_id || last;
                state.currentMemoryUser = suid;
                state.displayName = (last.student_name || suid) + (last.relation ? `（${last.relation}）` : "");
                localStorage.setItem("gateway_memory_user", suid);
            }
            $("bindInput").value = "";
            $("bindRelationInput").value = "";
            renderAccountMenu();
            loadHistory();
            appendBubble("系统", r.message || "连接成功", "系统");
        } catch (e) {
            appendBubble("系统", e.message || "连接失败", "系统");
        }
    }
    const btnBind = $("btnBind");
    if (btnBind) btnBind.addEventListener("click", doBindStudent);
    const bindInput = $("bindInput");
    if (bindInput) bindInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doBindStudent(); });

    // ========== 退出登录 ==========
    function doLogout(reason = "") {
        stopMessagePolling();
        if (ws) {
            try { ws.close(); } catch (e) {}
            ws = null;
        }
        if (wsReconnectTimer) {
            clearTimeout(wsReconnectTimer);
            wsReconnectTimer = null;
        }
        state.loggedIn = false;
        state.token = "";
        state.username = "";
        state.userId = "";
        state.role = "student";
        state.linkedStudents = [];
        state.currentMemoryUser = "";
        state.displayName = "";
        // 清空头像，避免登出后仍显示上一位用户的头像（未清缓存场景）
        state.userAvatar = "";
        _avatarVer = 0;
        localPending = [];
        clearQuote();        // 清空引用状态与预览条
        closeMentionPicker();
        state.cachedGroupMembers = {};
        hideThinking(true);   // 收起可能仍在显示的"正在思考…"提示
        closeBroadcast();     // 关闭可能打开的公告弹窗
        // 退出登录：关闭抽奖面板并清空本地抽奖状态，避免下一个账号看到上一个账号的数据
        closeLotteryModal();
        lotteryState.groupId = "";
        lotteryState.room = null;
        lotteryState.members = [];
        lotteryState.selected = new Set();
        lotteryState.inviteSelected = new Set();
        lotteryState.inviteOpen = false;
        lotteryState.error = "";
        lotteryState.loading = false;
        lotteryState.requestSeq++;
        setLotteryDot(false);
        // 清除"不再提醒"记录：换账号登录时应重新看到公告
        try { localStorage.removeItem("broadcast_muted_key"); } catch (e) {}
        try {
            localStorage.removeItem("cache_conversations");
            localStorage.removeItem("cache_friends");
            localStorage.removeItem("cache_groups");
            localStorage.removeItem("cache_read_positions");
        } catch (e) {}
        localStorage.removeItem("gateway_token");
        localStorage.removeItem("gateway_user");
        localStorage.removeItem("gateway_role");
        localStorage.removeItem("gateway_userid");
        localStorage.removeItem("gateway_signkey");
        localStorage.removeItem("gateway_memory_user");
        const chatView = $("chatView");
        if (chatView) chatView.innerHTML = "";
        const welcome = $("welcome");
        if (welcome) welcome.style.display = "";
        if ($("authPassword")) $("authPassword").value = "";
        if ($("authConfirmPassword")) $("authConfirmPassword").value = "";
        showAuthOverlay();
        renderAccountMenu();
        const authHint = $("authHint");
        if (authHint) {
            authHint.textContent = reason || "";
        }
    }
    const btnLogout = $("btnLogout");
    if (btnLogout) {
        btnLogout.addEventListener("click", (e) => {
            e.stopPropagation();
            if (typeof closeNav === "function") closeNav();
            closeAccountCard();
            doLogout();
        });
    }

    // ========== 导航站（顶栏 LOGO 点击展开，logo 从顶栏飞到中心） ==========
    // nav_script.js 负责 logo 点击→展开菜单（state 0→openLevel1）及 ESC→closeAll。
    // 这里只负责遮罩/标签卡层级联动与恢复顶栏。
    function resetNavScriptState() {
        if (typeof window.__closeNavStation === "function") {
            window.__closeNavStation();
        } else {
            // 触发原导航站的 ESC 处理器（closeAll），把其内部 state 归零，保证下次可正常展开
            document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
        }
    }
    function openNav() {
        if (!requireAuth()) return;
        resetNavScriptState();
        state.navOpen = true;
        logoContainer.style.opacity = "";
        navMask.classList.add("nav-open");
        navStation.classList.add("nav-open");
        const chatPage = $("chat-page");
        if (chatPage) chatPage.classList.add("hidden");
        const backBtn = $("btnBackChat");
        if (backBtn) backBtn.classList.add("visible");
        // 隐藏顶栏 LOGO 按钮，让导航站的 logo 从该位置飞向中心
        $("btnOpenNav").style.visibility = "hidden";
        // 延迟触发展开一级菜单
        setTimeout(() => {
            if (typeof window.__openNavLevel1 === "function") {
                window.__openNavLevel1();
            }
        }, 500);
    }
    let isClosingNav = false;
    function closeNav() {
        if (isClosingNav) return;
        isClosingNav = true;
        try {
            state.navOpen = false;
            navMask.classList.remove("nav-open");
            navStation.classList.remove("nav-open");
            const chatPage = $("chat-page");
            if (chatPage) chatPage.classList.remove("hidden");
            $("btnOpenNav").style.visibility = "";
            const backBtn = $("btnBackChat");
            if (backBtn) backBtn.classList.remove("visible");
            // 恢复导航站 logo 并直接隐形，避免向顶栏过渡时发生缩放膨胀
            logoContainer.style.opacity = "0";
            logoContainer.classList.remove("active");
            if (typeof window.__closeNavStation === "function") {
                window.__closeNavStation();
            }
            setTimeout(() => {
                const menuLayer = $("menu-layer");
                if (menuLayer) {
                    menuLayer.style.opacity = "0";
                    menuLayer.style.pointerEvents = "none";
                    menuLayer.innerHTML = "";
                }
            }, 500);
            // 从导航菜单切换过的主题同步到顶栏按钮
            readThemeFromNav();
            applyTheme();
        } finally {
            isClosingNav = false;
        }
    }
    $("btnOpenNav").addEventListener("click", openNav);
    // 「返回对话页」按钮：导航站 / 账号卡片 / 隐私政策卡片共用
    $("btnBackChat").addEventListener("click", (e) => {
        e.stopPropagation();
        if (adsOverlay && adsOverlay.classList.contains("open")) { closeAds(); return; }
        if (privacyOverlay && privacyOverlay.classList.contains("open")) { closePrivacyPolicy(); return; }
        if (accountOverlay.classList.contains("open")) { closeAccountCard(); return; }
        closeNav();
    });
    window.__onNavClosed = closeNav;
    window.addEventListener("navStationClosed", closeNav);

    // ESC：关闭导航站（回退对话页）或登录弹窗
    // 说明：nav_script.js 自身也监听 ESC 并执行 closeAll（菜单收起、state 归零），
    // 本处理器负责把遮罩/标签卡层级收起回对话页，二者互补。
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            if (adsOverlay && adsOverlay.classList.contains("open")) { closeAds(); return; }
            if (privacyOverlay && privacyOverlay.classList.contains("open")) { closePrivacyPolicy(); return; }
            if (accountOverlay.classList.contains("open")) { closeAccountCard(); return; }
            if (ageOverlay && ageOverlay.classList.contains("open")) { closeAgeCard(); return; }
            if (state.navOpen) { closeNav(); return; }
            if (authOverlay.classList.contains("active") && !state.loggedIn) return;
            if (authOverlay.classList.contains("active")) hideAuthOverlay();
        }
    });

    // 点击隐私卡片外部（overlay 背景）关闭
    if (privacyOverlay) {
        privacyOverlay.addEventListener("click", (e) => {
            if (e.target === privacyOverlay) closePrivacyPolicy();
        });
    }

    // 点击赞助卡片外部（overlay 背景）关闭
    if (adsOverlay) {
        adsOverlay.addEventListener("click", (e) => {
            if (e.target === adsOverlay) closeAds();
        });
    }

    // 点击账号卡片外部（overlay 背景）关闭；卡片内部点击不触发关闭
    if (accountOverlay) {
        accountOverlay.addEventListener("click", (e) => {
            if (e.target === accountOverlay) closeAccountCard();
        });
    }

    // 点击年龄确认卡片外部（overlay 背景）回退到登录/注册界面；卡片内部点击不触发关闭
    if (ageOverlay) {
        ageOverlay.addEventListener("click", (e) => {
            if (e.target === ageOverlay) closeAgeCard();
        });
    }

    // 点击导航站的 logo 展开菜单（由 nav_script.js 处理），无需在此重复逻辑。

    // ========== 像素网格（对话标签卡顶部，灰色渐变至主题色，自适应宽度） ==========
    let breathTimers = [];
    function stopBreathing() { breathTimers.forEach(t => clearTimeout(t)); breathTimers = []; }

    function getDynamicColumns(container) {
        const width = (container && container.offsetWidth) || 860;
        const cellSize = 5;
        return Math.max(48, Math.floor(width / cellSize));
    }

    function generateGridData(cols = 48) {
        const rows = 3;
        let gridRows = [];
        let breathableIndices = [];
        const cutoff1 = Math.round(cols * 0.25);
        const cutoff2 = Math.round(cols * 0.45);
        const cutoff3 = Math.round(cols * 0.70);
        const cutoffBreath = Math.round(cols * 0.50);

        for (let r = 0; r < rows; r++) {
            let row = [];
            for (let c = 0; c < cols; c++) {
                let isFilled = false;
                if (c < cutoff1) isFilled = true;
                else if (c < cutoff2) isFilled = Math.random() < 0.75;
                else if (c < cutoff3) isFilled = Math.random() < 0.45;
                else isFilled = Math.random() < 0.18;
                row.push(isFilled);
                if (c >= cutoffBreath && Math.random() < 0.35) {
                    breathableIndices.push(r * cols + c);
                }
            }
            gridRows.push(row);
        }
        return { gridRows, breathableIndices, cols };
    }

    function startBreathing(cardElement, breathableIndices) {
        if (!cardElement) return;
        const pixelGrid = cardElement.querySelector('.pixel-grid');
        if (!pixelGrid) return;
        const cells = pixelGrid.children;
        if (!cells.length) return;
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        const ratePerSecond = isMobile() ? 1 : 4;
        let pool = breathableIndices.slice();
        if (pool.length === 0) return;
        function breatheOne() {
            if (!cardElement || !pixelGrid.parentNode) return;
            const randIdx = Math.floor(Math.random() * pool.length);
            const index = pool[randIdx];
            if (index < cells.length) {
                cells[index].classList.toggle('filled');
            }
            const timer = setTimeout(breatheOne, 1000 / ratePerSecond);
            breathTimers.push(timer);
        }
        const delay = Math.random() * 1000;
        const startTimer = setTimeout(breatheOne, delay);
        breathTimers.push(startTimer);
    }

    function buildPixelGrid(container) {
        if (!container) return [];
        container.innerHTML = "";
        const cols = getDynamicColumns(container);
        const { gridRows, breathableIndices } = generateGridData(cols);
        gridRows.forEach(rowArr => rowArr.forEach((filled, colIdx) => {
            const cell = document.createElement("div");
            cell.className = "pixel-cell" + (filled ? " filled" : "");
            const ratio = Math.round((colIdx / Math.max(1, cols - 1)) * 100);
            cell.style.setProperty('--grad-pos', `${ratio}%`);
            container.appendChild(cell);
        }));
        return breathableIndices;
    }

    function initChatPixelGrid() {
        const container = $("chatPixelGrid");
        if (container) {
            const breathable = buildPixelGrid(container);
            startBreathing($("chat-card"), breathable);
        }
    }

    function initAccountPixelGrid() {
        const container = $("accountPixelGrid");
        if (container) {
            const breathable = buildPixelGrid(container);
            startBreathing($("account-card"), breathable);
        }
    }

    initChatPixelGrid();
    window.addEventListener("resize", () => {
        clearTimeout(window._pixelResizeTimer);
        window._pixelResizeTimer = setTimeout(() => {
            initChatPixelGrid();
            if (accountOverlay && accountOverlay.classList.contains("open")) {
                initAccountPixelGrid();
            }
        }, 250);
    });

    // ========== 隐私政策（Markdown 渲染 + 0.5s 渐显/渐隐标签卡） ==========
    let privacyFromAuth = false;   // 是否从登录界面打开（返回按钮回登录界面而非对话页）
    let isClosingPrivacy = false;

    function escapeHtmlStr(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // 轻量 Markdown 渲染器：支持 #/##/### 标题、**粗体**、*斜体*、~~删除线~~、`行内代码`、
    // ```代码块```、- 列表、1. 有序列表、> 引用、--- 分割线、[文字](url) 链接、| 表格 |、段落
    // 说明：先转义 HTML 再生成标签，杜绝注入；隐私政策与全站广播共用此实现。
    function renderMarkdown(md) {
        const src = String(md || "").replace(/\r\n?/g, "\n");
        const codeBlocks = [];
        // 先抽出围栏代码块（避免内部被当作 Markdown 解析），留占位符后续替换
        const prepared = src.replace(/```([\s\S]*?)```/g, (m, code) => {
            codeBlocks.push(`<pre><code>${escapeHtmlStr(code.replace(/^\n/, ""))}</code></pre>`);
            return `\u0000BLOCK${codeBlocks.length - 1}\u0000`;
        });

        const lines = prepared.split("\n");
        let html = "";
        let inTable = false;
        let inList = null;      // "ul" | "ol"
        const closeList = () => { if (inList) { html += `</${inList}>`; inList = null; } };
        const closeTable = () => { if (inTable) { html += "</table>"; inTable = false; } };
        const inline = (t) => {
            let s = escapeHtmlStr(t);
            s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
            s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1">');
            s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
            s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
            s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
            s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
            s = s.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");
            s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
            return s;
        };
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            const trimmed = line.trim();
            // 代码块占位符
            const ph = trimmed.match(/^\u0000BLOCK(\d+)\u0000$/);
            if (ph) { closeList(); closeTable(); html += codeBlocks[Number(ph[1])]; continue; }
            if (!trimmed) { closeList(); closeTable(); continue; }
            // 分割线
            if (/^([-*_])\s*\1\s*\1[\s\1]*$/.test(trimmed)) { closeList(); closeTable(); html += "<hr>"; continue; }
            // 表格行
            if (trimmed.startsWith("|")) {
                closeList();
                const cells = trimmed.split("|").filter((c, idx, arr) => !(idx === 0 && c.trim() === "") && !(idx === arr.length - 1 && c.trim() === "")).map(c => c.trim());
                // 分隔行（|---|）跳过
                if (cells.length && cells.every(c => /^:?-{2,}:?$/.test(c))) { continue; }
                if (!inTable) { inTable = true; html += "<table>"; }
                // 判断表头：上一行是表头分隔行时已跳过，这里简化：第一张表视为表头
                html += "<tr>" + cells.map(c => `<td>${inline(c)}</td>`).join("") + "</tr>";
                continue;
            }
            closeTable();
            // 标题
            const h = trimmed.match(/^(#{1,6})\s+(.*)$/);
            if (h) {
                closeList();
                const lv = Math.min(h[1].length, 4);
                html += `<h${lv}>${inline(h[2])}</h${lv}>`;
                continue;
            }
            // 引用
            const q = trimmed.match(/^>\s?(.*)$/);
            if (q) { closeList(); html += `<blockquote>${inline(q[1])}</blockquote>`; continue; }
            // 无序列表
            const li = trimmed.match(/^[-*+]\s+(.*)$/);
            if (li) {
                if (inList !== "ul") { closeList(); inList = "ul"; html += "<ul>"; }
                html += `<li>${inline(li[1])}</li>`;
                continue;
            }
            // 有序列表
            const oli = trimmed.match(/^\d+[.)]\s+(.*)$/);
            if (oli) {
                if (inList !== "ol") { closeList(); inList = "ol"; html += "<ol>"; }
                html += `<li>${inline(oli[1])}</li>`;
                continue;
            }
            closeList();
            // 普通段落
            html += `<p>${inline(trimmed)}</p>`;
        }
        closeList(); closeTable();
        return html;
    }

    let _privacyMdCache = null;
    async function getPrivacyMarkdown() {
        if (_privacyMdCache !== null) return _privacyMdCache;
        try {
            const resp = await fetch("privacy_policy.md", { cache: "no-cache" });
            if (resp.ok) { _privacyMdCache = await resp.text(); return _privacyMdCache; }
        } catch (e) { /* fallback */ }
        return "# 隐私政策\n\n内容加载失败，请刷新重试。";
    }

    function openPrivacyPolicy(fromAuth = false) {
        if (!privacyOverlay || isClosingPrivacy) return;
        privacyFromAuth = !!fromAuth;
        // 先打开卡片（无论 Markdown 是否加载成功），内容异步填充
        const mask = $("nav-mask");
        if (mask) mask.classList.add("nav-open");
        const backBtn = $("btnBackChat");
        if (backBtn) {
            backBtn.classList.add("visible");
            backBtn.textContent = fromAuth ? "← 返回登录" : "← 返回对话页";
        }
        if (privacyCard) privacyCard.classList.remove("privacy-closing");
        privacyOverlay.classList.add("open");
        // 异步渲染 Markdown 内容（加载失败也兜底显示）
        getPrivacyMarkdown().then((md) => {
            const body = $("privacyBody");
            if (body) body.innerHTML = renderMarkdown(md);
        }).catch(() => {
            const body = $("privacyBody");
            if (body) body.innerHTML = "<p>内容加载失败，请刷新重试。</p>";
        });
    }

    function closePrivacyPolicy() {
        if (!privacyOverlay || isClosingPrivacy) return;
        if (!privacyOverlay.classList.contains("open")) return;
        isClosingPrivacy = true;
        if (privacyCard) privacyCard.classList.add("privacy-closing");
        const mask = $("nav-mask");
        if (mask && !state.navOpen) mask.classList.remove("nav-open");
        const backBtn = $("btnBackChat");
        if (backBtn && !state.navOpen) backBtn.classList.remove("visible");
        setTimeout(() => {
            privacyOverlay.classList.remove("open");
            if (privacyCard) privacyCard.classList.remove("privacy-closing");
            isClosingPrivacy = false;
        }, 500);  // 0.5s 渐隐
    }

    // 暴露全局钩子供导航站"隐私政策"卡片调用
    window.__openPrivacyPolicy = openPrivacyPolicy;
    window.__closePrivacyPolicy = closePrivacyPolicy;

    // ========== 服务赞助（广告轮播）标签卡 ==========
    const ADS = ["images/ads/ad1.jpg", "images/ads/ad2.jpg", "images/ads/ad3.jpg"];
    const ADS_INTERVAL = 5000;   // 无操作时 5 秒自动切下一张
    let adsIndex = 0;
    let adsTimer = null;
    let isClosingAds = false;

    function renderAdsDots() {
        const dots = $("adsDots");
        if (!dots) return;
        dots.innerHTML = "";
        ADS.forEach((_, i) => {
            const d = document.createElement("div");
            d.className = "ads-dot" + (i === adsIndex ? " active" : "");
            d.addEventListener("click", (e) => { e.stopPropagation(); showAd(i); resetAdsTimer(); });
            dots.appendChild(d);
        });
        const counter = $("adsCounter");
        if (counter) counter.textContent = `${adsIndex + 1} / ${ADS.length}`;
    }

    function showAd(i) {
        adsIndex = (i + ADS.length) % ADS.length;
        const img = $("adsImg");
        if (img) {
            img.classList.add("switching");
            const next = ADS[adsIndex];
            setTimeout(() => { img.src = next; img.classList.remove("switching"); }, 180);
        }
        renderAdsDots();
    }

    function nextAd() { showAd(adsIndex + 1); }
    function resetAdsTimer() {
        if (adsTimer) clearInterval(adsTimer);
        adsTimer = setInterval(nextAd, ADS_INTERVAL);
    }
    function stopAdsTimer() { if (adsTimer) { clearInterval(adsTimer); adsTimer = null; } }
    function onAdsWheel(e) {
        // 滚轮滑动：切下一张，并重置自动计时（这样"有滚轮就不再等到超时"）
        e.preventDefault();
        nextAd();
        resetAdsTimer();
    }

    function openAds() {
        if (!adsOverlay || isClosingAds) return;
        const mask = $("nav-mask");
        if (mask) mask.classList.add("nav-open");
        const backBtn = $("btnBackChat");
        if (backBtn) { backBtn.classList.add("visible"); backBtn.textContent = "← 返回对话页"; }
        if (adsCard) adsCard.classList.remove("ads-closing");
        adsIndex = 0;
        showAd(0);
        adsOverlay.classList.add("open");
        const stage = $("adsStage");
        if (stage) stage.addEventListener("wheel", onAdsWheel, { passive: false });
        resetAdsTimer();
    }

    function closeAds() {
        if (!adsOverlay || isClosingAds) return;
        if (!adsOverlay.classList.contains("open")) return;
        isClosingAds = true;
        stopAdsTimer();
        const stage = $("adsStage");
        if (stage) stage.removeEventListener("wheel", onAdsWheel);
        if (adsCard) adsCard.classList.add("ads-closing");
        const mask = $("nav-mask");
        if (mask && !state.navOpen) mask.classList.remove("nav-open");
        const backBtn = $("btnBackChat");
        if (backBtn && !state.navOpen) backBtn.classList.remove("visible");
        setTimeout(() => {
            adsOverlay.classList.remove("open");
            if (adsCard) adsCard.classList.remove("ads-closing");
            isClosingAds = false;
        }, 400);
    }
    window.__openAds = openAds;
    window.__closeAds = closeAds;

    // 点击广告图放大查看
    const adsStage = $("adsStage");
    const adLightbox = $("ad-lightbox");
    const adLightboxImg = $("adLightboxImg");
    if (adsStage && adLightbox && adLightboxImg) {
        adsStage.addEventListener("click", (e) => {
            const img = e.target.closest("img");
            if (!img) return;
            adLightboxImg.src = img.src;
            adLightbox.classList.add("open");
        });
        adLightbox.addEventListener("click", () => adLightbox.classList.remove("open"));
    }

    // 登录界面"《隐私政策》"链接
    const authPrivacyLink = $("authPrivacyLink");
    if (authPrivacyLink) {
        authPrivacyLink.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            openPrivacyPolicy(true);
        });
    }

    // ========== 年龄确认标签卡（学生注册） ==========
    let _pendingRegister = null;   // 暂存未完成的注册数据
    let _ageConfirmed = false;     // 是否已确认满14
    let _guardianUid = "";         // 不满14时填的家长/教师 user_id

    function openAgeCard() {
        if (!ageOverlay) return;
        ageOverlay.classList.add("open");
        $("ageHint").textContent = "";
        // 重置监护人区域
        _ageConfirmed = false; _guardianUid = "";
        $("ageGuardianArea").style.display = "none";
        $("ageGuardianInput").value = "";
    }
    function closeAgeCard() {
        if (!ageOverlay) return;
        ageOverlay.classList.remove("open");
    }
    $("btnAgeYes").addEventListener("click", () => {
        _ageConfirmed = true; _guardianUid = "";
        closeAgeCard();
        completeStudentRegister();
    });
    $("btnAgeNo").addEventListener("click", () => {
        $("ageGuardianArea").style.display = "block";
    });
    $("btnAgeGuardianConfirm").addEventListener("click", () => {
        const gid = ($("ageGuardianInput") ? $("ageGuardianInput").value : "").trim().toUpperCase();
        if (!gid) { $("ageHint").textContent = "请输入家长/教师 user_id"; return; }
        _ageConfirmed = true; _guardianUid = gid;
        closeAgeCard();
        completeStudentRegister();
    });

    function startAgeConfirmation(regData) {
        _pendingRegister = regData;
        openAgeCard();
    }
    async function completeStudentRegister() {
        const p = _pendingRegister;
        if (!p) return;
        const btn = $("btnAuthSubmit");
        btn.disabled = true;
        try {
            const body = { username: p.username, password: p.password, role: "student", age: _ageConfirmed ? ( _guardianUid ? 13 : 18 ) : null };
            if (_guardianUid) body.guardian_user_id = _guardianUid;
            const r = await gatewayApi("/gateway/register", "POST", body);
            if (!r.ok) {
                // 若监护人 id 无效，回到年龄卡提示
                if (r.message && (r.message.includes("家长") || r.message.includes("教师"))) {
                    $("ageHint").textContent = r.message;
                    openAgeCard();
                    return;
                }
                $("authHint").textContent = r.message;
                return;
            }
            state.loggedIn = true;
            state.token = r.token;
            state.username = r.username;
            state.signKey = r.sign_key || "";
            state.role = r.role || "student";
            state.userId = r.user_id || "";
            state.linkedStudents = r.linked_students || [];
            state.minorMode = r.minor_mode || false;
            // 头像随注册结果重置（新用户为空 → 回退字母）
            state.userAvatar = r.avatar || "";
            _avatarVer = 0;
            localStorage.setItem("gateway_token", r.token);
            localStorage.setItem("gateway_user", r.username);
            localStorage.setItem("gateway_role", state.role);
            localStorage.setItem("gateway_userid", state.userId);
            if (state.signKey) localStorage.setItem("gateway_signkey", state.signKey);
            else localStorage.removeItem("gateway_signkey");
            $("authHint").textContent = "";
            _pendingRegister = null;
            await enterChat();
        } catch (e) {
            $("authHint").textContent = e.message;
        } finally {
            btn.disabled = false;
        }
    }

    // ========== 注销账号 ==========
    const btnDeleteAccount = $("btnDeleteAccount");
    if (btnDeleteAccount) {
        btnDeleteAccount.addEventListener("click", async (e) => {
            e.stopPropagation();
            const isStudent = state.role === "student";
            const msg = isStudent
                ? "确认注销账号？将彻底删除您的全部数据与记忆体，此操作不可恢复。"
                : "确认注销账号？仅删除您的账户，不影响已连接学生的记忆体。";
            if (!confirm(msg)) return;
            try {
                const r = await gatewayApi("/gateway/delete_account", "POST", {});
                if (!r.ok) throw new Error(r.message || "注销失败");
                // 清空本地状态并回到登录页
                doLogout("账号已注销");
            } catch (err) {
                appendBubble("系统", err.message || "注销失败", "错误");
            }
        });
    }

    // ========== 未成年人模式开关（家长/老师端） ==========
    // 在账号卡绑定学生区，为选定的学生提供未成年模式开关
    function buildMinorModeToggle() {
        const linkedEl = $("linkedList");
        if (!linkedEl) return;
        // 已连接学生 chip 区由 renderAccountMenu 渲染，这里仅在家长/老师端附加开关
        const parentBtn = document.getElementById("minorModeToggle");
        if (parentBtn) parentBtn.remove();
        if (state.role === "parent" || state.role === "teacher") {
            const row = document.createElement("div");
            row.className = "account-row minor-mode-row";
            row.id = "minorModeToggle";
            row.innerHTML = `<label class="minor-mode-label">
                <input type="checkbox" id="minorModeCheck"> 开启未成年模式（当前学生）
              </label>`;
            linkedEl.appendChild(row);
            const cb = $("minorModeCheck");
            if (cb) {
                // 当前学生 minor 状态查询（家长端视角：基于 currentMemoryUser）
                cb.addEventListener("change", async () => {
                    const sid = state.currentMemoryUser;
                    if (!sid) { appendBubble("系统", "请先选择学生", "系统"); return; }
                    try {
                        const r = await gatewayApi("/gateway/minor_mode", "POST", {
                            student_user_id: sid, enabled: cb.checked,
                        });
                        if (r.ok) {
                            appendBubble("系统", r.minor_mode ? "已为该学生开启未成年模式" : "已关闭未成年模式", "系统");
                        } else {
                            cb.checked = !cb.checked;
                            appendBubble("系统", r.message || "设置失败", "系统");
                        }
                    } catch (err) {
                        cb.checked = !cb.checked;
                        appendBubble("系统", err.message || "设置失败", "系统");
                    }
                });
            }
        }
    }

    // =========================================================================
    // 聊天客户端系统：社交、好友列表、群聊、动画切换、消息收发与撤回（对标 QQ）
    // =========================================================================
    function fmtChatTime(ts) {
        if (!ts) return "";
        const d = new Date(ts * 1000);
        const now = new Date();
        const p = n => String(n).padStart(2, "0");
        if (d.toDateString() === now.toDateString()) {
            return `${p(d.getHours())}:${p(d.getMinutes())}`;
        }
        const yesterday = new Date(now);
        yesterday.setDate(now.getDate() - 1);
        if (d.toDateString() === yesterday.toDateString()) {
            return `昨天 ${p(d.getHours())}:${p(d.getMinutes())}`;
        }
        return `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    }

    // 聊天区时间分隔（QQ 风格）：按消息时间给出更完整的分隔文案
    // 今天 → "HH:MM"；昨天 → "昨天 HH:MM"；本周内 → "周三 HH:MM"；更早 → "YYYY年M月D日 HH:MM"
    function fmtTimeDivider(ts) {
        if (!ts) return "";
        const d = new Date(ts * 1000);
        const now = new Date();
        const p = n => String(n).padStart(2, "0");
        const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
        const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const dayDiff = Math.floor((startOfToday - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
        if (dayDiff <= 0) return hm;
        if (dayDiff === 1) return `昨天 ${hm}`;
        if (dayDiff < 7) return `星期${"日一二三四五六"[d.getDay()]} ${hm}`;
        if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
        return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
    }

    // QQ 规则：相邻消息间隔超过 5 分钟（或首条消息）时插入一个时间分隔
    const TIME_DIVIDER_GAP = 5 * 60;
    function makeTimeDivider(ts) {
        const el = document.createElement("div");
        el.className = "msg-time-divider";
        el.dataset.time = String(ts || "");
        el.textContent = fmtTimeDivider(ts);
        return el;
    }
    // 判断某条消息前是否需要时间分隔（对比上一条消息时间）
    function needTimeDivider(prevTs, ts) {
        if (ts == null) return false;
        if (prevTs == null) return true;
        return (ts - prevTs) > TIME_DIVIDER_GAP;
    }

    // ===== 新消息标识（QQ 风格"以下为新消息"分隔）=====
    // state.readPositions: {会话key: 上次已读时间戳}，来自 /gateway/conversations，跨刷新持久
    // state.newDividerTs: 本次打开会话时"已读位置"，用于渲染分隔条并计算是否还在下方
    function conversationKey(chatObj) {
        if (!chatObj) return "";
        return chatObj.chat_type === "bot" ? "bot" : (chatObj.target || chatObj.id || "");
    }
    function makeNewMsgDivider() {
        const el = document.createElement("div");
        el.className = "msg-new-divider";
        el.textContent = "以下为新消息";
        return el;
    }
    // 读取容器内最后一条消息行的时间（用于实时追加时判断是否要插分隔）
    function lastMsgTimeIn(container) {
        if (!container) return null;
        const rows = container.querySelectorAll("[data-msg-time]");
        if (!rows.length) return null;
        const v = rows[rows.length - 1].dataset.msgTime;
        return v ? parseFloat(v) : null;
    }

    // 读取容器内第一条消息行的时间（用于向上分页加载时连接处的时间分隔）
    function firstMsgTimeIn(container) {
        if (!container) return null;
        const row = container.querySelector("[data-msg-time]");
        return (row && row.dataset && row.dataset.msgTime) ? parseFloat(row.dataset.msgTime) : null;
    }

    // 把会话标记为已读到其最新消息时间：本地立即生效 + 告知服务端持久化
    function markConversationRead(chatObj) {
        if (!chatObj) return;
        const key = conversationKey(chatObj);
        if (!key) return;
        // 以会话最后消息时间为准（无消息则用当前时间）
        let ts = Date.now() / 1000;
        if (chatObj.last_message && chatObj.last_message.time) ts = chatObj.last_message.time;
        else {
            const t = lastMsgTimeIn($("chatView"));
            if (t != null) ts = t;
        }
        const cur = state.readPositions[key] || 0;
        if (ts <= cur) return;   // 已读位置只前进
        state.readPositions[key] = ts;
        state.unreadCounts[key] = 0;
        gatewayApi("/gateway/messages/read", "POST", {
            chat_type: chatObj.chat_type, target: chatObj.target, ts,
        }).catch(() => { /* 静默：本地已生效 */ });
    }

    // 微任务防抖调度联系人列表渲染：合并同时间段内的多次更新，消除频繁重排与中间态闪烁
    let _contactRenderPending = false;
    function scheduleContactListRender() {
        if (_contactRenderPending) return;
        _contactRenderPending = true;
        Promise.resolve().then(() => {
            _contactRenderPending = false;
            renderContactList();
        });
    }

    async function loadConversations() {
        if (!state.token) return;
        try {
            const r = await gatewayApi("/gateway/conversations", "GET");
            if (r.ok && Array.isArray(r.conversations)) {
                state.conversations = r.conversations;
                _saveLocalCache("cache_conversations", state.conversations);
                if (r.read_positions) {
                    state.readPositions = r.read_positions;
                    _saveLocalCache("cache_read_positions", state.readPositions);
                }
                scheduleContactListRender();
            }
        } catch (e) { /* 接口异常时保留现有缓存，绝不将列表重置为空 */ }
    }

    // 带一次重试的接口调用：碰上限流（429）或瞬时网络抖动时退避重试，
    // 避免群聊/好友列表因一次失败被静默丢弃而不显示。
    async function gatewayApiRetry(path, method = "GET", body = null, retries = 2) {
        let lastErr = null;
        for (let i = 0; i <= retries; i++) {
            try {
                return await gatewayApi(path, method, body);
            } catch (e) {
                lastErr = e;
                const msg = String((e && e.message) || "");
                const transient = msg.includes("频繁") || msg.includes("429") || msg.includes("加载失败") || msg.includes("网络");
                if (i < retries && transient) { await gentleDelay(400 * (i + 1)); continue; }
                break;
            }
        }
        throw lastErr;
    }

    async function loadFriends() {
        if (!state.token) return;
        try {
            const r = await gatewayApiRetry("/gateway/friends/list", "GET");
            if (r.ok && Array.isArray(r.friends)) {
                state.friends = r.friends;
                _saveLocalCache("cache_friends", state.friends);
                scheduleContactListRender();
            }
        } catch (e) { /* 保留现有好友列表，绝不重置为空 */ }
    }

    async function loadGroups() {
        if (!state.token) return;
        try {
            const r = await gatewayApiRetry("/gateway/groups/list", "GET");
            if (r.ok && Array.isArray(r.groups)) {
                state.groups = r.groups;
                _saveLocalCache("cache_groups", state.groups);
                scheduleContactListRender();
            }
        } catch (e) { /* 保留现有群聊列表，绝不重置为空 */ }
    }

    async function loadFriendRequests() {
        if (!state.token) return;
        try {
            const r = await gatewayApi("/gateway/friends/requests", "GET");
            if (r.ok) {
                const count = (r.received || []).length;
                state.pendingRequestsCount = count;
                const dot = $("reqBadgeDot");
                if (dot) dot.style.display = count > 0 ? "block" : "none";
            }
        } catch (e) { /* silent */ }
    }

    // 串行小步等待：把并发请求摊开，避免撞上服务端 IP 限流（10 次/秒）
    function gentleDelay(ms = 260) {
        return new Promise(res => setTimeout(res, ms));
    }

    // 并发安全拉取所有联系人关系（首屏/恢复时调用）
    // 注意：服务端 IP 限流为 10 次/秒，若四个接口（各自还可能并发子请求）同时打出去，
    // 会有一部分被 429 拒绝并被上层 catch 静默丢弃，导致群聊/好友列表随机不出现。
    // 因此这里改为串行 + 间隔，牺牲几十毫秒换取列表稳定完整。
    async function refreshAllContacts() {
        if (!state.token) return;
        await loadConversations();
        await gentleDelay();
        await loadFriends();
        await gentleDelay();
        await loadGroups();
        await gentleDelay();
        await loadFriendRequests();
        scheduleContactListRender();
    }

    function renderContactList() {
        const listEl = $("contactList");
        if (!listEl) return;
        const filter = ($("contactSearchInput") ? $("contactSearchInput").value : "").trim().toLowerCase();
        const countEl = $("contactCount");

        // 合并为一栏：辉夜(官方AI) + 全部好友(含私聊) + 全部群聊，按最近消息时间倒序
        // 家长/老师没有自己的记忆体：辉夜仅在其已绑定学生时可用，
        // 且对话作用域为其绑定的学生记忆体（见 enterChat/_resolve_memory_scope）。
        const botDisabled = state.role !== "student" && !(state.linkedStudents && state.linkedStudents.length);
        // 头像/最后消息以 /gateway/conversations 为准（/friends、/groups 接口不带 last_message）
        const convByTarget = {};
        (state.conversations || []).forEach(c => { convByTarget[String(c.target || c.id)] = c; });

        const botItem = {
            id: "bot",
            chat_type: "bot",
            target: "bot",
            name: state.botName || "辉夜",
            role: "ai",
            is_online: true,
            pinned: true,
            muted: false,
            disabled: botDisabled,
        };
        const friends = (state.friends || []).map(f => {
            const conv = convByTarget[f.username];
            return {
                id: f.username,
                chat_type: "user",
                target: f.username,
                name: f.alias || f.username,
                role: f.role,
                is_online: f.is_online,
                pinned: f.pinned,
                muted: f.muted,
                // 修复"列表不显示上一条消息"：好友/群接口不含 last_message，
                // 需从会话聚合接口按 target 合并
                last_message: (conv && conv.last_message) || f.last_message,
                created_at: f.created_at,
            };
        });
        const groups = (state.groups || []).map(g => {
            const conv = convByTarget[g.id];
            return {
                id: g.id,
                chat_type: "group",
                target: g.id,
                name: g.name,
                role: "group",
                is_online: false,
                pinned: g.pinned,
                muted: g.muted,
                member_count: g.member_count,
                last_message: (conv && conv.last_message) || g.last_message,
                created_at: g.created_at,
            };
        });
        // 若会话接口带回了辉夜最新消息，合并到辉夜项
        const botConv = (state.conversations || []).find(c => c.chat_type === "bot");
        if (botConv && botConv.last_message) botItem.last_message = botConv.last_message;

        let items = [botItem, ...friends, ...groups];

        if (filter) {
            items = items.filter(it => {
                const name = (it.name || it.username || "").toLowerCase();
                const last = (it.last_message && it.last_message.content ? it.last_message.content : "").toLowerCase();
                return name.includes(filter) || last.includes(filter);
            });
        }

        // 置顶优先，其次按最近消息时间倒序（无时间则靠后）
        const ts = it => (it.last_message && it.last_message.time) || it.created_at || 0;
        items.sort((a, b) => {
            if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
            return ts(b) - ts(a);
        });

        if (countEl) {
            const total = 1 + (state.friends || []).length + (state.groups || []).length;
            countEl.textContent = `${total} 个会话`;
        }

        if (!items.length) {
            listEl.innerHTML = `<div class="empty-hint">${filter ? "未找到匹配项" : "暂无内容"}</div>`;
            return;
        }
        const prevEmpty = listEl.querySelector(".empty-hint");
        if (prevEmpty) prevEmpty.remove();

        // ===== 关键：按 target 做 keyed 复用，避免每 2 秒轮询重建 <img> 导致头像频闪 =====
        // 仅复用结构、更新变化字段；头像 <img> 若 src 未变则不重建。
        const rowKey = it => String(it.target || it.id || it.username);
        const existing = new Map();
        [...listEl.children].forEach(ch => {
            if (ch.dataset && ch.dataset.key) existing.set(ch.dataset.key, ch);
        });
        const reused = new Set();
        let placed = 0;

        items.forEach(item => {
            const key = rowKey(item);
            const isBot = item.chat_type === "bot" || item.role === "ai";
            const isGroup = item.chat_type === "group" || item.role === "group";
            const avatarClass = isBot ? "bot" : isGroup ? "group" : "";
            const avatarText = isBot ? "辉" : isGroup ? "群" : (item.name || item.username || "?").slice(0, 1).toUpperCase();

            const targetKey = key;
            // 未读数：以服务端持久化的 unread 为准（跨刷新/重登保持）；
            // 本次会话内新到消息由 onIncomingChatMessage 本地即时累加。
            // 两者取较大值（不做加法，避免同一条消息被重复计入）。
            let unreadCount = 0;
            if (state.currentChat && state.currentChat.target === item.target) {
                unreadCount = 0;   // 正在查看的会话不显示未读
            } else {
                const srv = (convByTarget[key] && convByTarget[key].unread) || 0;
                unreadCount = Math.max(state.unreadCounts[targetKey] || 0, srv);
            }

            let previewText = "暂无消息";
            let timeText = "";
            if (item.disabled) {
                previewText = "请先在账号页绑定学生";
            } else if (item.last_message) {
                previewText = item.last_message.content || "[图片]";
                if (item.last_message.time) timeText = fmtChatTime(item.last_message.time);
            } else if (item.created_at) {
                timeText = fmtChatTime(item.created_at);
            }

            let row = existing.get(key);
            if (!row) {
                row = document.createElement("div");
                row.dataset.key = key;
                row.innerHTML = `
                    <div class="contact-avatar-wrap">
                        <div class="contact-avatar"></div>
                        <div class="status-dot"></div>
                        <div class="unread-badge" style="display:none"></div>
                    </div>
                    <div class="contact-info">
                        <div class="contact-row-top">
                            <span class="contact-name"></span>
                            <span class="contact-time"></span>
                        </div>
                        <div class="contact-row-bottom">
                            <span class="contact-preview"></span>
                            <div class="contact-icons"></div>
                        </div>
                    </div>
                `;
                // 事件只绑定一次（闭包引用 row.dataset.item 取最新数据）
                row.addEventListener("click", () => {
                    const it = JSON.parse(row.dataset.item || "null");
                    if (!it) return;
                    if (it.disabled) { alert("请先在账号页绑定学生，再与该学生的记忆体对话。"); return; }
                    if (it.chat_type === "bot") {
                        openChat(it);
                    } else if (it.chat_type === "group") {
                        openChat({ chat_type: "group", target: it.target, name: it.name,
                                   member_count: it.member_count, pinned: it.pinned, muted: it.muted });
                    } else {
                        openChat({ chat_type: "user", target: it.target, name: it.name, role: it.role,
                                   isOnline: it.is_online, is_online: it.is_online,
                                   pinned: it.pinned, muted: it.muted });
                    }
                });
                row.addEventListener("contextmenu", (e) => {
                    const it = JSON.parse(row.dataset.item || "null");
                    if (it) showContactContextMenu(e, it);
                });
            }
            reused.add(key);
            row.dataset.item = JSON.stringify(item);

            // 更新类名（未读只用头像角标表示，不改变条目外观，避免像"被选中"）
            row.className = "contact-item"
                + (item.pinned ? " pinned" : "")
                + (item.disabled ? " disabled" : "")
                + (state.currentChat && state.currentChat.target === item.target ? " active" : "");

            // 更新文本字段
            row.querySelector(".contact-name").textContent = item.name || item.username || "";
            row.querySelector(".contact-time").textContent = timeText;
            row.querySelector(".contact-preview").textContent = previewText;
            row.querySelector(".contact-icons").innerHTML =
                (item.pinned ? "<span>📌</span>" : "") + (item.muted ? "<span>🔕</span>" : "");

            const badge = row.querySelector(".unread-badge");
            if (unreadCount > 0) { badge.style.display = ""; badge.textContent = unreadCount > 99 ? "99+" : String(unreadCount); }
            else badge.style.display = "none";

            const dot = row.querySelector(".status-dot");
            if (isGroup) { dot.style.display = "none"; }
            else { dot.style.display = ""; dot.classList.toggle("online", !!item.is_online); }

            // 头像：仅在 src 变化时重建 <img>（避免轮询导致频闪）。
            // 占位字母用 .avatar-initial 绝对定位，与 <img> 不并排，加载前后无布局抽动。
            const avatarEl = row.querySelector(".contact-avatar");
            const beforeCls = avatarEl.dataset.cls || "";
            if (beforeCls !== avatarClass) { avatarEl.className = "contact-avatar " + avatarClass; avatarEl.dataset.cls = avatarClass; }
            const wantSrc = isBot
                ? (state.botAvatar || state.botAvatarDefault || "")
                : (isGroup ? "" : userAvatarUrl(item.target || item.username));
            const ensureInitial = () => {
                let ini = avatarEl.querySelector(".avatar-initial");
                if (!ini) {
                    ini = document.createElement("span");
                    ini.className = "avatar-initial";
                    avatarEl.appendChild(ini);
                }
                if (ini.textContent !== avatarText) ini.textContent = avatarText;
                return ini;
            };
            if (!wantSrc) {
                // 无图（群聊 / 无头像用户）：显示字母占位，清理残留 <img>
                if (avatarEl.dataset.src !== "\u0000") { avatarEl.dataset.src = "\u0000"; avatarEl.innerHTML = ""; }
                ensureInitial();
            } else if (avatarEl.dataset.src !== wantSrc) {
                avatarEl.dataset.src = wantSrc;
                avatarEl.innerHTML = "";
                const ini = ensureInitial();
                const im = document.createElement("img");
                im.alt = "";
                im.onload = () => { if (ini.parentNode) ini.remove(); };
                im.onerror = () => { im.remove(); ensureInitial(); };
                im.src = wantSrc;
                avatarEl.appendChild(im);
            }

            // 按目标顺序插入（DOM 复用：已存在则移动，不重建）
            const refNode = listEl.children[placed];
            if (refNode !== row) listEl.insertBefore(row, refNode || null);
            placed++;
        });

        // 移除已不存在的项
        [...listEl.children].forEach(ch => {
            if (!reused.has(ch.dataset.key)) ch.remove();
        });
    }

    // ========== 打开聊天与返回动画（好友列表左移，聊天向右展开；返回向左变小，列表归位） ==========
    function openChat(chatObj) {
        if (!chatObj) return;
        state.currentChat = chatObj;
        clearQuote();          // 切换会话：清除上一会话遗留的引用
        closeMentionPicker();  // 切换会话：关闭 @ 选人面板
        // 表情包仅对好友/群聊开放；与辉夜的对话只能用 emoji，直接隐藏入口
        applyStickerAvailability();
        // 切换会话：关闭抽奖面板，按新会话刷新入口与角标
        closeLotteryModal();
        lotteryState.groupId = chatObj.chat_type === "group" ? chatObj.target : "";
        lotteryState.room = null;
        lotteryState.members = [];
        lotteryState.inviteOpen = false;
        lotteryState.error = "";
        setLotteryDot(false);
        applyLotteryAvailability();
        if (chatObj.chat_type === "group") refreshLotteryBadge(chatObj);
        const targetKey = chatObj.target || chatObj.id || chatObj.username;
        state.unreadCounts[targetKey] = 0;
        // 记录本次打开前的已读位置：晚于它的消息将被标为"以下为新消息"
        const key = conversationKey(chatObj);
        state.newDividerTs = state.readPositions[key] || null;

        const chatPage = $("chat-page");
        const chatCard = $("chat-card");

        // 1. 聊天页容器激活 chat-active（联系人卡平滑收窄，保持交互完全可用）
        if (chatPage) chatPage.classList.add("chat-active");

        // 2. 聊天卡展开：仅加 open-right 类，宽度/位移/透明度由 CSS transition 平滑过渡
        if (chatCard) {
            chatCard.style.display = "";      // 清除内联 display，交由 CSS 控制
            void chatCard.offsetWidth;        // 触发 reflow，确保从收起态开始过渡
            chatCard.classList.add("open-right");
        }

        // 3. 更新顶栏信息
        updateChatHeader(chatObj);

        // 确保滚动容器监听器已就绪
        ensureChatScrollBound();

        // 4. 加载该会话消息
        loadChatMessages(chatObj);

        renderContactList();
    }

    function closeChat() {
        const chatPage = $("chat-page");
        const chatCard = $("chat-card");

        // 1. 聊天卡收起：移除 open-right 即可，宽度/位移/透明度由 CSS transition 反向平滑收拢
        if (chatCard) {
            chatCard.classList.remove("open-right");
        }

        // 2. 移除 chat-active，联系人卡平滑变宽归位（与聊天卡收拢同步，同一过渡时长）
        if (chatPage) chatPage.classList.remove("chat-active");

        state.currentChat = null;
        const sp = $("stickerPanel"); if (sp) sp.style.display = "none";
        applyLotteryAvailability();
        loadConversations();
    }

    $("btnChatBack").addEventListener("click", closeChat);

    function updateChatHeader(chatObj) {
        const titleEl = $("chatTitle");
        const aiFlag = $("chatAiFlag");
        const statusEl = $("chatStatus");
        const muteBadge = $("chatMutedBadge");

        if (titleEl) {
            titleEl.textContent = chatObj.name || (chatObj.chat_type === "bot" ? state.botName : chatObj.target);
        }
        if (aiFlag) {
            aiFlag.style.display = (chatObj.chat_type === "bot") ? "inline-block" : "none";
        }
        if (muteBadge) {
            muteBadge.style.display = chatObj.muted ? "inline-block" : "none";
        }
        if (statusEl) {
            if (chatObj.chat_type === "bot") {
                statusEl.textContent = "记忆认知对话";
            } else if (chatObj.chat_type === "group") {
                statusEl.textContent = `${chatObj.member_count || 0} 位成员`;
            } else {
                const online = (chatObj.isOnline !== undefined) ? chatObj.isOnline : chatObj.is_online;
                statusEl.textContent = online ? "在线" : "离线";
            }
        }
    }

    async function loadChatMessages(chatObj) {
        const view = $("chatView");
        if (!view) return;
        $("welcome").style.display = "none";
        // 切换会话时清空思考提示（避免上一个会话残留的转圈动画带到新会话）
        hideThinking(true);

        if (chatObj.chat_type === "bot") {
            $("welcomeTitle").textContent = `Nascence${state.botName}`;
            $("welcomeDesc").textContent = "记忆认知对话 · 随时开始";
            loadHistory();
            return;
        }

        // 单聊与群聊会话加载：带请求票据防竞态乱序覆盖，原子替换 DOM，失败保留已有内容不上屏白板
        const thisTicket = ++currentChatTicket;
        const curKey = conversationKey(chatObj);

        try {
            const r = await gatewayApi(`/gateway/messages?chat_type=${encodeURIComponent(chatObj.chat_type)}&target=${encodeURIComponent(chatObj.target)}`, "GET");
            // 防竞态：若在请求期间用户切换到了其他会话，直接丢弃过时数据
            if (thisTicket !== currentChatTicket || !state.currentChat || conversationKey(state.currentChat) !== curKey) {
                return;
            }
            const msgs = (r && r.messages) || [];
            if (!msgs.length) {
                view.innerHTML = "";
                $("welcomeTitle").textContent = chatObj.name || "聊天室";
                $("welcomeDesc").textContent = "开始你们的第一次对话吧";
                $("welcome").style.display = "flex";
                historyHasMore = false;
                historyOldestTime = null;
                historyLoadingMore = false;
            } else {
                $("welcome").style.display = "none";
                const frag = document.createDocumentFragment();
                let prevTime = null;
                const readTs = state.newDividerTs;
                let newDividerPlaced = false;
                msgs.forEach(m => {
                    if (needTimeDivider(prevTime, m.time)) {
                        frag.appendChild(makeTimeDivider(m.time));
                        prevTime = m.time;
                    }
                    if (!newDividerPlaced && readTs != null && m.time != null && m.time > readTs) {
                        frag.appendChild(makeNewMsgDivider());
                        newDividerPlaced = true;
                    }
                    const row = buildBubbleRow(m);
                    if (row) frag.appendChild(row);
                    if (m.time != null) prevTime = m.time;
                });

                // 原子替换：组装好后再挂载，避免发请求到返回期间整屏空白
                view.innerHTML = "";
                view.appendChild(frag);

                state.newDividerTs = null;
                historyOldestTime = msgs[0].time;
                historyHasMore = !!r.has_more;
                historyLoadingMore = false;

                scrollChatToBottom();
                ensureChatScrollBound();
            }
        } catch (e) {
            // 失败保护：有历史则保留，无历史则给出轻量可重试提示
            if (thisTicket === currentChatTicket && !view.children.length) {
                view.innerHTML = `<div class="empty-hint" style="cursor:pointer;" onclick="loadChatMessages(state.currentChat)">消息加载失败（${escapeHtml(e.message || "网络异常")}），点击重试</div>`;
            }
        }
        markConversationRead(chatObj);
    }

    // ========== 消息右键上下文菜单（对标 QQ 撤回与复制） ==========
    let currentCtxMsg = null;

    function showMsgContextMenu(e, m, row) {
        e.preventDefault();
        e.stopPropagation();
        currentCtxMsg = { msg: m, row: row };
        const menu = $("msg-context-menu");
        if (!menu) return;

        const isSelf = m.sender === state.username || m.sender === state.displayName;
        const isSystem = (m.sender === "系统" || m.source === "错误" || m.source === "系统" || m.recalled);

        const recallBtn = $("ctxMenuRecall");
        if (recallBtn) {
            const canRecall = isSelf && !m.recalled && m.content !== "此消息已撤回" && state.currentChat && state.currentChat.chat_type !== "bot";
            recallBtn.style.display = canRecall ? "block" : "none";
        }
        const replyBtn = $("ctxMenuReply");
        if (replyBtn) replyBtn.style.display = isSystem ? "none" : "block";
        const mentionBtn = $("ctxMenuMention");
        if (mentionBtn) {
            mentionBtn.style.display = (!isSystem && m.sender) ? "block" : "none";
            mentionBtn.textContent = `@${m.sender || ""}`;
        }
        menu.style.display = "block";
        menu.style.left = Math.min(e.clientX, window.innerWidth - 130) + "px";
        menu.style.top = Math.min(e.clientY, window.innerHeight - 90) + "px";
    }

    $("ctxMenuReply").addEventListener("click", () => {
        if (currentCtxMsg && currentCtxMsg.msg) {
            setQuote(currentCtxMsg.msg, currentCtxMsg.row);
        }
        $("msg-context-menu").style.display = "none";
    });

    $("ctxMenuMention").addEventListener("click", () => {
        if (currentCtxMsg && currentCtxMsg.msg && currentCtxMsg.msg.sender) {
            insertMention(currentCtxMsg.msg.sender);
        }
        $("msg-context-menu").style.display = "none";
    });

    $("ctxMenuCopy").addEventListener("click", () => {
        if (currentCtxMsg && currentCtxMsg.msg) {
            copyText(currentCtxMsg.msg.content || "");
        }
        $("msg-context-menu").style.display = "none";
    });

    $("ctxMenuRecall").addEventListener("click", async () => {
        if (currentCtxMsg && currentCtxMsg.msg) {
            await doRecallMessage(currentCtxMsg.msg);
        }
        $("msg-context-menu").style.display = "none";
    });

    async function doRecallMessage(msgObj) {
        if (!msgObj || !state.currentChat) return;
        // 若该消息仍在发送中（临时 id 尚未被服务端确认），等待发送完成拿到真实 id 再撤回，
        // 否则会把临时 id 发给后端导致"消息不存在"（发送后立即撤回的场景）。
        if (window.__pendingSendById && window.__pendingSendById[msgObj.id]) {
            try { await window.__pendingSendById[msgObj.id]; } catch (e) { /* ignore */ }
        }
        const realId = (msgObj.id && String(msgObj.id).startsWith("msg_") && window.__confirmedMsgId && window.__confirmedMsgId[msgObj.id])
            ? window.__confirmedMsgId[msgObj.id] : msgObj.id;
        // 对标 QQ 规则：发送超过2分钟的消息无法撤回
        const nowSec = Date.now() / 1000;
        if (msgObj.time && (nowSec - msgObj.time > 120)) {
            alert("发送已超过2分钟，不能撤回。");
            return;
        }
        try {
            const r = await gatewayApi("/gateway/messages/recall", "POST", {
                chat_type: state.currentChat.chat_type,
                target: state.currentChat.target,
                msg_id: realId,
            });
            if (r.ok) {
                markMessageRecalled(realId, state.username);
                loadConversations();
            } else {
                alert(r.message || "撤回失败");
            }
        } catch (e) {
            alert(e.message || "撤回失败");
        }
    }

    function markMessageRecalled(msgId, sender) {
        const row = document.querySelector(`[data-msg-id="${msgId}"]`);
        if (row) {
            const isSelf = sender === state.username;
            row.className = "msg-row system";
            row.innerHTML = `<div class="msg-recalled">${isSelf ? "你" : escapeHtml(sender || "对方")} 撤回了一条消息</div>`;
        }
    }

    // ========== 联系人右键菜单（置顶、免打扰、删除/退出） ==========
    let currentContactItem = null;

    function showContactContextMenu(e, item) {
        e.preventDefault();
        e.stopPropagation();
        currentContactItem = item;
        const menu = $("contact-context-menu");
        if (!menu) return;

        const pinBtn = $("ctxContactPin");
        const muteBtn = $("ctxContactMute");
        const delBtn = $("ctxContactDelete");

        if (pinBtn) pinBtn.textContent = item.pinned ? "取消置顶" : "置顶该聊天";
        if (muteBtn) muteBtn.textContent = item.muted ? "关闭免打扰" : "开启消息免打扰";
        if (delBtn) {
            if (item.chat_type === "bot" || item.role === "ai") {
                delBtn.style.display = "none";
            } else if (item.chat_type === "group" || item.role === "group") {
                delBtn.style.display = "block";
                delBtn.textContent = "退出群聊";
            } else {
                delBtn.style.display = "block";
                delBtn.textContent = "删除好友";
            }
        }

        menu.style.display = "block";
        menu.style.left = Math.min(e.clientX, window.innerWidth - 130) + "px";
        menu.style.top = Math.min(e.clientY, window.innerHeight - 130) + "px";
    }

    $("ctxContactPin").addEventListener("click", async () => {
        if (!currentContactItem) return;
        const it = currentContactItem;
        $("contact-context-menu").style.display = "none";
        const newPinned = !it.pinned;
        try {
            if (it.chat_type === "bot" || it.role === "ai") {
                await gatewayApi("/gateway/bot/settings", "POST", { pinned: newPinned });
            } else if (it.chat_type === "group" || it.role === "group") {
                await gatewayApi("/gateway/groups/settings", "POST", { group_id: it.target || it.id, pinned: newPinned });
            } else {
                await gatewayApi("/gateway/friends/settings", "POST", { target_username: it.target || it.username, pinned: newPinned });
            }
            loadConversations();
            loadFriends();
            loadGroups();
        } catch (e) { alert(e.message || "操作失败"); }
    });

    $("ctxContactMute").addEventListener("click", async () => {
        if (!currentContactItem) return;
        const it = currentContactItem;
        $("contact-context-menu").style.display = "none";
        const newMuted = !it.muted;
        try {
            if (it.chat_type === "bot" || it.role === "ai") {
                await gatewayApi("/gateway/bot/settings", "POST", { muted: newMuted });
            } else if (it.chat_type === "group" || it.role === "group") {
                await gatewayApi("/gateway/groups/settings", "POST", { group_id: it.target || it.id, muted: newMuted });
            } else {
                await gatewayApi("/gateway/friends/settings", "POST", { target_username: it.target || it.username, muted: newMuted });
            }
            loadConversations();
            loadFriends();
            loadGroups();
        } catch (e) { alert(e.message || "操作失败"); }
    });

    $("ctxContactDelete").addEventListener("click", async () => {
        if (!currentContactItem) return;
        const it = currentContactItem;
        $("contact-context-menu").style.display = "none";
        if (it.chat_type === "group" || it.role === "group") {
            if (!confirm(`确认退出群聊「${it.name}」？`)) return;
            try {
                await gatewayApi("/gateway/groups/leave", "POST", { group_id: it.target || it.id });
                if (state.currentChat && state.currentChat.target === (it.target || it.id)) closeChat();
                loadGroups();
                loadConversations();
            } catch (e) { alert(e.message || "退出失败"); }
        } else {
            const u = it.target || it.username;
            if (!confirm(`确认删除好友「${u}」？`)) return;
            try {
                await gatewayApi("/gateway/friends/delete", "POST", { target_username: u });
                if (state.currentChat && state.currentChat.target === u) closeChat();
                loadFriends();
                loadConversations();
            } catch (e) { alert(e.message || "删除失败"); }
        }
    });

    document.addEventListener("click", () => {
        const m1 = $("msg-context-menu"); if (m1) m1.style.display = "none";
        const m2 = $("contact-context-menu"); if (m2) m2.style.display = "none";
        const m3 = $("emojiPicker"); if (m3) m3.style.display = "none";
        const m4 = $("mentionPicker"); if (m4) m4.style.display = "none";
        const m5 = $("stickerPanel"); if (m5) m5.style.display = "none";
    });

    // 引用预览条：取消引用按钮
    const _btnCloseQuote = $("btnCloseQuote");
    if (_btnCloseQuote) {
        _btnCloseQuote.addEventListener("click", (e) => {
            e.stopPropagation();
            clearQuote();
        });
    }

    // ========== Emoji 表情选择器 ==========
    // 仅保留常用表情（单一码点、跨平台/服务器字体兼容性好）。
    // 避免生僻表情与带变体选择符(VS16)的组合，防止服务器上显示为方框。
    const EMOJIS = [
        "😀","😄","😁","😆","😅","😂","🤣","😊","🙂","😉","😍","🥰",
        "😘","😋","😜","🤔","🤗","😎","🥳","😏","😢","😭","😩","😤",
        "👍","👎","👏","🙏","👌","💪","🎉","🔥","❤","💔","✨","💯",
        "😴","😪","😳","😱","😡","🤝","🙌","👋","😇","🥺","😞","😔"
    ];

    function initEmojiPicker() {
        const panel = $("emojiPicker");
        if (!panel) return;
        panel.innerHTML = "";
        EMOJIS.forEach(emo => {
            const sp = document.createElement("span");
            sp.className = "emoji-item";
            sp.textContent = emo;
            sp.addEventListener("click", (e) => {
                e.stopPropagation();
                const ta = $("chatInput");
                if (ta) {
                    ta.value += emo;
                    ta.focus();
                }
                panel.style.display = "none";
            });
            panel.appendChild(sp);
        });

        const btn = $("btnEmojiToggle");
        if (btn) {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const willShow = panel.style.display === "none";
                const sp = $("stickerPanel"); if (sp) sp.style.display = "none";   // 与表情包面板互斥
                panel.style.display = willShow ? "flex" : "none";
            });
        }
    }

    // ========== 表情包（贴纸）功能 ==========
    // 约定：服务端 stickers/<包名>/ 下，logo.* 为该包封面（展示在包选择区），
    // 其余图片为包内表情包（名字取文件名去扩展名）。用户需先下载该包到本地缓存
    // （IndexedDB）后才能发送；辉夜（bot）会话不提供此功能。
    const STICKER_DB_NAME = "nhy_stickers";
    // v2：贴纸字节改存 ArrayBuffer（见下方注释），旧版缓存直接重建
    const STICKER_DB_VERSION = 2;
    const STICKER_STORE = "packs";
    const STICKER_URL_CACHE = "__stickerUrlCache";   // 包名 -> { 文件名 -> blobURL }

    function stickerDbOpen() {
        return new Promise((resolve) => {
            try {
                if (!window.indexedDB) return resolve(null);
                const req = indexedDB.open(STICKER_DB_NAME, STICKER_DB_VERSION);
                req.onupgradeneeded = () => {
                    const db = req.result;
                    // 版本升级：旧版把 Blob 直接存入 IndexedDB，而 iOS Safari 读回后
                    // 可能得到一个体积为 0 / 无法解码的 Blob，表现为"表情包全是空白"。
                    // 这里直接重建存储，让缓存以新的 ArrayBuffer 形式重新落地。
                    if (db.objectStoreNames.contains(STICKER_STORE)) {
                        db.deleteObjectStore(STICKER_STORE);
                    }
                    db.createObjectStore(STICKER_STORE, { keyPath: "id" });
                };
                req.onsuccess = () => resolve(req.result);
                req.onerror = () => resolve(null);
            } catch (e) { resolve(null); }
        });
    }

    async function stickerDbGetAll() {
        const db = await stickerDbOpen();
        if (!db) return [];
        return new Promise((resolve) => {
            try {
                const tx = db.transaction(STICKER_STORE, "readonly");
                const req = tx.objectStore(STICKER_STORE).getAll();
                req.onsuccess = () => resolve(req.result || []);
                req.onerror = () => resolve([]);
            } catch (e) { resolve([]); }
        });
    }

    async function stickerDbPut(record) {
        const db = await stickerDbOpen();
        if (!db) return false;
        return new Promise((resolve) => {
            try {
                const tx = db.transaction(STICKER_STORE, "readwrite");
                tx.objectStore(STICKER_STORE).put(record);
                tx.oncomplete = () => resolve(true);
                tx.onerror = () => resolve(false);
            } catch (e) { resolve(false); }
        });
    }

    // 已下载的包：{ id: packName, files: { 文件名: Blob }, count, savedAt }
    let stickerLibrary = {};
    let stickerManifest = [];      // 服务端可用的包列表
    let stickerActivePack = "";    // 当前选中的包名
    let stickerPackFiles = {};     // 当前包的文件清单（文件名 -> 表情包名）
    let stickerRandomSeries = {};  // 当前包的随机系列：{ 基名: [文件名...] }（如 骰子 -> 骰子_1..6）
    let stickerPackDisplayName = ""; // 当前包显示名
    // 默认表情包组（服务端目录名）：免手动下载，首次打开面板时自动缓存，保证"开箱即用"
    const DEFAULT_STICKER_PACK = "默认表情包";

    function stickerPackUrl(pack, filename) {
        // 防御：文件名缺失时返回空串，绝不拼出 /stickers/<包>/undefined 这类无效请求
        // （渲染层若拿到 undefined 拼进 src，浏览器会发 404 请求且格子显示成碎图标）
        if (!pack || !filename || filename === "undefined") return "";
        return `/stickers/${encodeURIComponent(pack)}/${encodeURIComponent(filename)}`;
    }

    // 从表情包 URL 反解出表情包名（取文件名去扩展名），供历史上没有存名字的消息兜底
    function stickerNameFromUrl(imageUrl) {
        try {
            const m = /^\/stickers\/([^/]+)\/(.+)$/.exec(imageUrl || "");
            if (!m) return "";
            return decodeURIComponent(m[2]).replace(/\.[^.]+$/, "");
        } catch (e) { return ""; }
    }

    // 生成引用卡片里图片/表情包的可读文案（统一去重，避免出现"[表情包] [表情包] 名字"）。
    // 服务端补全的 content 本身已带前缀（如"[表情包] 长条贴纸"），因此需要先剥掉再拼。
    function quoteMediaLabel(q) {
        const isSticker = (q.mtype === "sticker" || /^\/stickers\//.test(q.image || ""));
        const label = isSticker ? "[表情包]" : "[图片]";
        let name = (q.sticker_name || "").trim();
        let content = (q.content || "").trim();
        // 剥掉已有前缀，还原出纯名字
        content = content.replace(/^\[表情包\]\s*/, "").replace(/^\[图片\]\s*/, "").trim();
        if (!name && content) name = content;
        if (!name && isSticker) name = stickerNameFromUrl(q.image);
        if (isSticker && name) return `${label} ${name}`;
        return name ? `${label} ${name}` : label;
    }

    // 把消息里的表情包 URL 换成本地缓存的 Blob URL（优先本地缓存，未缓存则回退远端 URL）
    function localStickerSrc(imageUrl) {
        try {
            const m = /^\/stickers\/([^/]+)\/(.+)$/.exec(imageUrl || "");
            if (!m) return imageUrl;
            const pack = decodeURIComponent(m[1]);
            const file = decodeURIComponent(m[2]);
            const cache = window[STICKER_URL_CACHE] || {};
            const url = cache[pack] && cache[pack][file];
            return url || imageUrl;
        } catch (e) { return imageUrl; }
    }

    // 给表情图挂"本地缓存失败自动回退远端"的保险。
    // 场景：某些浏览器（尤其 iOS Safari）读回的本地缓存 blob 可能无法解码，
    // 此时若不回退，用户看到的就是一片空白；回退到服务端地址即可正常显示。
    function bindStickerFallback(img, remoteUrl) {
        if (!img || !remoteUrl) return;
        img.addEventListener("error", () => {
            const cur = img.getAttribute("src") || "";
            // 已经是远端地址却仍失败 → 不再重试，避免死循环
            if (cur === remoteUrl || cur.startsWith("data:")) return;
            img.src = remoteUrl;
        });
    }

    // 为已下载的包建立 Blob URL 映射（同一文件只建一次，避免内存泄漏）
    function buildStickerUrlCache() {
        window[STICKER_URL_CACHE] = window[STICKER_URL_CACHE] || {};
        Object.keys(stickerLibrary).forEach(pack => {
            const files = stickerLibrary[pack].files || {};
            const map = window[STICKER_URL_CACHE][pack] || (window[STICKER_URL_CACHE][pack] = {});
            Object.keys(files).forEach(fn => {
                if (map[fn]) return;
                if (files[fn] === null || files[fn] === undefined) return;
                try {
                    // v2 存的是 {buf: ArrayBuffer, type: MIME}；兼容早期版本的裸 Blob
                    let blob;
                    if (files[fn] && files[fn].buf) {
                        blob = new Blob([files[fn].buf], { type: files[fn].type || "image/png" });
                    } else if (files[fn] instanceof Blob) {
                        // 旧缓存：若读回为空（iOS 已知问题）则跳过，交由远端地址兜底
                        if (!files[fn].size) return;
                        blob = files[fn];
                    } else {
                        return;
                    }
                    if (!blob.size) return;
                    map[fn] = URL.createObjectURL(blob);
                } catch (e) { /* ignore：失败时由 localStickerSrc 回退远端地址 */ }
            });
        });
    }

    async function loadStickerLibrary() {
        const rows = await stickerDbGetAll();
        stickerLibrary = {};
        rows.forEach(r => { if (r && r.id) stickerLibrary[r.id] = r; });
        buildStickerUrlCache();
    }

    function isStickerAvailable() {
        // 辉夜（bot）会话不允许使用表情包功能
        return !!(state.currentChat && state.currentChat.chat_type !== "bot");
    }

    // 按当前会话类型显示/隐藏表情包入口（辉夜会话隐藏，并收起已打开的面板）
    function applyStickerAvailability() {
        const btn = $("btnStickerToggle");
        const panel = $("stickerPanel");
        const ok = isStickerAvailable();
        if (btn) btn.style.display = ok ? "" : "none";
        if (!ok && panel) panel.style.display = "none";
    }

    async function refreshStickerManifest() {
        try {
            const r = await gatewayApi("/gateway/stickers/manifest", "GET");
            if (r && r.ok && Array.isArray(r.packs)) stickerManifest = r.packs;
        } catch (e) { /* silent */ }
    }

    async function loadStickerPackFiles(pack) {
        try {
            const r = await gatewayApi(`/gateway/stickers/manifest?pack=${encodeURIComponent(pack)}`, "GET");
            if (r && r.ok && Array.isArray(r.stickers)) {
                const map = {};
                r.stickers.forEach(s => { if (s && s.file) map[s.file] = s.name || s.file; });
                // 随机系列（如 骰子_1..6）：记录基名 -> 文件列表，供"折叠成单格 + 点击随机抽"
                const series = {};
                (r.random_series || []).forEach(s => {
                    if (s && s.base && Array.isArray(s.files) && s.files.length >= 2) {
                        series[s.base] = s.files;
                    }
                });
                stickerRandomSeries = series;
                const meta = stickerManifest.find(p => p.id === pack) || {};
                stickerPackDisplayName = meta.name || pack;
                return map;
            }
        } catch (e) { /* silent */ }
        stickerRandomSeries = {};
        return {};
    }

    function renderStickerPackBar() {
        const bar = $("stickerPackBar");
        if (!bar) return;
        bar.innerHTML = "";
        if (!stickerManifest.length) {
            const empty = document.createElement("div");
            empty.className = "sticker-hint";
            empty.textContent = "服务端尚未配置表情包（请在 stickers/ 目录下新建表情包组）";
            bar.appendChild(empty);
            return;
        }
        stickerManifest.forEach(p => {
            const item = document.createElement("div");
            item.className = "sticker-pack-item" + (p.id === stickerActivePack ? " active" : "");
            if (p.logo) {
                const img = document.createElement("img");
                img.className = "sticker-pack-logo";
                img.alt = p.name || p.id;
                img.src = stickerPackUrl(p.id, p.logo);
                item.appendChild(img);
            } else {
                const fb = document.createElement("div");
                fb.className = "sticker-pack-logo-fallback";
                fb.textContent = "🧩";
                item.appendChild(fb);
            }
            const nm = document.createElement("div");
            nm.className = "sticker-pack-item-name";
            nm.textContent = p.name || p.id;
            nm.title = p.name || p.id;
            item.appendChild(nm);
            item.addEventListener("click", (e) => {
                e.stopPropagation();
                selectStickerPack(p.id);
            });
            bar.appendChild(item);
        });
    }

    function renderStickerGrid() {
        const grid = $("stickerGrid");
        const hint = $("stickerDownloadHint");
        const nameEl = $("stickerPackName");
        if (!grid) return;
        grid.innerHTML = "";
        if (nameEl) nameEl.textContent = stickerActivePack || "表情包";
        if (hint) hint.style.display = "none";

        if (!stickerActivePack) {
            const t = document.createElement("div");
            t.className = "sticker-hint";
            t.textContent = stickerManifest.length ? "请在上方选择一个表情包组" : "暂无可用表情包";
            grid.appendChild(t);
            return;
        }

        const downloaded = stickerLibrary[stickerActivePack];
        if (!downloaded) {
            // 未下载：询问是否下载
            const t = document.createElement("div");
            t.className = "sticker-locked";
            t.textContent = `「${stickerActivePack}」尚未下载到本地，下载后即可发送`;
            grid.appendChild(t);
            if (hint) hint.style.display = "block";
            return;
        }

        const files = Object.keys(downloaded.files || {});
        if (!files.length) {
            const t = document.createElement("div");
            t.className = "sticker-hint";
            t.textContent = "该表情包组内没有可用表情";
            grid.appendChild(t);
            return;
        }
        const names = stickerPackFiles || {};
        // 随机系列折叠：同一系列（如 骰子_1..6）只显示一个格子，点击再随机抽一张
        const seriesOf = stickerRandomSeries || {};
        const inSeries = new Set();
        Object.values(seriesOf).forEach(arr => (arr || []).forEach(fn => inSeries.add(fn)));

        const makeCell = (label, title, pickFiles) => {
            const cell = document.createElement("div");
            cell.className = "sticker-item";
            cell.title = title;
            const faces = pickFiles && pickFiles.length ? pickFiles : [];
            // 用第一张作为预览图（骰子显示 1 点那面，符合"骰子"的直觉）
            const previewFile = faces[0];
            const img = document.createElement("img");
            img.alt = label;
            // 注意：不要用 loading="lazy" —— 表情包面板最初是 display:none 的，
            // iOS Safari 对隐藏容器内的 lazy 图片可能长期不触发加载，表现为"格子空白"。
            // 表情图体积很小（压缩后 10~40KB），立即加载的代价可以忽略。
            const cache = window[STICKER_URL_CACHE] || {};
            const remoteUrl = stickerPackUrl(stickerActivePack, previewFile);
            const src = (cache[stickerActivePack] && cache[stickerActivePack][previewFile]) || remoteUrl;
            if (src) {
                img.src = src;
                bindStickerFallback(img, remoteUrl);   // 本地缓存失效时回退远端
            } else {
                // 没有可用的文件名：不设置 src（避免无效请求），只显示名称占位
                img.alt = title || label;
            }
            cell.appendChild(img);
            if (faces.length >= 2) {
                // 随机系列：右下角加一枚骰子角标，提示"这是随机的"
                const badge = document.createElement("span");
                badge.className = "sticker-random-badge";
                badge.textContent = `🎲${faces.length}`;
                badge.title = `随机表情：共 ${faces.length} 种，每次发送随机抽一个`;
                cell.appendChild(badge);
            }
            cell.addEventListener("click", (e) => {
                e.stopPropagation();
                if (faces.length >= 2) {
                    // 随机抽取一张：骰子每次点数都不一样
                    const pick = faces[Math.floor(Math.random() * faces.length)];
                    const raw = names[pick] || pick.replace(/\.[^.]+$/, "");
                    // 把「骰子_5」这类内部文件名转成更直观的展示名（骰子 5点），
                    // 会话列表与聊天记录里一眼就能看出掷出了几点。
                    const mm = /^(.*?)_(\d+)$/.exec(raw);
                    const shown = mm ? `${mm[1]} ${mm[2]}点` : raw;
                    sendSticker(stickerActivePack, pick, shown);
                } else {
                    sendSticker(stickerActivePack, previewFile, label);
                }
            });
            grid.appendChild(cell);
        };

        // 先渲染随机系列（排在前面，便于快速使用）
        Object.keys(seriesOf).sort().forEach(base => {
            const faces = (seriesOf[base] || []).filter(fn => downloaded.files[fn]);
            if (faces.length < 2) return;
            makeCell(base, `${base}（随机 ${faces.length} 种）`, faces);
        });
        // 再渲染普通表情
        files.forEach(fn => {
            if (inSeries.has(fn)) return;   // 已折叠进随机格子
            const nm = names[fn] || fn.replace(/\.[^.]+$/, "");
            // 必须把文件名传进去：previewFile 取自 faces[0]，
            // 传空会导致拼出 /stickers/<包>/undefined 的无效请求（表情格子变成碎图标）。
            makeCell(nm, nm, [fn]);
        });
    }

    async function selectStickerPack(packId) {
        stickerActivePack = packId || "";
        stickerPackFiles = stickerActivePack ? await loadStickerPackFiles(stickerActivePack) : {};
        renderStickerPackBar();
        renderStickerGrid();
    }

    // 下载并缓存一个表情包组。silent=true 用于默认组的后台自动下载（不弹窗、不占用按钮态）
    async function downloadStickerPack(packArg = null, silent = false) {
        const pack = packArg || stickerActivePack;
        if (!pack) return false;
        const btn = silent ? null : $("btnStickerDownload");
        if (btn) { btn.disabled = true; btn.textContent = "下载中…"; }
        try {
            // 下载清单必须用目标包自己的清单：后台下载别的包时不能覆盖当前面板状态
            const isCurrent = (pack === stickerActivePack);
            const names = (isCurrent && stickerPackFiles && Object.keys(stickerPackFiles).length)
                ? stickerPackFiles
                : await loadStickerPackFilesFor(pack);
            if (isCurrent) stickerPackFiles = names;
            const entries = Object.keys(names);
            if (!entries.length) {
                if (!silent) alert("该表情包组内没有可用表情");
                return false;
            }
            const meta = stickerManifest.find(p => p.id === pack) || {};
            const files = {};
            let fail = 0;
            for (const fn of entries) {
                try {
                    const url = stickerPackUrl(pack, fn);
                    if (!url) { fail++; continue; }
                    const resp = await fetch(url, { credentials: "same-origin" });
                    if (!resp.ok) { fail++; continue; }
                    const buf = await resp.arrayBuffer();
                    if (!buf || !buf.byteLength) { fail++; continue; }
                    // 存 ArrayBuffer 而不是 Blob：iOS Safari 把 Blob 写入 IndexedDB 后
                    // 读回可能得到空对象（体积为 0），是"表情包在 iPhone 上全空白"的根因。
                    // 存字节 + 记 MIME，读回时再用 Blob 构造，跨浏览器稳定。
                    files[fn] = {
                        buf: buf,
                        type: resp.headers.get("Content-Type") || "image/png",
                    };
                } catch (e) { fail++; }
            }
            if (!Object.keys(files).length) {
                if (!silent) alert("表情包下载失败，请检查网络后重试");
                return false;
            }
            const record = {
                id: pack,
                name: meta.name || pack,
                logo: meta.logo || "",
                files,
                count: Object.keys(files).length,
                savedAt: Date.now(),
            };
            await stickerDbPut(record);
            stickerLibrary[pack] = record;
            buildStickerUrlCache();
            renderStickerGrid();
            if (fail) console.warn(`[表情包] ${pack}: ${fail} 个文件下载失败`);
            return true;
        } catch (e) {
            if (!silent) alert(e.message || "表情包下载失败");
            return false;
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = "下载该表情包"; }
        }
    }

    // 只读取某个包的文件清单（不修改当前面板状态），供后台自动下载使用
    async function loadStickerPackFilesFor(pack) {
        try {
            const r = await gatewayApi(`/gateway/stickers/manifest?pack=${encodeURIComponent(pack)}`, "GET");
            if (r && r.ok && Array.isArray(r.stickers)) {
                const map = {};
                r.stickers.forEach(s => { if (s && s.file) map[s.file] = s.name || s.file; });
                return map;
            }
        } catch (e) { /* silent */ }
        return {};
    }

    // 当前可用的默认表情包组 id：
    // 以服务端排序为准（服务端保证默认组排在 packs[0]），若清单里能找到同名目录则优先用它，
    // 这样即使目录被改名，前端也不会失效（只是不再自动下载，用户手动点一下即可）。
    function resolveDefaultPack() {
        if (stickerManifest.some(p => p.id === DEFAULT_STICKER_PACK)) return DEFAULT_STICKER_PACK;
        return "";
    }

    // 默认表情包组：首次使用时后台静默缓存到本地，做到"开箱即用"（用户无需手动点下载）
    async function ensureDefaultStickerPack() {
        const pack = resolveDefaultPack();
        if (!pack || stickerLibrary[pack]) return false;
        try {
            return await downloadStickerPack(pack, true);
        } catch (e) {
            return false;
        }
    }

    async function openStickerPanel() {
        if (!isStickerAvailable()) {
            // 区分两种不可用场景，避免在未打开会话时也提示"辉夜不支持"造成误解
            if (!state.currentChat) {
                alert("请先打开好友或群聊，再使用表情包。");
            } else {
                alert("与辉夜的对话不支持表情包，请使用表情。");
            }
            return false;
        }
        if (!Object.keys(stickerLibrary).length) await loadStickerLibrary();
        // 每次打开都重新拉取清单：页面初始化时可能尚未登录（拿到空清单），
        // 且服务端随时可能新增/删除表情包组，重取可保证面板始终反映最新目录。
        await refreshStickerManifest();
        // 默认表情包组（含骰子）后台静默缓存，保证首次打开即可直接使用
        await ensureDefaultStickerPack();
        if (stickerActivePack && !stickerManifest.some(p => p.id === stickerActivePack)) {
            stickerActivePack = "";   // 当前选中的包已被移除
        }
        if (!stickerActivePack && stickerManifest.length) {
            // 默认表情包组优先选中（存在时才用），否则回退第一个包
            stickerActivePack = resolveDefaultPack() || stickerManifest[0].id;
        }
        if (stickerActivePack) {
            stickerPackFiles = await loadStickerPackFiles(stickerActivePack);
        }
        renderStickerPackBar();
        renderStickerGrid();
        return true;
    }

    async function sendSticker(pack, filename, displayName, seriesLabel) {
        if (!requireAuth()) return;
        if (!isStickerAvailable()) return;
        const url = stickerPackUrl(pack, filename);
        if (!url) {
            // 文件名缺失说明渲染层数据异常：直接拒绝，避免发出指向 /stickers/<包>/undefined 的坏消息
            console.warn("[表情包] 缺少文件名，已取消发送:", pack, filename);
            return;
        }
        // 随机系列（骰子等）：消息名带上点数，便于在聊天记录里一眼看出掷出了几点
        const shownName = displayName || "";
        const chatType = state.currentChat.chat_type;
        const target = state.currentChat.target;
        const tempId = `msg_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
        const curQuote = state.currentQuote;
        const msgObj = {
            id: tempId,
            sender: state.username,
            target: target,
            content: `[表情包] ${shownName}`,
            mtype: "sticker",
            image: url,
            sticker_name: shownName,
            time: Date.now() / 1000,
            recalled: false,
        };
        if (curQuote) msgObj.quote = curQuote;
        appendBubble(state.username, msgObj.content, "客户端", null, false, msgObj);
        const row = document.querySelector(`[data-msg-id="${tempId}"]`);
        if (row) row.dataset.local = "1";
        clearQuote();
        const panel = $("stickerPanel");
        if (panel) panel.style.display = "none";
        try {
            const body = {
                chat_type: chatType,
                target: target,
                text: "",
                image: url,
                mtype: "sticker",
                // 显式声明表情包名，避免服务端从 percent-encoded 的 URL 反解出乱码名字
                sticker_name: displayName,
            };
            if (curQuote) body.quote = curQuote;
            const res = await gatewayApi("/gateway/messages/send", "POST", body);
            if (res.ok && res.message) {
                const r2 = document.querySelector(`[data-msg-id="${tempId}"]`);
                if (r2) {
                    r2.dataset.msgId = res.message.id;
                    r2.removeAttribute("data-local");
                    delete r2.dataset.local;
                }
                if (window.__confirmedMsgId) window.__confirmedMsgId[tempId] = res.message.id;
                syncQuoteId(tempId, res.message.id);
            } else {
                appendBubble("系统", res.message || "表情包发送失败", "错误");
            }
            loadConversations();
        } catch (err) {
            appendBubble("系统", err.message || "表情包发送失败", "错误");
        }
    }

    function initStickerPanel() {
        const btn = $("btnStickerToggle");
        const panel = $("stickerPanel");
        if (btn && panel) {
            btn.addEventListener("click", async (e) => {
                e.stopPropagation();
                const willShow = panel.style.display === "none";
                $("emojiPicker").style.display = "none";
                closeMentionPicker();
                if (!willShow) { panel.style.display = "none"; return; }
                // 双击 openStickerPanel 已做可用性拦截；失败时不得强制展开空面板
                const ok = await openStickerPanel();
                if (!ok) return;
                panel.style.display = "flex";
            });
        }
        const mgr = $("btnStickerManage");
        if (mgr) {
            mgr.addEventListener("click", (e) => {
                e.stopPropagation();
                const bar = $("stickerPackBar");
                if (!bar) return;
                const hidden = getComputedStyle(bar).display === "none";
                bar.style.display = hidden ? "flex" : "none";
                mgr.textContent = hidden ? "收起" : "表情包组";
            });
        }
        const dl = $("btnStickerDownload");
        if (dl) {
            dl.addEventListener("click", (e) => {
                e.stopPropagation();
                downloadStickerPack();
            });
        }
        loadStickerLibrary();
        refreshStickerManifest();
        // 初始未打开任何会话：先隐藏入口，待 openChat 按会话类型决定是否显示，
        // 避免用户在无会话/辉夜会话下点到按钮却弹出"不支持"提示。
        applyStickerAvailability();
    }

    // ========== 群抽奖（抽取模式 / 骰子模式） ==========
    // 抽取模式：从已加入的群友中随机抽一位；
    // 骰子模式：已加入的群友各自投掷，3 秒冷却，实时高亮最高分（绿色）与最低分（红色）。
    const LOTTERY_DICE_FACES = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"];
    const lotteryState = {
        groupId: "",          // 面板绑定的群
        room: null,           // 服务端下发的抽奖公共视图
        members: [],          // 群成员详情（邀请列表用）
        mode: "draw",         // 发起表单当前选择的模式
        selected: new Set(),  // 发起表单勾选的邀请对象
        inviteOpen: false,    // 进行中的抽奖是否展开邀请面板
        inviteSelected: new Set(),
        loading: false,
        rolling: false,       // 投掷请求进行中（防止连点）
        cooldownEnd: 0,       // 本地冷却截止时间（ms）
        tickTimer: null,
        requestSeq: 0,
        error: "",
    };

    function isLotteryAvailable() {
        return !!(state.currentChat && state.currentChat.chat_type === "group");
    }

    function isLotteryModalOpen() {
        const ov = $("lottery-overlay");
        return !!(ov && ov.classList.contains("open"));
    }

    function setLotteryDot(show) {
        const dot = $("lotteryDot");
        if (dot) dot.classList.toggle("show", !!show);
    }

    function applyLotteryAvailability() {
        const btn = $("btnLottery");
        const ok = isLotteryAvailable();
        if (btn) btn.style.display = ok ? "" : "none";
        if (!ok) {
            closeLotteryModal();
            lotteryState.groupId = "";
            lotteryState.room = null;
            lotteryState.members = [];
            setLotteryDot(false);
        }
    }

    function stopLotteryTick() {
        if (lotteryState.tickTimer) {
            clearInterval(lotteryState.tickTimer);
            lotteryState.tickTimer = null;
        }
    }

    function closeLotteryModal() {
        const ov = $("lottery-overlay");
        if (ov) ov.classList.remove("open");
        stopLotteryTick();
    }

    function lotteryCooldownRemaining() {
        const ms = lotteryState.cooldownEnd - Date.now();
        return ms > 0 ? ms / 1000 : 0;
    }

    function applyLotteryRoom(room) {
        lotteryState.room = room || null;
        lotteryState.error = "";
        if (!room) {
            lotteryState.cooldownEnd = 0;
            setLotteryDot(false);
            return;
        }
        const me = (room.participants || []).find(p => p.username === state.username);
        if (room.status === "rolling" && me && me.last_roll && room.server_now) {
            // 以服务端时钟为准计算剩余冷却，客户端时间偏差不影响倒计时
            const remain = Math.max(0, (room.cooldown_seconds || 3) - (room.server_now - me.last_roll));
            lotteryState.cooldownEnd = Date.now() + remain * 1000;
        } else {
            lotteryState.cooldownEnd = 0;
        }
        setLotteryDot(room.status === "open" || room.status === "rolling");
    }

    function lotterySetError(msg) {
        lotteryState.error = msg || "";
        const el = $("lotteryError");
        if (el) {
            el.textContent = msg || "";
            el.style.display = msg ? "block" : "none";
        }
    }

    async function loadLotteryMembers(gid) {
        try {
            const r = await gatewayApi(`/gateway/groups/detail?group_id=${encodeURIComponent(gid)}`, "GET");
            if (r && r.ok && r.group && Array.isArray(r.group.members)) {
                state.cachedGroupMembers[gid] = r.group.members;
                return r.group.members.filter(m => m && m.username);
            }
        } catch (e) { /* 回退缓存 */ }
        return (state.cachedGroupMembers[gid] || []).filter(m => m && m.username);
    }

    async function refreshLotteryBadge(chatObj) {
        if (!chatObj || chatObj.chat_type !== "group") return;
        const gid = chatObj.target;
        try {
            const r = await gatewayApi(`/gateway/lottery/current?group_id=${encodeURIComponent(gid)}`, "GET");
            const cur = state.currentChat;
            if (!cur || cur.chat_type !== "group" || cur.target !== gid) return;
            lotteryState.groupId = gid;
            applyLotteryRoom(r && r.ok ? r.lottery : null);
            if (isLotteryModalOpen()) renderLottery();
        } catch (e) { /* 静默：抽奖状态不影响聊天 */ }
    }

    async function openLotteryModal() {
        if (!isLotteryAvailable()) return;
        const cur = state.currentChat;
        const gid = cur.target;
        const ov = $("lottery-overlay");
        if (!ov) return;
        const ep = $("emojiPicker"); if (ep) ep.style.display = "none";
        const sp = $("stickerPanel"); if (sp) sp.style.display = "none";
        closeMentionPicker();
        lotteryState.groupId = gid;
        lotteryState.error = "";
        lotteryState.inviteOpen = false;
        lotteryState.inviteSelected = new Set();
        lotteryState.loading = true;
        ov.classList.add("open");
        if ($("lotteryTitle")) $("lotteryTitle").textContent = `🎁 抽奖 · ${cur.name || "群聊"}`;
        renderLottery();

        const seq = ++lotteryState.requestSeq;
        try {
            const [membersRes, stateRes] = await Promise.allSettled([
                loadLotteryMembers(gid),
                gatewayApi(`/gateway/lottery/current?group_id=${encodeURIComponent(gid)}`, "GET"),
            ]);
            if (seq !== lotteryState.requestSeq) return;
            if (!state.currentChat || state.currentChat.chat_type !== "group" || state.currentChat.target !== gid) return;
            const members = membersRes.status === "fulfilled" ? membersRes.value : [];
            lotteryState.members = members;
            lotteryState.selected = new Set(members
                .filter(m => m.username !== state.username)
                .map(m => m.username));
            lotteryState.loading = false;
            const resp = stateRes.status === "fulfilled" ? stateRes.value : null;
            applyLotteryRoom(resp && resp.ok ? resp.lottery : null);
            renderLottery();
            if (stateRes.status === "rejected") {
                lotterySetError((stateRes.reason && stateRes.reason.message) || "加载抽奖信息失败");
            }
        } catch (e) {
            if (seq !== lotteryState.requestSeq) return;
            lotteryState.loading = false;
            renderLottery();
            lotterySetError(e.message || "加载抽奖信息失败");
        }
    }

    function lotteryOptionCard(m, selected, dataAttr) {
        const ownerTag = m.is_owner ? "群主 · " : "";
        const online = m.is_online ? "在线" : "离线";
        return `
            <div class="user-option-card${selected ? " selected" : ""}" ${dataAttr}="${encodeURIComponent(m.username)}">
                <div class="user-option-left">
                    <div class="user-option-avatar" data-avatar-user="${encodeURIComponent(m.username)}"></div>
                    <div class="user-option-info">
                        <span class="user-option-name">${escapeHtml(m.username)}</span>
                        <span class="user-option-sub">${ownerTag}${online}</span>
                    </div>
                </div>
                <div class="option-indicator"></div>
            </div>`;
    }

    function fillLotteryAvatars() {
        const body = $("lotteryBody");
        if (!body) return;
        body.querySelectorAll("[data-avatar-user]").forEach(el => {
            let uname = el.dataset.avatarUser || "";
            try { uname = decodeURIComponent(uname); } catch (e) { /* 保持原值 */ }
            fillUserAvatar(el, uname);
        });
    }

    function renderLottery() {
        const body = $("lotteryBody");
        if (!body) return;
        stopLotteryTick();
        if (lotteryState.loading) {
            body.innerHTML = '<div class="empty-hint">加载中...</div>';
            return;
        }
        const room = lotteryState.room;
        if (!room) {
            renderLotteryCreateForm(body);
        } else {
            renderLotteryRoom(body, room);
        }
        fillLotteryAvatars();
    }

    function renderLotteryCreateForm(body) {
        const mode = lotteryState.mode;
        const others = lotteryState.members.filter(m => m.username !== state.username);
        const picker = others.length
            ? others.map(m => lotteryOptionCard(m, lotteryState.selected.has(m.username), "data-pick-user")).join("")
            : '<div class="empty-hint">群内暂无其他成员可邀请，你可以独自开始</div>';
        const total = lotteryState.selected.size + 1;
        const allSelected = others.length > 0 && others.every(m => lotteryState.selected.has(m.username));

        body.innerHTML = `
            <div class="lottery-content lottery-create">
                <div class="lottery-hint-line">选择玩法后邀请群友，被邀请的群友会直接加入本局抽奖。</div>
                <div class="lottery-modes">
                    <div class="lottery-mode-card${mode === "draw" ? " active" : ""}" data-lottery-mode="draw">
                        <div class="lottery-mode-icon">🎯</div>
                        <div class="lottery-mode-name">抽取模式</div>
                        <div class="lottery-mode-desc">从已加入的群友中随机抽出一位</div>
                    </div>
                    <div class="lottery-mode-card${mode === "dice" ? " active" : ""}" data-lottery-mode="dice">
                        <div class="lottery-mode-icon">🎲</div>
                        <div class="lottery-mode-name">骰子模式</div>
                        <div class="lottery-mode-desc">每人投掷，3 秒冷却，实时高亮最高分和最低分</div>
                    </div>
                </div>
                <div class="field lottery-invite-field">
                    <div class="lottery-section-head">
                        <label>邀请群友加入（共 ${total} 人，含你自己）</label>
                        ${others.length ? `<button type="button" class="action-btn-small" id="btnLotterySelectAll">${allSelected ? "清空" : "全选"}</button>` : ""}
                    </div>
                    <div class="lottery-member-picker">${picker}</div>
                </div>
                <div class="lottery-error" id="lotteryError" style="display:${lotteryState.error ? "block" : "none"};">${escapeHtml(lotteryState.error)}</div>
            </div>
            <div class="lottery-footer">
                <button type="button" class="auth-btn" id="btnStartLottery">发起抽奖（${total} 人）</button>
            </div>
        `;

        body.querySelectorAll("[data-lottery-mode]").forEach(card => {
            card.addEventListener("click", () => {
                lotteryState.mode = card.dataset.lotteryMode === "dice" ? "dice" : "draw";
                body.querySelectorAll("[data-lottery-mode]").forEach(c => {
                    c.classList.toggle("active", c.dataset.lotteryMode === lotteryState.mode);
                });
            });
        });

        body.querySelectorAll("[data-pick-user]").forEach(card => {
            card.addEventListener("click", () => {
                let uname = card.dataset.pickUser || "";
                try { uname = decodeURIComponent(uname); } catch (e) { /* 保持原值 */ }
                if (lotteryState.selected.has(uname)) lotteryState.selected.delete(uname);
                else lotteryState.selected.add(uname);
                card.classList.toggle("selected", lotteryState.selected.has(uname));
                const countEl = body.querySelector(".lottery-section-head label");
                if (countEl) countEl.textContent = `邀请群友加入（共 ${lotteryState.selected.size + 1} 人，含你自己）`;
                const startBtn = $("btnStartLottery");
                if (startBtn) startBtn.textContent = `发起抽奖（${lotteryState.selected.size + 1} 人）`;
                const selectAllBtn = $("btnLotterySelectAll");
                if (selectAllBtn) {
                    const allNow = others.every(m => lotteryState.selected.has(m.username));
                    selectAllBtn.textContent = allNow ? "清空" : "全选";
                }
            });
        });

        const selectAll = $("btnLotterySelectAll");
        if (selectAll) {
            selectAll.addEventListener("click", () => {
                const all = others.every(m => lotteryState.selected.has(m.username));
                if (all) lotteryState.selected.clear();
                else others.forEach(m => lotteryState.selected.add(m.username));
                renderLottery();
            });
        }

        const startBtn = $("btnStartLottery");
        if (startBtn) startBtn.addEventListener("click", startLottery);
    }

    function lotteryPlayerRow(p, room, topSet, lowSet) {
        const isSelf = p.username === state.username;
        const isDrawWinner = room.mode === "draw" && room.status === "finished" && room.winner === p.username;
        const isDiceTop = room.mode === "dice" && room.status === "finished" && topSet.has(p.username);
        const isWinner = isDrawWinner || isDiceTop;
        const classes = ["lottery-player"];
        if (topSet.has(p.username)) classes.push("hi");
        if (lowSet.has(p.username)) classes.push("lo");
        if (isWinner) classes.push("winner");
        if (isSelf) classes.push("me");

        let scoreHtml = "";
        if (room.mode === "dice") {
            if (p.value != null) {
                const face = LOTTERY_DICE_FACES[p.value - 1] || "🎲";
                scoreHtml = `<span class="lottery-die">${face}</span><span class="lottery-score">${p.value}</span>`;
            } else {
                scoreHtml = '<span class="lottery-score-empty">未投掷</span>';
            }
        }
        const tags = [];
        if (isDrawWinner) tags.push('<span class="lottery-rank-tag winner">🏆 胜出</span>');
        if (isDiceTop) tags.push('<span class="lottery-rank-tag winner">🏆 最高</span>');
        if (topSet.has(p.username) && !isWinner) tags.push('<span class="lottery-rank-tag hi">最高</span>');
        if (lowSet.has(p.username)) tags.push('<span class="lottery-rank-tag lo">最低</span>');
        if (p.in_group === false) tags.push('<span class="lottery-rank-tag muted">已退群</span>');

        const sub = room.mode === "dice"
            ? (p.rolls ? `已投掷 ${p.rolls} 次` : "等待投掷")
            : (isDrawWinner ? "本局中奖者" : "已加入抽奖");
        return `
            <div class="${classes.join(" ")}">
                <div class="lottery-player-left">
                    <div class="lottery-avatar" data-avatar-user="${encodeURIComponent(p.username)}"></div>
                    <div class="lottery-player-info">
                        <div class="lottery-player-name">${escapeHtml(p.username)}${isSelf ? '<span class="lottery-me-tag">我</span>' : ""}</div>
                        <div class="lottery-player-sub">${sub}</div>
                    </div>
                </div>
                <div class="lottery-score-wrap">
                    ${tags.join("")}
                    ${scoreHtml}
                </div>
            </div>`;
    }

    function renderLotteryInviteBox(room) {
        if (!lotteryState.inviteOpen) return "";
        const inRoom = new Set((room.participants || []).map(p => p.username));
        const candidates = lotteryState.members.filter(m => m.username !== state.username && !inRoom.has(m.username));
        if (!candidates.length) {
            return '<div class="lottery-invite-box"><div class="empty-hint">群内其他成员都已在抽奖中</div></div>';
        }
        return `
            <div class="lottery-invite-box">
                <div class="lottery-invite-head">选择要邀请的群友</div>
                <div class="lottery-member-picker">
                    ${candidates.map(m => lotteryOptionCard(m, lotteryState.inviteSelected.has(m.username), "data-invite-user")).join("")}
                </div>
                <div class="lottery-invite-actions">
                    <button type="button" class="action-btn-small" id="btnLotteryInviteConfirm">确认邀请</button>
                    <button type="button" class="action-btn-small" id="btnLotteryInviteCancel">取消</button>
                </div>
            </div>`;
    }

    function renderLotteryRoom(body, room) {
        const isCreator = room.creator === state.username;
        // 发起者已退群时，允许其他群成员结束/取消，避免一局抽奖把群功能锁死
        const canManage = isCreator || room.creator_in_group === false;
        const isParticipant = (room.participants || []).some(p => p.username === state.username);
        const active = room.status === "open" || room.status === "rolling";
        const topSet = new Set(room.top || []);
        const lowSet = new Set(room.low || []);
        const statusText = room.status === "open" ? "等待抽取" : (room.status === "rolling" ? "投掷中" : "已结束");
        const modeBadge = room.mode === "draw" ? "🎯 抽取模式" : "🎲 骰子模式";

        let resultHtml = "";
        if (room.status === "finished") {
            if (room.mode === "draw" && room.winner) {
                resultHtml = `
                    <div class="lottery-result lottery-result-pop">
                        <div class="lottery-result-label">🎉 恭喜中奖</div>
                        <div class="lottery-result-name">${escapeHtml(room.winner)}</div>
                        <div class="lottery-result-sub">从 ${room.participant_count || 0} 位已加入的群友中抽出</div>
                    </div>`;
            } else if (room.mode === "dice") {
                const topNames = room.top || [];
                if (topNames.length) {
                    const tieLabel = topNames.length > 1 ? "🏆 最高分（并列）" : "🏆 最高分";
                    const nameClass = topNames.length > 2 ? "lottery-result-name small" : "lottery-result-name";
                    resultHtml = `<div class="lottery-result lottery-result-pop">
                           <div class="lottery-result-label">${tieLabel}</div>
                           <div class="${nameClass}">${escapeHtml(topNames.join("、"))}</div>
                           <div class="lottery-result-sub">${room.winner_value} 点 · 本局共 ${room.participant_count || 0} 人参与</div>
                       </div>`;
                } else {
                    resultHtml = `<div class="lottery-result"><div class="lottery-result-sub">本局无人投掷，抽奖已结束</div></div>`;
                }
            }
        }
        if (room.status === "open" && room.mode === "draw") {
            resultHtml = `<div class="lottery-result-sub lottery-waiting-hint">等待已加入的群友就位，点击下方按钮即可随机抽出一位。</div>`;
        }

        const participants = [...(room.participants || [])];
        if (room.mode === "dice") {
            // 骰子模式置顶规则：最高分组 → 最低分组 → 其余按点数降序 → 未投掷。
            // 使用服务端 top/low 的顺序（与参与者加入顺序一致），保证并列成员也不会乱序。
            const topOrder = new Map((room.top || []).map((name, idx) => [name, idx]));
            const lowOrder = new Map((room.low || []).map((name, idx) => [name, idx]));
            participants.sort((a, b) => {
                const aTop = topOrder.has(a.username);
                const bTop = topOrder.has(b.username);
                if (aTop !== bTop) return aTop ? -1 : 1;
                if (aTop && bTop) return topOrder.get(a.username) - topOrder.get(b.username);

                const aLow = lowOrder.has(a.username);
                const bLow = lowOrder.has(b.username);
                if (aLow !== bLow) return aLow ? -1 : 1;
                if (aLow && bLow) return lowOrder.get(a.username) - lowOrder.get(b.username);

                if (a.value == null && b.value == null) return 0;
                if (a.value == null) return 1;
                if (b.value == null) return -1;
                return b.value - a.value;
            });
        }
        const board = participants.length
            ? `<div class="lottery-player-list">${participants.map(p => lotteryPlayerRow(p, room, topSet, lowSet)).join("")}</div>`
            : '<div class="empty-hint">还没有人加入本局抽奖</div>';

        const actions = [];
        if (active && room.mode === "draw") {
            if (isCreator) {
                actions.push('<button type="button" class="auth-btn lottery-main-btn" id="btnLotteryDraw">🎯 开始抽取</button>');
            } else if (isParticipant) {
                actions.push('<div class="lottery-hint-line">已加入抽奖，等待发起者开奖。</div>');
            } else {
                actions.push('<div class="lottery-hint-line">你尚未加入本局抽奖，等待发起者邀请。</div>');
            }
        }
        if (active && room.mode === "dice" && isParticipant) {
            actions.push('<button type="button" class="auth-btn lottery-main-btn" id="btnLotteryRoll">🎲 投掷</button>');
            actions.push('<div class="lottery-cooldown-hint">每人每 3 秒可投掷一次，取最近一次点数</div>');
        } else if (active && room.mode === "dice" && !isParticipant) {
            actions.push('<div class="lottery-hint-line">你尚未加入本局抽奖，等待发起者邀请。</div>');
        }
        if (active && canManage) {
            if (isCreator) {
                actions.push('<button type="button" class="action-btn-small" id="btnLotteryInvite">+ 邀请群友</button>');
            }
            if (room.mode === "dice") {
                actions.push('<button type="button" class="action-btn-small" id="btnLotteryFinish">结束并公布</button>');
            }
            actions.push('<button type="button" class="action-btn-small danger" id="btnLotteryCancel">取消本局</button>');
        }
        if (!active) {
            const primaryLabel = isCreator ? "再来一局" : "发起新抽奖";
            const primaryId = isCreator ? "btnLotteryAgain" : "btnLotteryNew";
            actions.push(`
                <div class="lottery-finished-actions">
                    <button type="button" class="auth-btn lottery-main-btn" id="${primaryId}">${primaryLabel}</button>
                    <button type="button" class="action-btn-small lottery-exit-btn" id="btnLotteryExit">退出</button>
                </div>`);
        }

        const inviteBox = (active && isCreator && lotteryState.inviteOpen) ? renderLotteryInviteBox(room) : "";
        const boardOrInvite = inviteBox || board;

        body.innerHTML = `
            <div class="lottery-content lottery-room">
                <div class="lottery-room-head">
                    <span class="lottery-mode-badge ${room.mode}">${modeBadge}</span>
                    <span class="lottery-status ${room.status}">${statusText}</span>
                </div>
                <div class="lottery-room-meta">发起者：${escapeHtml(room.creator || "")} · 已加入 ${room.participant_count || 0} 人</div>
                ${resultHtml}
                ${boardOrInvite}
                <div class="lottery-error" id="lotteryError" style="display:${lotteryState.error ? "block" : "none"};">${escapeHtml(lotteryState.error)}</div>
            </div>
            <div class="lottery-footer">
                <div class="lottery-actions">${actions.join("")}</div>
            </div>
        `;

        // 邀请勾选
        body.querySelectorAll("[data-invite-user]").forEach(card => {
            card.addEventListener("click", () => {
                let uname = card.dataset.inviteUser || "";
                try { uname = decodeURIComponent(uname); } catch (e) { /* 保持原值 */ }
                if (lotteryState.inviteSelected.has(uname)) lotteryState.inviteSelected.delete(uname);
                else lotteryState.inviteSelected.add(uname);
                card.classList.toggle("selected", lotteryState.inviteSelected.has(uname));
            });
        });
        const inviteBtn = $("btnLotteryInvite");
        if (inviteBtn) {
            inviteBtn.addEventListener("click", () => {
                lotteryState.inviteOpen = !lotteryState.inviteOpen;
                lotteryState.inviteSelected = new Set();
                lotterySetError("");
                renderLottery();
            });
        }
        const inviteCancel = $("btnLotteryInviteCancel");
        if (inviteCancel) {
            inviteCancel.addEventListener("click", () => {
                lotteryState.inviteOpen = false;
                lotteryState.inviteSelected = new Set();
                renderLottery();
            });
        }
        const inviteConfirm = $("btnLotteryInviteConfirm");
        if (inviteConfirm) inviteConfirm.addEventListener("click", inviteLotteryMembers);
        const drawBtn = $("btnLotteryDraw");
        if (drawBtn) drawBtn.addEventListener("click", drawLottery);
        const rollBtn = $("btnLotteryRoll");
        if (rollBtn) rollBtn.addEventListener("click", rollLottery);
        const finishBtn = $("btnLotteryFinish");
        if (finishBtn) finishBtn.addEventListener("click", finishLottery);
        const cancelBtn = $("btnLotteryCancel");
        if (cancelBtn) cancelBtn.addEventListener("click", cancelLottery);
        const againBtn = $("btnLotteryAgain");
        if (againBtn) againBtn.addEventListener("click", startLotteryAgain);
        const newBtn = $("btnLotteryNew");
        if (newBtn) newBtn.addEventListener("click", backToLotteryCreate);
        const exitBtn = $("btnLotteryExit");
        // 退出结算页 = 回到选模式/发起界面，而不是关闭整个抽奖面板
        if (exitBtn) exitBtn.addEventListener("click", backToLotteryCreate);

        updateLotteryCooldownUI();
        if (room.status === "rolling") startLotteryTick();
    }

    function updateLotteryCooldownUI() {
        const btn = $("btnLotteryRoll");
        if (!btn || lotteryState.rolling) return;
        const remain = lotteryCooldownRemaining();
        if (remain > 0) {
            btn.disabled = true;
            btn.classList.add("cooling");
            btn.textContent = `⏳ 冷却 ${remain.toFixed(1)}s`;
        } else {
            btn.disabled = false;
            btn.classList.remove("cooling");
            if (lotteryState.error && lotteryState.error.indexOf("冷却") !== -1) lotterySetError("");
            const room = lotteryState.room;
            const me = room ? (room.participants || []).find(p => p.username === state.username) : null;
            btn.textContent = me && me.rolls ? "🎲 再投一次" : "🎲 投掷";
        }
    }

    function startLotteryTick() {
        stopLotteryTick();
        lotteryState.tickTimer = setInterval(() => {
            if (!isLotteryModalOpen()) { stopLotteryTick(); return; }
            updateLotteryCooldownUI();
        }, 200);
    }

    async function startLottery() {
        const gid = lotteryState.groupId;
        if (!gid) return;
        const btn = $("btnStartLottery");
        if (btn) { btn.disabled = true; btn.textContent = "发起中…"; }
        lotterySetError("");
        try {
            const r = await gatewayApi("/gateway/lottery/create", "POST", {
                group_id: gid,
                mode: lotteryState.mode,
                members: Array.from(lotteryState.selected),
            });
            if (r && r.ok) {
                applyLotteryRoom(r.lottery);
                lotteryState.error = "";
                renderLottery();
            } else {
                if (r && r.lottery) applyLotteryRoom(r.lottery);
                renderLottery();
                lotterySetError((r && r.message) || "发起抽奖失败");
            }
        } catch (e) {
            renderLottery();
            lotterySetError(e.message || "发起抽奖失败");
        }
    }

    async function startLotteryAgain() {
        const room = lotteryState.room;
        if (!room) return;
        lotteryState.mode = room.mode;
        lotteryState.selected = new Set((room.participants || [])
            .map(p => p.username)
            .filter(u => u && u !== state.username));
        // 重新拉取成员，避免使用过期的群成员列表
        lotteryState.members = await loadLotteryMembers(lotteryState.groupId);
        lotterySetError("");
        await startLottery();
    }

    async function backToLotteryCreate() {
        const gid = lotteryState.groupId;
        if (!gid) return;
        const prevMode = lotteryState.room ? lotteryState.room.mode : "draw";
        lotteryState.members = await loadLotteryMembers(gid);
        lotteryState.selected = new Set(lotteryState.members
            .filter(m => m.username !== state.username)
            .map(m => m.username));
        lotteryState.mode = prevMode;
        lotteryState.room = null;
        lotteryState.inviteOpen = false;
        lotteryState.inviteSelected = new Set();
        lotteryState.error = "";
        renderLottery();
    }

    async function rollLottery() {
        if (lotteryState.rolling) return;
        const gid = lotteryState.groupId;
        if (!gid) return;
        lotteryState.rolling = true;
        const btn = $("btnLotteryRoll");
        if (btn) {
            btn.disabled = true;
            btn.classList.add("rolling");
            btn.textContent = "🎲 投掷中…";
        }
        try {
            const r = await gatewayApi("/gateway/lottery/roll", "POST", { group_id: gid });
            if (r && r.ok) {
                applyLotteryRoom(r.lottery);
                lotterySetError("");
                renderLottery();
            } else if (r && r.code === "cooldown") {
                lotteryState.cooldownEnd = Date.now() + Math.max(0.1, Number(r.retry_after) || 1) * 1000;
                updateLotteryCooldownUI();
                lotterySetError(r.message || "投掷冷却中");
            } else {
                if (r && r.lottery) applyLotteryRoom(r.lottery);
                renderLottery();
                lotterySetError((r && r.message) || "投掷失败");
            }
        } catch (e) {
            renderLottery();
            lotterySetError(e.message || "投掷失败");
        } finally {
            lotteryState.rolling = false;
            const b = $("btnLotteryRoll");
            if (b) {
                b.classList.remove("rolling");
                updateLotteryCooldownUI();
            }
        }
    }

    async function drawLottery() {
        const gid = lotteryState.groupId;
        if (!gid) return;
        const btn = $("btnLotteryDraw");
        if (btn) { btn.disabled = true; btn.textContent = "抽取中…"; }
        try {
            const r = await gatewayApi("/gateway/lottery/draw", "POST", { group_id: gid });
            if (r && r.ok) {
                applyLotteryRoom(r.lottery);
                lotterySetError("");
                renderLottery();
            } else {
                if (r && r.lottery) applyLotteryRoom(r.lottery);
                renderLottery();
                lotterySetError((r && r.message) || "抽取失败");
            }
        } catch (e) {
            renderLottery();
            lotterySetError(e.message || "抽取失败");
        }
    }

    async function finishLottery() {
        if (!confirm("结束本局骰子抽奖，并根据当前点数公布最高分吗？")) return;
        const gid = lotteryState.groupId;
        if (!gid) return;
        try {
            const r = await gatewayApi("/gateway/lottery/finish", "POST", { group_id: gid });
            if (r && r.ok) {
                applyLotteryRoom(r.lottery);
                lotterySetError("");
                renderLottery();
            } else {
                if (r && r.lottery) applyLotteryRoom(r.lottery);
                renderLottery();
                lotterySetError((r && r.message) || "结束抽奖失败");
            }
        } catch (e) {
            renderLottery();
            lotterySetError(e.message || "结束抽奖失败");
        }
    }

    async function cancelLottery() {
        if (!confirm("确定取消本局抽奖吗？取消后所有成员都将看到本局关闭。")) return;
        const gid = lotteryState.groupId;
        if (!gid) return;
        try {
            const r = await gatewayApi("/gateway/lottery/cancel", "POST", { group_id: gid });
            if (r && r.ok) {
                applyLotteryRoom(null);
                lotteryState.inviteOpen = false;
                lotterySetError("");
                renderLottery();
            } else {
                renderLottery();
                lotterySetError((r && r.message) || "取消抽奖失败");
            }
        } catch (e) {
            renderLottery();
            lotterySetError(e.message || "取消抽奖失败");
        }
    }

    async function inviteLotteryMembers() {
        const gid = lotteryState.groupId;
        if (!gid) return;
        const selected = Array.from(lotteryState.inviteSelected);
        if (!selected.length) {
            lotterySetError("请先勾选要邀请的群友");
            return;
        }
        const btn = $("btnLotteryInviteConfirm");
        if (btn) { btn.disabled = true; btn.textContent = "邀请中…"; }
        try {
            const r = await gatewayApi("/gateway/lottery/invite", "POST", {
                group_id: gid,
                members: selected,
            });
            if (r && r.ok) {
                applyLotteryRoom(r.lottery);
                const added = (r.added || []).length;
                if (added) {
                    lotteryState.inviteOpen = false;
                    lotteryState.inviteSelected = new Set();
                    renderLottery();
                } else {
                    renderLottery();
                    lotterySetError(r.message || "所选群友已在抽奖中");
                }
            } else {
                if (r && r.lottery) applyLotteryRoom(r.lottery);
                renderLottery();
                lotterySetError((r && r.message) || "邀请失败");
            }
        } catch (e) {
            renderLottery();
            lotterySetError(e.message || "邀请失败");
        }
    }

    function onIncomingLotteryEvent(msg) {
        if (!msg || !msg.group_id) return;
        const cur = state.currentChat;
        if (!cur || cur.chat_type !== "group" || cur.target !== msg.group_id) return;
        lotteryState.groupId = msg.group_id;
        applyLotteryRoom(msg.lottery || null);
        if (isLotteryModalOpen()) renderLottery();
    }

    function initLottery() {
        const btn = $("btnLottery");
        if (btn) {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                if (isLotteryModalOpen()) closeLotteryModal();
                else openLotteryModal();
            });
        }
        const closeBtn = $("btnCloseLotteryModal");
        if (closeBtn) closeBtn.addEventListener("click", closeLotteryModal);
        const ov = $("lottery-overlay");
        if (ov) {
            ov.addEventListener("click", (e) => {
                if (e.target === ov) closeLotteryModal();
            });
        }
        applyLotteryAvailability();
    }

    // ========== 联系人搜索框与分栏切换 ==========
    const contactSearch = $("contactSearchInput");
    if (contactSearch) {
        contactSearch.addEventListener("input", renderContactList);
        contactSearch.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                const val = contactSearch.value.trim();
                if (val) {
                    openSearchModal(val);
                }
            }
        });
    }

    // 合并为一栏后不再有子标签切换（消息/好友/群聊统一展示）

    // ========== 模态弹窗：搜索与加好友 ==========
    function openSearchModal(initQuery = "") {
        const ov = $("search-modal-overlay");
        if (!ov) return;
        ov.classList.add("open");
        const inp = $("globalSearchInput");
        if (inp) {
            inp.value = initQuery;
            inp.focus();
            if (initQuery) doGlobalSearch();
        }
    }
    function closeSearchModal() {
        const ov = $("search-modal-overlay");
        if (ov) ov.classList.remove("open");
    }
    $("btnOpenSearch").addEventListener("click", () => openSearchModal());
    $("btnCloseSearchModal").addEventListener("click", closeSearchModal);

    async function doGlobalSearch() {
        const inp = $("globalSearchInput");
        const q = (inp ? inp.value : "").trim();
        const listEl = $("searchResultList");
        if (!listEl) return;
        if (!q) { listEl.innerHTML = '<div class="empty-hint">请输入关键词搜索</div>'; return; }
        listEl.innerHTML = '<div class="empty-hint">正在搜索...</div>';
        try {
            const r = await gatewayApi(`/gateway/users/search?q=${encodeURIComponent(q)}`, "GET");
            if (!r.ok || !r.users || !r.users.length) {
                listEl.innerHTML = '<div class="empty-hint">未找到匹配的用户</div>';
                return;
            }
            listEl.innerHTML = "";
            r.users.forEach(u => {
                const div = document.createElement("div");
                div.className = "search-user-item";
                let actionBtn = "";
                if (u.is_friend) {
                    actionBtn = '<span class="action-btn-small" style="opacity:0.6">已是好友</span>';
                } else if (u.has_pending_request) {
                    actionBtn = '<span class="action-btn-small" style="opacity:0.6">已申请</span>';
                } else if (u.incoming_request) {
                    actionBtn = `<button class="action-btn-small" data-act="accept-user" data-user="${u.username}">同意加好友</button>`;
                } else {
                    actionBtn = `<button class="action-btn-small" data-act="apply-user" data-user="${u.username}">添加好友</button>`;
                }
                div.innerHTML = `
                    <div class="user-item-left">
                        <div class="contact-avatar" style="width:34px;height:34px;font-size:14px;">${u.username.slice(0,1).toUpperCase()}</div>
                        <div>
                            <div class="user-item-name">${escapeHtml(u.username)}</div>
                            <div class="user-item-id">ID: ${escapeHtml(u.user_id || "--")} · ${u.role}</div>
                        </div>
                    </div>
                    <div>${actionBtn}</div>
                `;
                div.querySelectorAll("[data-act]").forEach(b => {
                    b.addEventListener("click", async () => {
                        const act = b.dataset.act;
                        const targetUser = b.dataset.user;
                        if (act === "apply-user") {
                            const msg = prompt(`向 ${targetUser} 发送好友申请附言：`, "请求添加您为好友");
                            if (msg === null) return;
                            try {
                                const resp = await gatewayApi("/gateway/friends/apply", "POST", { target_username: targetUser, message: msg });
                                alert(resp.message || "申请已发送");
                                doGlobalSearch();
                                loadFriends();
                                loadConversations();
                            } catch (err) { alert(err.message || "申请失败"); }
                        } else if (act === "accept-user") {
                            try {
                                // 查找该用户对应的 request_id
                                const reqs = await gatewayApi("/gateway/friends/requests", "GET");
                                const foundReq = (reqs.received || []).find(r => r.from_user === targetUser);
                                if (foundReq) {
                                    const resp = await gatewayApi("/gateway/friends/handle_request", "POST", { request_id: foundReq.id, action: "accept" });
                                    alert(resp.message || "已同意好友申请");
                                } else {
                                    alert("未找到好友申请");
                                }
                                doGlobalSearch();
                                loadFriends();
                                loadConversations();
                            } catch (err) { alert(err.message || "操作失败"); }
                        }
                    });
                });
                listEl.appendChild(div);
            });
        } catch (e) {
            listEl.innerHTML = `<div class="empty-hint">${e.message || "搜索失败"}</div>`;
        }
    }
    $("btnDoGlobalSearch").addEventListener("click", doGlobalSearch);
    $("globalSearchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doGlobalSearch(); });

    // ========== 模态弹窗：好友申请与通知 ==========
    async function openRequestsModal() {
        const ov = $("requests-modal-overlay");
        if (!ov) return;
        ov.classList.add("open");
        const listEl = $("friendRequestsList");
        if (!listEl) return;
        listEl.innerHTML = '<div class="empty-hint">加载中...</div>';
        try {
            const r = await gatewayApi("/gateway/friends/requests", "GET");
            const received = r.received || [];
            if (!received.length) {
                listEl.innerHTML = '<div class="empty-hint">暂无好友申请</div>';
                return;
            }
            listEl.innerHTML = "";
            received.forEach(req => {
                const div = document.createElement("div");
                div.className = "request-user-item";
                div.innerHTML = `
                    <div class="user-item-left">
                        <div class="contact-avatar" style="width:34px;height:34px;">${req.from_user.slice(0,1).toUpperCase()}</div>
                        <div>
                            <div class="user-item-name">${escapeHtml(req.from_user)}</div>
                            <div class="user-item-id">${escapeHtml(req.message || "请求加好友")}</div>
                        </div>
                    </div>
                    <div style="display:flex;gap:6px;">
                        <button class="action-btn-small" data-act="accept" data-id="${req.id}">同意</button>
                        <button class="action-btn-small danger" data-act="reject" data-id="${req.id}">拒绝</button>
                    </div>
                `;
                div.querySelectorAll("[data-act]").forEach(b => {
                    b.addEventListener("click", async () => {
                        const act = b.dataset.act;
                        const reqId = b.dataset.id;
                        try {
                            const resp = await gatewayApi("/gateway/friends/handle_request", "POST", { request_id: reqId, action: act });
                            alert(resp.message || "操作完成");
                            openRequestsModal();
                            loadFriendRequests();
                            loadFriends();
                            loadConversations();
                        } catch (err) { alert(err.message || "处理失败"); }
                    });
                });
                listEl.appendChild(div);
            });
        } catch (e) {
            listEl.innerHTML = `<div class="empty-hint">${e.message || "加载失败"}</div>`;
        }
    }
    $("btnOpenRequests").addEventListener("click", openRequestsModal);
    $("btnCloseRequestsModal").addEventListener("click", () => {
        const ov = $("requests-modal-overlay"); if (ov) ov.classList.remove("open");
    });

    // ========== 模态弹窗：创建群聊 ==========
    function openCreateGroupModal() {
        const ov = $("create-group-overlay");
        if (!ov) return;
        ov.classList.add("open");
        $("newGroupName").value = "";
        const box = $("groupSelectFriends");
        if (!box) return;
        box.innerHTML = "";
        if (!state.friends.length) {
            box.innerHTML = '<div class="empty-hint">暂无好友可邀请，请先添加好友</div>';
            return;
        }
        state.friends.forEach(f => {
            const card = document.createElement("div");
            card.className = "user-option-card";
            card.dataset.username = f.username;
            card.innerHTML = `
                <div class="user-option-left">
                    <div class="user-option-avatar"></div>
                    <div class="user-option-info">
                        <span class="user-option-name">${escapeHtml(f.alias || f.username)}</span>
                        <span class="user-option-sub">@${escapeHtml(f.username)}</span>
                    </div>
                </div>
                <div class="option-indicator"></div>
            `;
            const avEl = card.querySelector(".user-option-avatar");
            fillUserAvatar(avEl, f.username);
            card.addEventListener("click", () => {
                card.classList.toggle("selected");
            });
            box.appendChild(card);
        });
    }
    $("btnOpenCreateGroup").addEventListener("click", openCreateGroupModal);
    $("btnCloseCreateGroupModal").addEventListener("click", () => {
        const ov = $("create-group-overlay"); if (ov) ov.classList.remove("open");
    });

    $("btnConfirmCreateGroup").addEventListener("click", async () => {
        const name = ($("newGroupName") ? $("newGroupName").value : "").trim();
        if (!name) { alert("请输入群聊名称"); return; }
        const selected = [];
        document.querySelectorAll("#groupSelectFriends .user-option-card.selected").forEach(card => {
            if (card.dataset.username) selected.push(card.dataset.username);
        });
        if (selected.length < 2) {
            alert("建群人数不少于3人，请至少勾选邀请2位好友");
            return;
        }
        try {
            const r = await gatewayApi("/gateway/groups/create", "POST", { name, members: selected });
            if (r.ok && r.group) {
                alert("群聊创建成功！");
                $("create-group-overlay").classList.remove("open");
                await loadGroups();
                await loadConversations();
                openChat({
                    chat_type: "group",
                    target: r.group.id,
                    name: r.group.name,
                    member_count: (r.group.members || []).length,
                    pinned: false,
                    muted: false,
                });
            } else {
                alert(r.message || "创建失败");
            }
        } catch (e) { alert(e.message || "创建群聊失败"); }
    });

    // ========== 模态弹窗：会话设置 / 群成员管理 ==========
    async function openChatSettingsModal() {
        const cur = state.currentChat;
        if (!cur) return;
        const ov = $("chat-settings-overlay");
        const titleEl = $("chatSettingsTitle");
        const bodyEl = $("chatSettingsBody");
        if (!ov || !bodyEl) return;
        ov.classList.add("open");
        titleEl.textContent = `设置 · ${cur.name || cur.target}`;
        bodyEl.innerHTML = '<div class="empty-hint">加载中...</div>';

        if (cur.chat_type === "bot") {
            bodyEl.innerHTML = `
                <div class="field">
                    <label>会话选项</label>
                    <div class="setting-switch-item ${cur.pinned ? "active" : ""}" id="rowBotPin">
                        <div class="setting-switch-label">
                            <span class="setting-switch-title">置顶聊天</span>
                            <span class="setting-switch-desc">将官方AI置顶在会话列表最上方</span>
                        </div>
                        <div class="toggle-switch"></div>
                    </div>
                    <div class="setting-switch-item ${cur.muted ? "active" : ""}" id="rowBotMute">
                        <div class="setting-switch-label">
                            <span class="setting-switch-title">消息免打扰</span>
                            <span class="setting-switch-desc">静音新消息提醒</span>
                        </div>
                        <div class="toggle-switch"></div>
                    </div>
                </div>
            `;
            $("rowBotPin").addEventListener("click", async () => {
                cur.pinned = !cur.pinned;
                $("rowBotPin").classList.toggle("active", cur.pinned);
                await gatewayApi("/gateway/bot/settings", "POST", { pinned: cur.pinned });
                loadConversations();
            });
            $("rowBotMute").addEventListener("click", async () => {
                cur.muted = !cur.muted;
                $("rowBotMute").classList.toggle("active", cur.muted);
                await gatewayApi("/gateway/bot/settings", "POST", { muted: cur.muted });
                updateChatHeader(cur);
                loadConversations();
            });
            return;
        }

        if (cur.chat_type === "user") {
            bodyEl.innerHTML = `
                <div class="field">
                    <label>会话选项</label>
                    <div class="setting-switch-item ${cur.pinned ? "active" : ""}" id="rowUserPin">
                        <div class="setting-switch-label">
                            <span class="setting-switch-title">置顶聊天</span>
                            <span class="setting-switch-desc">将会话固定在会话列表顶部</span>
                        </div>
                        <div class="toggle-switch"></div>
                    </div>
                    <div class="setting-switch-item ${cur.muted ? "active" : ""}" id="rowUserMute">
                        <div class="setting-switch-label">
                            <span class="setting-switch-title">消息免打扰</span>
                            <span class="setting-switch-desc">开启后不再显示未读高亮</span>
                        </div>
                        <div class="toggle-switch"></div>
                    </div>
                </div>
                <div class="field">
                    <label>好友备注</label>
                    <input type="text" id="setUserAlias" placeholder="设置备注名..." value="${escapeHtml(cur.name !== cur.target ? cur.name : "")}">
                    <button class="auth-btn" id="btnSaveAlias" style="margin-top:6px;height:32px;font-size:12px;">保存备注</button>
                </div>
                <div class="field" style="margin-top:10px;">
                    <button class="account-btn danger" id="btnDelFriendModal">删除该好友</button>
                </div>
            `;
            $("rowUserPin").addEventListener("click", async () => {
                cur.pinned = !cur.pinned;
                $("rowUserPin").classList.toggle("active", cur.pinned);
                await gatewayApi("/gateway/friends/settings", "POST", { target_username: cur.target, pinned: cur.pinned });
                loadConversations();
                loadFriends();
            });
            $("rowUserMute").addEventListener("click", async () => {
                cur.muted = !cur.muted;
                $("rowUserMute").classList.toggle("active", cur.muted);
                await gatewayApi("/gateway/friends/settings", "POST", { target_username: cur.target, muted: cur.muted });
                updateChatHeader(cur);
                loadConversations();
                loadFriends();
            });
            $("btnSaveAlias").addEventListener("click", async () => {
                const alias = $("setUserAlias").value.trim();
                await gatewayApi("/gateway/friends/settings", "POST", { target_username: cur.target, alias });
                cur.name = alias || cur.target;
                updateChatHeader(cur);
                loadConversations();
                loadFriends();
                alert("备注已更新");
            });
            $("btnDelFriendModal").addEventListener("click", async () => {
                if (!confirm(`确认解除与「${cur.target}」的好友关系？`)) return;
                await gatewayApi("/gateway/friends/delete", "POST", { target_username: cur.target });
                ov.classList.remove("open");
                closeChat();
                loadFriends();
                loadConversations();
            });
            return;
        }

        if (cur.chat_type === "group") {
            try {
                const r = await gatewayApi(`/gateway/groups/detail?group_id=${encodeURIComponent(cur.target)}`, "GET");
                if (!r.ok || !r.group) {
                    bodyEl.innerHTML = '<div class="empty-hint">获取群信息失败</div>';
                    return;
                }
                const g = r.group;
                const isOwner = g.owner === state.username;
                let membersHtml = (g.members || []).map(m => `
                    <div class="search-user-item" style="padding:6px 10px;">
                        <div class="user-item-left">
                            <div class="contact-avatar" style="width:28px;height:28px;font-size:12px;">${m.username.slice(0,1).toUpperCase()}</div>
                            <div>
                                <span style="font-weight:600;font-size:13px;">${escapeHtml(m.username)}</span>
                                ${m.is_owner ? '<span class="ai-flag" style="background:#2d6cdf;color:#fff;border:none;">群主</span>' : ""}
                                <span style="font-size:11px;color:var(--text-dim);margin-left:4px;">${m.is_online ? "在线" : "离线"}</span>
                            </div>
                        </div>
                    </div>
                `).join("");

                bodyEl.innerHTML = `
                    <div class="field">
                        <label>群聊名称</label>
                        <input type="text" id="setGroupName" value="${escapeHtml(g.name)}" ${isOwner ? "" : "disabled"}>
                        ${isOwner ? '<button class="auth-btn" id="btnSaveGroupName" style="margin-top:6px;height:32px;font-size:12px;">修改群名</button>' : ""}
                    </div>
                    <div class="field">
                        <label>群聊选项</label>
                        <div class="setting-switch-item ${g.pinned ? "active" : ""}" id="rowGroupPin">
                            <div class="setting-switch-label">
                                <span class="setting-switch-title">置顶聊天</span>
                                <span class="setting-switch-desc">将本群固定在会话列表顶部</span>
                            </div>
                            <div class="toggle-switch"></div>
                        </div>
                        <div class="setting-switch-item ${g.muted ? "active" : ""}" id="rowGroupMute">
                            <div class="setting-switch-label">
                                <span class="setting-switch-title">消息免打扰</span>
                                <span class="setting-switch-desc">开启后本群不提示未读高亮</span>
                            </div>
                            <div class="toggle-switch"></div>
                        </div>
                    </div>
                    <div class="field">
                        <div style="display:flex;justify-content:space-between;align-items:center;">
                            <label>群成员 (${g.members.length})</label>
                            <button class="action-btn-small" id="btnInviteToGroup">+ 邀请好友</button>
                        </div>
                        <div style="max-height:160px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;margin-top:6px;">
                            ${membersHtml}
                        </div>
                    </div>
                    <div class="field" style="margin-top:10px;">
                        <button class="account-btn danger" id="btnLeaveGroupModal">${isOwner ? "解散/退出群聊" : "退出群聊"}</button>
                    </div>
                `;

                $("rowGroupPin").addEventListener("click", async () => {
                    cur.pinned = !cur.pinned;
                    $("rowGroupPin").classList.toggle("active", cur.pinned);
                    await gatewayApi("/gateway/groups/settings", "POST", { group_id: cur.target, pinned: cur.pinned });
                    loadConversations();
                    loadGroups();
                });
                $("rowGroupMute").addEventListener("click", async () => {
                    cur.muted = !cur.muted;
                    $("rowGroupMute").classList.toggle("active", cur.muted);
                    await gatewayApi("/gateway/groups/settings", "POST", { group_id: cur.target, muted: cur.muted });
                    updateChatHeader(cur);
                    loadConversations();
                    loadGroups();
                });
                if ($("btnSaveGroupName")) {
                    $("btnSaveGroupName").addEventListener("click", async () => {
                        const newName = $("setGroupName").value.trim();
                        if (!newName) return;
                        await gatewayApi("/gateway/groups/settings", "POST", { group_id: cur.target, name: newName });
                        cur.name = newName;
                        updateChatHeader(cur);
                        loadConversations();
                        loadGroups();
                        alert("群名称已更新");
                    });
                }
                $("btnInviteToGroup").addEventListener("click", () => {
                    const existU = new Set(g.members.map(x => x.username));
                    const canInvite = state.friends.filter(f => !existU.has(f.username));
                    if (!canInvite.length) { alert("所有好友均已在该群中"); return; }
                    // 选项卡片替代原生勾选框，带头像与选中微交互
                    const wrap = document.createElement("div");
                    wrap.className = "invite-picker-mask";
                    wrap.innerHTML = `
                        <div class="menu-card invite-picker-card">
                            <div class="pixel-block"></div>
                            <div class="card-content modal-content">
                                <div class="card-head">
                                    <span class="card-title">邀请好友入群</span>
                                    <button class="modal-close-btn" data-invite-cancel>✕</button>
                                </div>
                                <div class="modal-body">
                                    <label class="field-label">选择要邀请的好友</label>
                                    <div class="invite-pick-list" id="invitePickList"></div>
                                    <button class="auth-btn" data-invite-ok style="margin-top:10px;">确认邀请</button>
                                </div>
                            </div>
                        </div>
                    `;
                    const listContainer = wrap.querySelector("#invitePickList");
                    canInvite.forEach(f => {
                        const card = document.createElement("div");
                        card.className = "user-option-card";
                        card.dataset.username = f.username;
                        card.innerHTML = `
                            <div class="user-option-left">
                                <div class="user-option-avatar"></div>
                                <div class="user-option-info">
                                    <span class="user-option-name">${escapeHtml(f.alias || f.username)}</span>
                                    <span class="user-option-sub">@${escapeHtml(f.username)}</span>
                                </div>
                            </div>
                            <div class="option-indicator"></div>
                        `;
                        const avEl = card.querySelector(".user-option-avatar");
                        fillUserAvatar(avEl, f.username);
                        card.addEventListener("click", () => {
                            card.classList.toggle("selected");
                        });
                        listContainer.appendChild(card);
                    });
                    document.body.appendChild(wrap);
                    const close = () => wrap.remove();
                    // 点击空白处回退
                    wrap.addEventListener("click", (e) => { if (e.target === wrap) close(); });
                    wrap.querySelector("[data-invite-cancel]").addEventListener("click", close);
                    wrap.querySelector("[data-invite-ok]").addEventListener("click", async () => {
                        const picked = [];
                        wrap.querySelectorAll(".user-option-card.selected").forEach(c => {
                            if (c.dataset.username) picked.push(c.dataset.username);
                        });
                        if (!picked.length) { alert("请至少选择一位好友"); return; }
                        try {
                            const invResp = await gatewayApi("/gateway/groups/invite", "POST", { group_id: cur.target, members: picked });
                            close();
                            alert(invResp.message || "邀请已发送");
                            openChatSettingsModal();
                            loadGroups();
                            loadConversations();
                        } catch (err) { alert(err.message || "邀请失败"); }
                    });
                });
                $("btnLeaveGroupModal").addEventListener("click", async () => {
                    if (!confirm(`确认退出群聊「${g.name}」？`)) return;
                    await gatewayApi("/gateway/groups/leave", "POST", { group_id: cur.target });
                    ov.classList.remove("open");
                    closeChat();
                    loadGroups();
                    loadConversations();
                });
            } catch (e) {
                bodyEl.innerHTML = `<div class="empty-hint">${e.message || "加载群信息失败"}</div>`;
            }
        }
    }
    $("btnChatSettings").addEventListener("click", openChatSettingsModal);
    $("btnCloseChatSettingsModal").addEventListener("click", () => {
        const ov = $("chat-settings-overlay"); if (ov) ov.classList.remove("open");
    });

    // ========== 所有模态弹窗：点击空白（遮罩背景）回退 ==========
    ["search-modal-overlay", "requests-modal-overlay", "create-group-overlay", "chat-settings-overlay"]
        .forEach(id => {
            const ov = $(id);
            if (!ov) return;
            ov.addEventListener("click", (e) => {
                if (e.target === ov) ov.classList.remove("open");  // 仅点卡片外部空白才关闭
            });
        });

    // ========== WebSocket 接收分发处理 ==========
    function onIncomingChatMessage(msg) {
        const { chat_type, from, target, group_id, message } = msg;
        const cur = state.currentChat;
        const msgId = message && message.id;
        // 发送者身份以 message.sender 为准（群聊广播不带 from 字段，仅凭 from 会把自己的消息误判为他人消息）
        const senderName = (message && message.sender) || from || "";
        const isSelf = !!(senderName && (senderName === state.username || senderName === state.displayName));

        let isCurrent = false;
        if (cur) {
            if (chat_type === "user" && cur.chat_type === "user" && (cur.target === from || cur.target === target)) {
                isCurrent = true;
            } else if (chat_type === "group" && cur.chat_type === "group" && cur.target === group_id) {
                isCurrent = true;
            }
        }

        // 去重：同一消息（服务器广播 vs 本地乐观渲染）只渲染一次。
        //  - 自己发的：sendChat/sendImageChat 已本地乐观上屏，WS 回声只做「认领转正」不再新建气泡；
        //  - 他人发的：按 msg-id 去重，避免轮询/重连补发造成重复。
        if (isSelf) {
            if (isCurrent && msgId) {
                const hit = document.querySelector(`[data-msg-id="${msgId}"]`);
                if (hit) { loadConversations(); return; }   // 已按真实 id 渲染
                // 本地乐观气泡尚未转正：按 FIFO 认领最早的一条未确认自己消息（支持连发相同内容）
                const candidates = Array.from(document.querySelectorAll("#chatView .msg-row.user"))
                    .filter(r => r.dataset.local === "1" || (r.dataset.msgId || "").startsWith("local_"));
                const claimed = candidates.find(r => {
                    const mid = r.dataset.msgId || "";
                    if (window.__confirmedMsgId && window.__confirmedMsgId[mid] === msgId) return true;
                    const t = r.querySelector(".msg-text");
                    return t && t.textContent === ((message && (message.content || message.text)) || "");
                }) || candidates[0];
                if (claimed) {
                    const oldId = claimed.dataset.msgId;
                    if (oldId && window.__confirmedMsgId) window.__confirmedMsgId[oldId] = msgId;
                    claimed.dataset.msgId = msgId;
                    claimed.removeAttribute("data-local");
                    delete claimed.dataset.local;
                    if (oldId) syncQuoteId(oldId, msgId);   // 引用目标 ID 同步
                    if (message && message.time != null) claimed.dataset.msgTime = String(message.time);
                    loadConversations();
                    return;   // 认领成功，绝不新建第二条
                }
            }
            loadConversations();
            return;
        }

        if (isCurrent && msgId && document.querySelector(`[data-msg-id="${msgId}"]`)) {
            loadConversations();
            return;
        }

        if (isCurrent) {
            const view = $("chatView");
            // 实时消息同样按 5 分钟规则插入时间分隔
            const prevT = lastMsgTimeIn(view);
            if (message && message.time != null && needTimeDivider(prevT, message.time)) {
                view.appendChild(makeTimeDivider(message.time));
            }
            const row = buildBubbleRow(message);
            if (row && view) { row.classList.add("msg-enter"); view.appendChild(row); scrollChatToBottom(); }
            // 正在查看的会话：消息即读，推进已读位置（避免下次进来又被标为新消息）
            if (cur) markConversationRead(cur);
        } else {
            const key = chat_type === "group" ? group_id : from;
            state.unreadCounts[key] = (state.unreadCounts[key] || 0) + 1;
        }
        loadConversations();
    }

    function onIncomingChatRecall(msg) {
        markMessageRecalled(msg.msg_id, msg.recalled_by);
        loadConversations();
    }

    function onIncomingSocialEvent(msg) {
        if (msg.event === "friend_request") {
            loadFriendRequests();
        } else if (msg.event === "friend_accepted") {
            loadFriends();
            loadConversations();
        } else if (msg.event === "group_created" || msg.event === "group_updated") {
            loadGroups();
            loadConversations();
        }
    }

    // ========== 初始化 ==========
    async function init() {
        window.addEventListener("beforeunload", stopBreathing);
        loadConfig();
        readThemeFromNav();
        applyTheme();
        initEmojiPicker();
        initStickerPanel();
        initLottery();
        connectWs();
        if (state.token) {
            await validateAndEnterSession();
        } else {
            doLogout();
        }
    }

    init();
})();
