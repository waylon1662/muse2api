# 固定会话（pin thread）设计分析

> 分析对象：`~/workspace/muse2api-main`，czg86389-hub/muse2api 克隆，HEAD `19db32a`，版本 1.6.11。
> 方法：只读代码，未改任何文件，未启动服务，未碰 VPS，未调用任何 Muse 接口。
> 约定：行号为 HEAD 当时的行号；写“未确认”的地方是代码里找不到依据的，不要当结论用。

---

## 0. 结论先说

**能做，但不是“加一个字段”那么简单。** 需要补三块现在完全没有的东西：

1. **身份识别**：现在只有一个全局 API Key（`config.py`），`ChatRequest` 没有 `user` 字段，`user` 传进来会被 Pydantic 静默丢弃；也没有 `key_id` 这个概念。
2. **会话对照表**：`(key_id, user) → thread URL + account_id` 的持久化存储，现在不存在；代码里没有任何地方读取或保存 thread URL。
3. **账号亲和性 + 每 user 串行**：现在每次请求都可能换账号（且失败重试会强制换账号），也没有每 user 的锁；同一个 user 的两次并发调用会各开各的会话。

最难、最可能出问题的一步：**在已有会话 URL 上发消息后，确认消息真的落进了那个会话**（不是日志上“打开了”，而是账号里那个 thread 真的多了一轮对话）。www222fff 分支就是卡在这里——日志显示 reopening，实际消息落进了新会话。我们的实现必须用“读账号里那个 thread 的内容”来验收，不能信日志。

---

## 1. 聊天请求流程

### 1.1 入口：`POST /v1/chat/completions` —— `app.py:1629-1770`（`chat_completions`）

依次经过：

| 步骤 | 位置 | 做了什么 |
|---|---|---|
| 鉴权 | `auth()`，`app.py:324-336` | 只校验 `Authorization: Bearer <CFG.api_key>`，单全局 Key；不返回任何调用方身份 |
| 拼 prompt | `build_chat_prompt(req.messages)`，`app.py:807-869` | 把全量 `messages` 历史拼成**一段**纯文本 prompt（见 1.3）；单轮无 system 时直接发原文 |
| 工具协议（可选） | `build_tools_prompt`，`app.py:887` | 默认关闭（`CFG.tool_protocol` 默认 `0`，`config.py`） |
| 选账号 | `store.pick_account(rotate=True)`，`app.py:约1655`，`store.py:171-207` | 见第 3 节；**发生在任何锁之外**，只记 `last_used`/`use_count`，不预留 |
| 流式 / 非流式分叉 | `app.py:1681` / `app.py:1743` | 两路都汇入 `safe_chat_stream`；非流式包一层 `asyncio.to_thread` |
| 执行 | `safe_chat_stream(cookies, prompt, expires, timeout, account_id)`，`app.py:396-510` | 见 1.2 |
| 返回 | `app.py:约1760-1770` | 固定 `usage` 全 0；`tool_calls` 时 `finish_reason="tool_calls"`，否则 `"stop"` |

`POST /v1/responses`（`app.py:1805-1890`）走同一条路：`build_chat_prompt(_responses_messages(req))` → `pick_account(rotate=True)` → `safe_chat_stream`。固定会话若只改 chat/completions，responses 会继续每次新开。

### 1.2 `safe_chat_stream`（`app.py:396-510`）

在 worker 线程里：

1. `GEN_LOCK.acquire(timeout=timeout)`（`app.py:411`）——全局并发槽位，见第 6 节。
2. 第 1 次尝试：`ACCOUNT_POOL.acquire(account_id=cur_id, timeout=timeout)`（`app.py:436`）——按账号加锁。
3. `_bind_account_proxy(...)` + `_renew_and_persist(cur_id, ...)`（`app.py:约443-444`）——绑定该账号代理快照、HTTP 续签 cookie。
4. `engine.start()` —— Chromium 未启动时才启动（见第 2 节）。
5. `engine.chat_stream(cur_cookies, prompt, cur_exp, timeout, account_id=cur_id, stop_event=...)`（`app.py:450`）——真正发消息、流式回吐。
6. 成功：`store.mark(cur_id, True, "")` + `_sync_cookies(cur_id)`；`finally` 里 `engine.close_current_page()`（`app.py:485`）+ 释放账号锁 + `GEN_LOCK.release()`。
7. 失败重试（第 2 次尝试）：`ACCOUNT_POOL.acquire(rotate=True, force_rotate=True, timeout=timeout)`（`app.py:423`）——**强制换一个账号**重试（见第 3.3 节的风险）。异常分支里还会 `engine.reset_thread()`（`app.py:472`，无参 = `for_chat=False`，见第 5 节）。

### 1.3 新会话是在哪里创建的

**`engine.py:845` 和 `engine.py:768` 两处，都导航到 `https://muse.ai/thread/new`。**

- `MuseEngine.ensure_page()`（`engine.py:795-864`）：每次调用（无可用热页时）先 `_close_page()` 再 `_open_page()`，然后 `page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})`（`engine.py:845`），等 `_wait_ws_ready` 通过后才返回。
- `MuseEngine.reset_thread(for_chat=False)`（`engine.py:741-786`）：媒体路径用；当 `window.location.pathname !== '/thread/new'` 或已有气泡时，同样导航到 `/thread/new`（`engine.py:768`）。

对话路径实际调用的是 `reset_thread(for_chat=True)`（`engine.py:1564`），它的逻辑见第 5.1 节：**多数情况下并不导航**，而是复用当前热页——但因为 `safe_chat_stream` 的 `finally` 每轮都 `close_current_page()`（销毁整个 BrowserContext，`engine.py:465-498`），下一轮调用时热页已不存在，`ensure_page` 必然重新打开并导航到 `/thread/new`。所以“每次新开会话”是这两个行为叠加的结果：**每轮销毁上下文 + 新上下文固定落在 `/thread/new`**。

注意：`ensure_page` 开新页之前还会做一次 HTTP 预续签（`renew_session_http`，`engine.py:550-663`；两次续签间隔 >600 秒才做），其 `Referer` 是 `https://muse.ai/thread/new`（`engine.py:561`），与固定会话无关。

### 1.4 消息怎么发、怎么等回复完成

`MuseEngine.chat_stream()`（`engine.py:1558-1625`）：

1. `self.ensure_page(cookies, expires, account_id=account_id)`（`engine.py:1563`）
2. `self.reset_thread(for_chat=True)`（`engine.py:1564`）
3. `base_agent, base_text, _ = self._poll_chat()`（`engine.py:1565`）——记下发送前最后一个助手气泡的数量与文本，作为基线。
4. `self._send(prompt)`（`engine.py:1566`）
5. 首字等待（最长 40 秒，60ms 采样）：`_poll_chat()` 返回的文本与基线不同（数量增加或文本变化）时，算出增量 `delta` 并 `yield`。
6. 持续捕获增量：文本不变且 Stop 按钮消失、连续 3 次（约 0.18s）无新文本 → 正常结束；Stop 按钮仍在时允许等到连续 80 次（约 5s）无新文本才结束；总超时 `timeout`（默认 300s，`config.py` 的 `chat_timeout`）。

`_send()`（`engine.py:1124-1199`）：

- 找页面上唯一的 `textarea`，滚动到视口中央、真实点击获得焦点；
- 用 React 18 原型 setter 写值并派发 `input`/`change` 事件（长 prompt 也不逐字注入）；短 prompt 写不进去时兜底 `Input.insertText`；
- 3 秒内循环点发送按钮（`_click_send` → `_CLICK_SEND_JS`，`engine.py:1117-1122`）；点不上则兜底 Ctrl+Enter / Enter。

`_poll_chat()`（`engine.py:1548-1557`）读 `_POLL_CHAT_JS`（`engine.py:1511-1528`）：先滚到聊天底部（虚拟列表只渲染可见节点，不滚到底新消息不在 DOM 里），取 `main` / `[class*="chat-scroll"]` 作用域内最后一个**非空助手气泡**（`hatch-agent-bubble-bg`）的文本、数量，以及 Stop 按钮是否存在。

`chat()`（`engine.py:1627-1635`）只是把 `chat_stream` 的 chunk 拼成整串返回；`app.py` 的聊天入口不直接用它（只在管理端测试等处使用）。

---

## 2. 会话 URL 在哪里能拿到？浏览器是怎么驱动的？

### 2.1 驱动方式：原生 CDP，不是 Playwright / Selenium

- `cdp.py:17-70`：`CDP` 类，基于 `websocket-client` + `requests` 的极简客户端。`send(method, params)` 发 CDP 命令并按 id 等回包；`js(expr)` 是 `Runtime.evaluate` 的封装；`pump(seconds)` 持续读事件。`http_json()`（`cdp.py:72-73`）读 `http://127.0.0.1:<cdp_port>/json/version` 拿 `webSocketDebuggerUrl`。
- `MuseEngine.start()`（`engine.py:364-417`）：起一个 headless Chromium（`--headless=new --no-sandbox --remote-debugging-port=<cdp_port，默认 19210，config.py> --user-data-dir=<profile_dir> about:blank`），先尝试复用已有健康 CDP，否则拉起进程并轮询等就绪。**整个服务共用一个 Chromium 进程**。
- 每个账号一个隔离的 `BrowserContext`：`_open_page()`（`engine.py:499-550`）经浏览器级 CDP 发 `Target.createBrowserContext`（有代理时挂一个本地 `HTTPProxyRelay` 做出口），再 `Target.createTarget` 建 `about:blank` 页，拼出 `ws://127.0.0.1:<cdp_port>/devtools/page/<targetId>` 连上。`page` 是**线程局部**的（`_ensure_local()`，`engine.py:329-360`），`current_acc_id` / `current_context_id` 同样是线程局部的。
- `_close_page()`（`engine.py:465-498`）：关页面 WebSocket、`Target.disposeBrowserContext` 销毁上下文、关代理 relay、删临时下载目录、清线程局部状态。

### 2.2 Thread URL 现在没有任何地方保存

代码里唯一读 URL 的地方是 `_wait_attachment()`（`engine.py:1323`，媒体路径）：`location.pathname` / `location.href` 只用来判断“空闲页面 + `/thread/new`”做媒体的防卡死检测（`engine.py:1400-1403`），**读完即弃，不保存、不返回**。

所以拿 thread URL 的办法只能是新加的：`page.js("location.href")`（即 `Runtime.evaluate`，`cdp.py:44-53`）。CDP 能力本身没问题，问题在**时机**（见第 5.4 节，未确认）。

### 2.3 页面生命周期与固定会话的关系

- `ensure_page` 的热页复用条件（`engine.py:800-810`）：已有 `page`、代理配置相同、`current_acc_id` 相同，且页面里有 `textarea`、`_wait_ws_ready` 通过 → 直接复用。否则销毁重建。
- 每次聊天调用结束，`safe_chat_stream` 的 `finally` 一定 `close_current_page()`。因此**跨调用不存在“热页”**，固定会话不能靠“不关页面”实现，必须靠“下次调用按保存的 URL 重新导航”。

---

## 3. 账号选择与轮换

### 3.1 一次请求怎么选账号

`chat_completions` 里 `store.pick_account(rotate=True)`（`store.py:171-207`）：

- 只考虑 `enabled` 且有 `cookies` 的账号；
- 若传了 `preferred_id` 且 `rotate=False` 且 `force_rotate=False`，才固定用那个账号（`store.py:180-189`）——**聊天入口从不传 `preferred_id`，所以这条分支现在是死代码（对聊天而言）**；
- 否则取 `ok is not False` 的健康账号，按 `(last_used, use_count)` 排序取最少用的一个，并写回 `last_used`/`use_count`（`store.py:194-200`）。`_sticky_id` / `_sticky_count` 只被赋值（`store.py:181-182, 192-193`），**代码里没有任何地方读取它们**；文档字符串说的“每满 15 次轮转”没有对应的计数逻辑，实际轮换就是“每次取最少用的”。
- `AccountPoolManager.acquire()`（`app.py:136-192`）在选定之后按账号加锁：`busy_accounts` 集合，同一账号同一时间只能被一个任务占用；无空闲账号时等条件变量，最长 `timeout`（默认 60 秒）。

也就是说：**现在没有“某个调用方固定用某个账号”的机制**，每次调用都可能落到不同账号。

### 3.2 每个账号的浏览器状态

- 账号本身在 `store.py` 里只是一条 JSON 记录（`id / label / cookies / cookies_exp / expires_at / enabled / ok / note / use_count ...`，见 `data/accounts.example.json`），**不含任何页面或会话状态**。
- 浏览器状态是临时的、按调用建立的：每次调用新建一个 `BrowserContext`（`_open_page`），注入该账号的 cookie（`_apply_cookies`，`engine.py:663-692`），导航到 `/thread/new`，调用结束即销毁。账号之间靠独立的 BrowserContext 隔离 cookie。
- `engine.current_acc_id` 只是线程局部的一个标记，用于 `ensure_page` 判断热页能不能复用。

### 3.3 把 A 账号的会话 URL 拿到 B 账号的浏览器里打开，会发生什么

**代码里没有任何分支处理这种情况，未确认。** 可以确定的是：

- 没有任何代码校验“这个 thread URL 属于当前账号”；如果直接把 URL 交给 `Page.navigate`，结果完全取决于 muse.ai 网页端的行为（未确认：可能是 404、跳回首页、或提示无权访问）。
- 更大的风险在重试路径：`safe_chat_stream` 第 2 次尝试会 `ACCOUNT_POOL.acquire(rotate=True, force_rotate=True)`（`app.py:423`）**强制换账号**。如果某次调用因为超时/异常走到重试，固定会话会被带到另一个账号的浏览器里——要么打不开，要么在错误的账号下新建会话。**固定会话必须关掉这条重试换号逻辑（至少对已 pin 的调用），或在重试时仍回到原账号。**
- 因此对照表里必须同时存 `account_id`，选账号时用 `preferred_id + rotate=False` 走 `pick_account` 的固定分支，并配合 `AccountPoolManager.acquire(account_id=...)` 锁住同一账号。

---

## 4. 持久化：现有存储机制与对照表的落点

### 4.1 现有机制

- `store.py:76-97`：`_read` / `_write`，JSON 落盘，`_write` 用 `tempfile.mkstemp` + `os.replace` 做原子写；模块级全局 `_LOCK`（`store.py:18`）串行化所有写。
- `Store`（`store.py:99-324`）：启动时把 `accounts.json`、`tasks.json` 读进内存，之后每次变更都全量写回。文件位置由 `config.py` 决定：`data_dir = <base_dir>/data`（`config.py:144-146`），`accounts_file = data/accounts.json`（`config.py:152-154`），`tasks_file = data/tasks.json`（`config.py:156-158`）。
- 另一个先例：`generation_timing.py:15-31` 的 `QueuePolicy`，同样是 `data/generation-queue.json` 单独一个 JSON 文件、原子写（`os.replace`）。`app.py:190` 实例化。
- 克隆里的 `data/` 目录现在只有 `accounts.example.json` 和 `media/`；`accounts.json` / `tasks.json` 是运行期生成的文件（不在 git 里）。

### 4.2 对照表放哪里最合适

**新文件 `data/pinned-threads.json`，单独一个小存储类（或复用 `Store` 的 `_read`/`_write` + `_LOCK` 模式），不要塞进 `accounts.json`。** 理由：

- `accounts.json` 在每次 `pick_account` / `mark` / `touch_keepalive` 时都会全量重写，把高频变更的对照表放进去会放大写冲突和损坏面；且账号文件会被管理端原样展示，thread URL 不宜混在里面。
- 一条记录建议的字段：`key_id`、`user`、`account_id`、`thread_url`、`updated_at`（另可加 `last_seen_bubble_count` 供排障）。
- `key_id` 现在不存在：`auth()` 只认一个全局 `CFG.api_key`（`config.py` 的 `MUSE2API_KEY`）。若保持单 Key，`key_id` 可取固定值（如 `"default"` 或 `sha256(api_key)` 前几位）；若将来支持多 Key，再扩展。
- `user` 字段现在不存在：`ChatRequest`（`app.py:555-573`）没有声明 `user`，其文档字符串明确写了“不认识字段一律忽略（Pydantic 默认行为）”。**必须在 `ChatRequest` 上加 `user: str | None = None`，否则调用方传什么都会被静默丢弃。**

---

## 5. 关键风险点：在已有会话里发消息能走通吗？

### 5.1 有利的一面：聊天路径本身不依赖“新会话页面”

逐项核对过，聊天链路里没有“必须是空会话”的硬性检查：

- `_send()`（`engine.py:1124-1199`）：只依赖页面上有 `textarea` 和发送按钮。发送按钮的查找（`_SEND_BUTTON_JS`，`engine.py:约1075-1110`）从 `textarea` 的祖先元素向上找，且**明确排除历史气泡里的按钮**（注释写了“a history preview may itself contain 'send'”），说明作者已经考虑过“页面上有历史内容”的情况。
- `_wait_ws_ready()`（`engine.py:718-741`）：只等 `readyState === 'complete'` + 有 `textarea` + hydration 完成 + 无 `Connecting...` + 输入框未禁用，不看 URL、不看气泡数。
- `_poll_chat()` / `_POLL_CHAT_JS`（`engine.py:1511-1528, 1548-1557`）：以发送前的最后助手气泡为基线，只认“新增/变化的最后助手气泡”，在已有会话里同样成立（前提是新回复确实产生一个新的、或内容不同的最后气泡，见 5.3）。
- `reset_thread(for_chat=True)`（`engine.py:741-786`）：**不检查 URL**，只看气泡数、附件、Stop 按钮、`Still sending` / `Connecting...`。只要气泡数 < 24、无附件、无 Stop、无卡死，它直接复用当前页——这说明现有自动化在设计上就容忍“在已有对话里继续发”。

### 5.2 最大的结构性障碍：`reset_thread` 的 24 气泡上限

`engine.py:763`：`for_chat` 时 `return bubbleCount >= 24`。一个固定会话每轮对话至少新增 1 个用户气泡 + 1 个助手气泡（`div[class*="hatch-chat-groupable-bubble"]` 计数），**大约 12 轮之后就会触发导航到 `/thread/new`**，固定会话在毫无报错的情况下静默失效。固定会话必须改掉这一条（例如：已 pin 的调用跳过气泡数判断，或导航目标改成保存的 thread URL 而不是 `/thread/new`）。

连带的两处“零气泡”等待也要改：

- `reset_thread` 导航到 `/thread/new` 后的就绪等待要求 `textarea` 存在 **且气泡数为 0**（`engine.py:约772-780`）。若导航目标改成已有会话 URL，这条等待永远不成立，必须换成“有 textarea + 已加载出历史气泡（或确认无加载中状态）”。
- 媒体路径 `generate()` 用 `reset_thread(for_chat=False)`（`engine.py:1915`），其规则是“不在 `/thread/new` 或有气泡就导航走”——生图/生视频现在固定每次新开会话，不在本次固定会话的范围内；但要注意别把聊天用的改动误伤到媒体路径。

### 5.3 回复检测的边界情况（需实测确认）

- `chat_stream` 的首字等待条件是 `not cur or (cnt <= base_agent and cur == base_text)` 时继续等（`engine.py:约1578-1590`）。如果某轮回复与上一轮的最后助手气泡**文本完全相同且气泡数没有增加**，首字检测会一直等到 40 秒超时。正常情况下新回复会新增气泡（`cnt` 增加即放行），但“模型回了完全相同的一句话”是不是新增气泡、还是复用上一个气泡，**代码里看不出来，未确认**。
- 长会话的虚拟列表：`_POLL_CHAT_JS` 每次先滚到底再取最后气泡，逻辑上能处理长会话；但 `reset_thread` 的气泡计数用的是整个 `main` 作用域的 `querySelectorAll`，不经过滚动，长会话里未渲染的节点可能不被计数——24 这个阈值在长会话里的实际触发点**未确认**。

### 5.4 Thread URL 的捕获时机（未确认）

代码里没有任何地方读取或断言“发送后 URL 从 `/thread/new` 变成 `/thread/<id>`”。合理推测是：首条消息发出后，muse.ai 把地址栏改写为新 thread 的 URL。但具体是 `_send()` 返回时、首字回复到达时、还是整轮回复结束时，**代码里没有依据，未确认**。实现时要在 `chat_stream` 里在多个时间点采样 `location.href`（发送后、首字后、结束时），以“首次出现 `/thread/<id>` 形态的 URL”为准并保存；验收必须直接读账号里那个 thread 的内容，不能只看 URL 变了。

### 5.5 其他风险

- 会话被用户在网页端手动删除 / 账号被登出 / cookie 过期：下次导航到保存的 URL 会失败或跳走。代码里没有这类分支，需要加“打开失败 → 丢弃旧映射、新开会话并重新保存”的兜底。
- `ensure_page` 的 HTTP 预续签（`>600 秒`才做一次）与固定会话无冲突；`_renew_and_persist` 只换 cookie，不动 thread。
- 并发见第 6 节；重试换号见第 3.3 节。

---

## 6. 并发：现在有什么锁？同一个 user 两次调用同时跑会发生什么？

现有三层，**没有一层是按 user 分的**：

1. `GEN_LOCK`（`app.py:95-134, 191`）：全局信号量，上限 `CFG.max_concurrency`（默认 8，`config.py` 的 `MUSE2API_CONCURRENCY`）。限制同时进行的生成任务总数，不区分账号也不区分 user。
2. `AccountPoolManager`（`app.py:136-192`）：`busy_accounts` 集合保证**同一账号同一时间只有一个任务**；无空闲账号时等待，最长 `timeout`（默认 60 秒）。不同账号可并行。
3. `Store._LOCK`（`store.py:18`）：只保护 `accounts.json` / `tasks.json` 的内存读写与落盘。

同一个 user 的两次调用同时跑，现在会发生：

- 两次 `chat_completions` 各自 `pick_account(rotate=True)`——**选号发生在任何锁之外**，两次可能选中同一账号（`pick_account` 只写 `last_used`/`use_count`，不预留），也可能选中不同账号；
- 各自进 `safe_chat_stream` 的 worker 线程，各自拿 `GEN_LOCK` 槽位；若选中同一账号，第二个会在 `ACCOUNT_POOL.acquire(account_id=...)` 上等第一个完成（最长等 `timeout`），然后各自开**独立的 BrowserContext**、各自导航到 `/thread/new`、各自发消息——即使用户相同，也会产生**两个不同的新会话**，且后完成的那个会把先完成的回复覆盖进历史拼串里（因为 prompt 是调用时拼好的全量历史快照）。
- 若固定会话已实现但不加每 user 锁：两次调用会同时打开**同一个 thread URL**（两个独立的 BrowserContext），同时往同一个会话里发消息——muse.ai 网页端如何处理两个并发写入（互相覆盖、串成一轮、或其中一个失败），**代码里没有依据，未确认**。必须加一个按 `(key_id, user)` 的锁/队列，让同一 user 的调用串行；锁的持有时间应覆盖“打开会话 → 发消息 → 等回复完成 → 保存 URL”全程。

---

## 7. 改动规模估计

| 文件 | 改动 | 估计行数 |
|---|---|---|
| `config.py` | 可选：加 `MUSE2API_PIN_THREADS` 之类的开关（默认开或关待定） | ~5 |
| `store.py` 或新增 `threads.py` | 对照表存储类：`get/save/delete`，复用 `_read`/`_write` 原子写 + 锁；或直接扩展 `Store` | 60-90 |
| `app.py` | `ChatRequest` 加 `user` 字段；`chat_completions` 里解析 pin、查对照表、按 `preferred_id + rotate=False` 选账号；新增每 `(key_id, user)` 的锁；`safe_chat_stream` 接 pin 参数、重试时不强制换号、回传 thread URL；失败时清理失效映射 | 80-120 |
| `engine.py` | `chat_stream` / `ensure_page` 接受 `thread_url` 参数（导航目标从硬编码 `/thread/new` 改为可注入）；`reset_thread` 加已 pin 分支（跳过 24 气泡规则、就绪等待不再要求零气泡）；在发送后/首字后/结束时采样 `location.href` 并返回 | 50-70 |
| 测试 | 至少覆盖：对照表读写、user 字段透传、pin 命中时不换号、24 气泡不再跳走、失效映射兜底 | 另计 |

**合计生产代码约 200-280 行。** 不含实测与联调。

**最难、最可能出问题的步骤（按风险排序）：**

1. **确认消息真的落进 pin 住的会话**——www222fff 分支的前车之鉴：日志/代码层面的“reopening”不可信，必须以“打开账号里那个 thread URL、数气泡、核对内容”作为唯一验收标准；且要覆盖“首轮新建保存 URL → 第二轮打开旧 URL → 第三轮及 12 轮后（24 气泡）仍在同一会话”三种情况。
2. **Thread URL 的捕获时机**（第 5.4 节）：发完消息后 URL 何时从 `/thread/new` 变成 `/thread/<id>`，代码里无依据，只能实测；采早了会存下 `/thread/new`，采晚了会漏存。
3. **`reset_thread` 的 24 气泡规则与零气泡就绪等待**（第 5.2 节，`engine.py:763` 与 `engine.py:775`）：改不好会出现“固定会话用 12 轮就静默失效”或“打开旧会话后永远等不到就绪”。
4. **重试换号与账号亲和性**（第 3.3 节）：`force_rotate=True` 的重试必须对已 pin 调用禁用，否则一次超时就会把会话带到另一个账号。
5. **同一 user 的并发串行**（第 6 节）：锁的粒度与持有时间；锁死了会堵住整个 user 的后续调用，锁不住会产生两个上下文同时写同一会话。

---

## 8. HEAD（19db32a）与发布提交 d169e6a 的差别

核对结果：

```
$ git diff d169e6a..HEAD --stat
 admin.html                 | 26 ++++++-------
 tests/test_admin_layout.py | 69 ++++++++++++++++++++++++++++++---
 2 files changed, 84 insertions(+), 11 deletions(-)

$ git diff d169e6a..HEAD -- app.py engine.py store.py config.py cdp.py proxy.py | wc -l
0
```

- HEAD `19db32a`（2026-11-16 01:16 UTC）比发布提交 `d169e6a`（2026-11-16 00:39 UTC）新 37 分钟，差别**只有管理端页面文案/布局**（`admin.html`）和对应的布局测试（`tests/test_admin_layout.py`）。
- `app.py`、`engine.py`、`store.py`、`config.py`、`cdp.py`、`proxy.py` 在两个提交之间**零差别**。
- **设计以哪个提交为准都可以；建议以 HEAD（19db32a）为准**，因为它就是当前克隆的状态，且与发布的 1.6.11 在功能代码上完全一致。

---

## 附：关键位置速查

- 入口：`app.py:1629` `chat_completions`；`app.py:1805` `responses_api`
- 请求模型：`app.py:555` `ChatRequest`（无 `user` 字段）；`app.py:807` `build_chat_prompt`
- 鉴权：`app.py:324` `auth`（单全局 Key）
- 执行：`app.py:396` `safe_chat_stream`；`app.py:411` `GEN_LOCK`；`app.py:423` 重试强制换号；`app.py:436` 按账号加锁
- 锁：`app.py:95` `ConcurrencyLock`；`app.py:136` `AccountPoolManager`
- 选号：`store.py:171` `pick_account`（`preferred_id` 固定分支 `store.py:180-189`）
- 存储：`store.py:76` `_read`/`_write`；`config.py:144-158` 文件路径；`generation_timing.py:15` `QueuePolicy`
- 引擎：`engine.py:313` `MuseEngine`；`engine.py:364` `start`；`engine.py:499` `_open_page`；`engine.py:465` `_close_page`；`engine.py:795` `ensure_page`（`engine.py:845` 导航 `/thread/new`）；`engine.py:741` `reset_thread`（`engine.py:768` 导航 `/thread/new`，`engine.py:763` 24 气泡规则，`engine.py:775` 零气泡就绪等待）；`engine.py:1124` `_send`；`engine.py:1511` `_POLL_CHAT_JS`；`engine.py:1558` `chat_stream`
- 驱动：`cdp.py:17` `CDP` 类
