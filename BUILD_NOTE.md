# 固定会话改版 — 构建说明

> 基于主项目 czg86389-hub/muse2api v1.6.11（HEAD 19db32a），按 PIN_DESIGN.md 实现。
> 本地改版，未推 GitHub；MIT LICENSE 原样保留。

## 改了哪些文件

| 文件 | 改动 |
|---|---|
| `config.py` | 新增开关 `MUSE2API_PIN_THREADS`（默认开，`0` 关闭）；新增 `pinned_threads_file`（`data/pinned-threads.json`） |
| `threads.py`（新增，108 行） | URL 识别（`thread_id_of` / `normalize_thread_url` / `is_thread_url`，`/thread/new` 不算会话）+ 对照表存储类 `PinnedThreads`（`get/save/delete/all`，原子写 + 锁，复用 store.py 的落盘模式）。记录字段：`key_id`、`user`、`account_id`、`thread_url`、`updated_at`。单全局 Key 时 `key_id` 固定为 `"default"`。`save` 拒绝非会话 URL。 |
| `engine.py` | 新增异常 `PinnedThreadGoneError`（`MuseGenerationError` 子类）；线程局部 `last_thread_url` + `_sample_thread_url()`；`ensure_page(..., thread_url=None)`：导航目标可注入、热页必须匹配目标会话、落点不是目标会话时抛 `PinnedThreadGoneError`；`reset_thread(..., pinned=False, thread_url=None)`：固定分支跳过 24 气泡规则、导航目标改成保存的 URL、就绪等待不再要求气泡数为 0；`chat_stream(..., thread_url=None)`：发送后、首字后、结束时三处采样 `location.href`。媒体路径（`reset_thread(for_chat=False)`、`generate()`）未动。 |
| `app.py` | `ChatRequest` 和 `ResponsesRequest` 加 `user: str \| None = None`（原来 Pydantic 会静默丢弃）；新增 `PINNED_THREADS`、`PIN_LOCKS`（每 `(key_id, user)` 一把锁）、`_resolve_pin` / `_pick_account_for_chat` / `_save_pin_thread`；`safe_chat_stream(..., pin=None)`：每 user 串行（锁覆盖打开会话到保存 URL 全程）、重试不换号、成功后保存 URL、固定会话打不开时丢弃旧映射改开新会话重试；`/v1/chat/completions` 与 `/v1/responses`（流式与非流式）都已接上。 |
| `.gitignore` | 加 `data/pinned-threads.json`（运行时文件，不进 git；`.env`、账号 cookie 本来就已排除） |
| `tests/test_pinned_threads.py`（新增，352 行） | 16 个离线单元测试（见下） |

生产代码改动量：269 行新增（含注释与文档字符串）+ 新文件 threads.py 108 行，与设计估计的 200–280 行生产代码基本一致。

## 关键行为（调用方视角）

- 请求带 `user`（例如 `"thea"`）即启用固定：首轮新开会话，成功后把 `(default, thea) -> thread URL + 账号` 写进 `data/pinned-threads.json`；之后每次调用都打开同一个 URL、同一个账号。
- 不带 `user` 时行为与原来完全一致（每次新开会话、正常轮换账号）。
- 开关 `MUSE2API_PIN_THREADS=0` 可全局关闭固定，回到原来行为。
- 同一个 user 的并发调用会串行（后一个等前一个完成，最长等 `timeout`，默认 300 秒），不会两个上下文同时往同一会话里写。
- 固定会话打不开（被删 / 无权访问 / 被重定向走）时：丢弃旧映射，本次调用改开新会话并重新保存；页面本身没加载出来（网络抖动）时不丢映射，按原 URL 再试一次。
- 映射里的账号被删/禁用/无 cookie/被标记不可用时：丢弃映射，当作全新调用方处理。
- 只存真正的 `/thread/<id>`，`/thread/new` 永远不存。

## 部署试用版的建议（只做参考，部署动作不在本次任务内）

- **不要动主部署**（`/opt/muse2api`，端口 18610，域名 muse.aurorlab.com）。
- 单独一份代码、单独一个容器：建议从本目录（`~/workspace/muse2api-main`）打包传到 VPS（例如经 1Panel 传一个 tar 包，或先推到私有仓库再拉），放到 `/opt/muse2api-pin` 这类单独目录。
- **端口不要用 18610 / 18611**：建议 `MUSE2API_PORT=18612`（容器内仍可监听 18610，宿主机映射 18612；或直接在容器里改端口，二选一）。CDP 端口在容器网络命名空间内，默认 19210 即可，不与主部署冲突。
- **单独数据目录**：用单独的 `MUSE2API_HOME`（或单独的数据卷），不要与主部署的 `data/` 混用；对照表 `data/pinned-threads.json` 会在试用版自己的数据目录里生成。
- **新的独立 API Key**：`MUSE2API_KEY` 重新生成一串（不要复用主部署的 Key，便于区分调用方和随时吊销）。
- **账号导入**：沿用分支试装时验证过的做法——服务端从 `/opt/muse2api/data/accounts.json` 经 `POST /admin/accounts` 导入同一账号（cookie 不打印、不落聊天记录）；或用浏览器扩展重新导入。主项目支持每账号代理（分支没有），导入后给账号配与主部署相同的代理（muse2api 容器内地址 `http://172.20.0.1:7890`，不要用 `127.0.0.1:7890`）。
- **其余配置沿用主部署**：Chromium 以 root 运行绕开 no-sandbox 问题的做法与主部署一致；不加 Caddy 域名（本地端口访问即可）；不要开 18612 以外的端口。

## 测试结果（本地离线，未调任何 Muse 接口、未启浏览器）

- 新增 `tests/test_pinned_threads.py`：**16 个测试全部通过**（`python -m unittest tests.test_pinned_threads`，0.7 秒）。
  覆盖：对照表读写（含拒绝存 `/thread/new`、落盘重读）、`user` 字段透传（chat + responses）、开关关闭与无 user 时不固定、命中映射时固定账号、映射账号失效时丢弃、重试不换号（两次 acquire 都锁同一账号、无 `force_rotate`）、固定会话打不开时丢弃旧映射改开新会话、每 user 串行（两路并发不重叠）、`reset_thread` 固定分支（导航到保存的 URL、不要求气泡数为 0、源码含跳过 24 气泡规则）、`ensure_page` 落点校验（落点不对抛 `PinnedThreadGoneError`、落点正确返回页面）、`_sample_thread_url` 行为、端点接线（chat 非流式与流式、`pin` 参数透传）。
- 全量现有测试对比（逐模块跑，与 `git stash` 后的干净树逐项对比）：**除新增文件通过外，其余 36 个模块的结果与改动前完全一致**，无回归。少数模块在本环境下本来就跑不起来（需要真实浏览器或另一种运行方式，例如 `test_media_thumbnails`、`test_release_integration` 在干净树上同样失败，`test_admin_layout` 等在干净树上同样报参数错误），与本次改动无关。
- 测试环境：本机临时 venv（`requirements.txt` 的依赖），`MUSE2API_HOME` 指向临时目录；未联网登录、未碰 VPS。

## 设计里“未确认”的点，只能靠 VPS 实测验证（不要只看日志）

以下 5 点在代码里找不到依据，本地测试覆盖不了，必须在试用版上实测：

1. **URL 翻转时机**：首条消息发出后，地址栏何时从 `/thread/new` 变成 `/thread/<id>`。实现里在发送后、首字后、结束时三处采样，以首次出现的 `/thread/<id>` 为准。实测要核对 `data/pinned-threads.json` 里存下的确实是 `/thread/<id>`，不是 `/thread/new`。
2. **跨账号打开 thread URL 的行为**：实现上用账号亲和性避开了（映射里存 `account_id`，只在该账号下打开），但如果 muse.ai 对“自己的 URL”仍有重定向或拒绝，`PinnedThreadGoneError` 会触发兜底。实测注意日志里是否出现“固定会话打不开”。
3. **回复与上一轮完全相同时的气泡行为**：首字检测要求新回复产生新增或内容不同的最后气泡；如果模型回了与上一轮完全相同的一句话，首字等待可能拖到 40 秒超时。实测可故意让模型重复一句话。
4. **长会话气泡计数的实际触发点**：24 气泡规则已绕过，但 `_poll_chat` 的虚拟列表在长会话里的行为未验证。验收要覆盖 12 轮以上（超过旧规则的触发点），确认仍在同一会话。
5. **双上下文并发写同一会话的结果**：实现上用每 user 锁串行化，但锁本身的有效性（例如客户端断开后锁是否释放、锁超时后是否会放行）需要一次真实并发调用验证。

**验收标准（唯一可信的）**：直接打开账号里那个 thread，看内容——首轮新建保存 URL → 第二轮打开旧 URL 发消息 → 第三轮及 12 轮后仍在同一会话、气泡数持续增长。日志上显示“打开了旧会话”不算数（www222fff 分支就是这么翻车的）。

## 部署实测修正（2026-11-17 ~20:10 CST，试用版部署时发现）
- Dockerfile 的 COPY 清单漏了 threads.py，容器因 ModuleNotFoundError: threads 反复重启；试用版 Dockerfile 已补上重建，本地 ~/workspace/muse2api-main/Dockerfile 也已补上。
- 代理 http://172.20.0.1:7890 只允许主部署网络 172.20.0.0/16 访问；试用容器须额外接入 muse2api_default 网络，否则代理超时返回 502。未改防火墙规则。
- 试用版：/opt/muse2api-pin，容器 muse2api-pin，宿主机端口 18612，独立 API Key（值不记录），账号 id 966dcac3a710（Finch）。
- 烟雾测试：call 1（user thea）“记住暗号：松柏-5820”→“收到”；call 2 “刚才的暗号是什么？”→“收到松柏-5820”。pinned-threads.json 有 1 条 thea 记录，thread https://muse.ai/thread/4c0e792f-4ef5-45e0-af9f-fbaabda21ccd。账号里实际轮次未核对（muse.ai 在浏览器里被扩展拦截），不算验证通过。
