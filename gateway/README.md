# gateway/README.md

# Nascence 辉夜 · 三端 Web 客户端

统一由 `webui.py` 一键启动（管理面板 + 网页版客户端 + 三端登录），
`gateway/server.py` 已并入主服务（合并一键启动）。

## 三端架构

- **学生端**：学生与辉夜单独对话，拥有独立记忆体 `data/_memory/<user_id>/`。
- **家长端**：家长注册后「连接学生」（输入学生 user_id）→ 与该学生**共享同一记忆体**
  （仅历史对话独立在 `data/<家长用户名>/`）。
- **老师端**：与家长端相同，可连接多个学生并在界面切换查看的记忆体。

三端共用同一套网页；注册时选择身份，登录后顶栏账号菜单显示「当前端 + user_id + 已连接学生」。

## 快速开始

```bash
# 一键启动（管理面板 8787 + 网页版客户端 8987）
python webui.py            # 或 bash start.sh
python webui.py --cognition  # 无界面自动启动认知循环
```

浏览器访问：
- 客户端：`http://127.0.0.1:8987/`（或 `http://127.0.0.1:8787/`）
- 管理台：`http://127.0.0.1:8787/admin`

首次使用在页面上「注册」（选择 学生/家长/老师）即可创建账号；注册成功即自动登录。

## 网关接口

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/gateway/register` | POST | 注册 {username, password, role}，返回 token/sign_key/role/user_id |
| `/gateway/login` | POST | 登录 {username, password}，返回 token/sign_key/role/user_id/linked_students |
| `/gateway/profile` | GET | 获取当前用户资料（角色/user_id/绑定），供刷新恢复 |
| `/gateway/bind` | POST | 家长/老师连接学生 {token, student_user_id} |
| `/gateway/chat` | POST | 对话 {token, sender, text, mentioned, memory_user_id?} |
| `/gateway/history` | GET | 获取当前用户消息历史 {token, memory_user_id?} |
| `/gateway/bot` | GET | 获取 bot 名字 |

## 数据目录

- 记忆体（共享）：`data/_memory/<student_user_id>/` → memory.db / faiss.index / memory.json / clock_state.txt / metrics
- 历史对话（按用户独立）：`data/<username>/` → message_state.json / dialogue_state.json / dialogue_log.jsonl

多用户共享记忆体时，SQLite（WAL）为权威存储，各 worker 每轮处理前增量同步，
确保家长与学生的对话互见对方新写入的记忆。

## 说明

- 对话处理复用项目核心 `core.cognition.process_dialogue` 与模型后端配置
  （文本走外部 API 或本地 GGUF，embedding 走本地 llama.cpp）。
- 每条记忆自动附带 `user_id` 与 `speaker_role` 标签（追溯来源用户与角色）。
