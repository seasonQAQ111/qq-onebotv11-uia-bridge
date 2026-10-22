# QQonebotv11桥接系统UIA

使用控件方式自动接收发送QQ消息。

不注入、不 Hook、不修改 QQ 进程：通过 Windows UI Automation 驱动真实的 QQ NT
桌面客户端，像人一样读取消息列表、切换会话、把回复打进输入框，再以 OneBot v11
反向 WebSocket 客户端的形式接入 AstrBot。

```
QQ NT 桌面客户端  <--UIA-->  本桥接  <--OneBot v11 反向 WS-->  AstrBot :11229
```

## 特性

- **控件驱动**：只用 UI Automation 读写 QQ 窗口，不碰网络协议、不做进程注入。
- **多会话寻址**：每条出站消息都会先切到目标会话，并在输入前**两次校验聊天头部**；
  校验不通过就判为发送失败，绝不把内容落进别的会话。
- **会话发现**：默认 `watch` 模式读会话列表（预览 + 未读红点）找出有新消息的会话，
  只切进这些会话，读完再还原用户原本正在看的那一个；另有 `active` / `cycle` 模式。
- **惰性巡检**：Chromium 在窗口被最小化或完全遮挡时停止重绘，会话行会“冻住”，
  变化检测就会漏消息。`force_read_seconds` 配合 `force_read_idle_seconds`
  在人离开键盘足够久之后强制进入每个会话读一遍，兜住这种情况。
- **图片**：AstrBot 发来的 `base64://` 图片段先落到 `logs\uia-outgoing`，
  再通过剪贴板粘贴进 QQ 编辑器，发送结果同样做校验。
- **闭麦闸门**：有人让机器人“闭嘴”时，该会话被关麦 `mute_seconds`。
  关闭的是**嘴不是耳朵**：消息照常推给 AstrBot，记忆和上下文不断，
  但桥接拒绝往回发，提示词和 @ 都无法在计时结束前把它唤醒。
  群聊默认要求指名（或 @）才会上闸，避免群友互怼误伤。
- **保活**：`keep_awake` 持有 `ES_SYSTEM_REQUIRED`，防止系统睡眠导致机器人停摆。
- **单实例锁**：桥接运行期间持有按配置名命名的 Windows 互斥体，同一个配置再启动第二份会被直接拒绝（退出码 3）。
  看门狗也用这把锁判断存活，因此即使那份桥接的命令行读不到（以管理员或其他用户启动），也不会被误判成“没在跑”而再拉一份起来。
  两个桥接同时连一个 AstrBot 时，AstrBot 不去重，每条消息会推两遍。
- **看门狗**：`watch-bridge.ps1` 常驻后台，进程掉了就拉起来；主动停止会写
  一个 hold 文件，看门狗看到后完全放手，不会把刚停掉的桥接又拉回来。

## 环境要求

- Windows 10 / 11
- QQ NT 桌面版（已登录，窗口可最小化但必须存在）
- Python 3.10+
- AstrBot 或其他 OneBot v11 实现，开启 aiocqhttp 反向 WS（本仓库默认 `ws://127.0.0.1:11229/ws`）

## 安装

```powershell
git clone https://github.com/seasonQAQ111/qq-onebotv11-uia-bridge.git
cd qq-onebot-uia-bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item scripts\bridge-config.example.json scripts\bridge-config.json
```

依赖只有三个：`uiautomation`、`pyperclip`、`websockets`，其余均为标准库。

## 配置

编辑 `scripts\bridge-config.json`，把示例里的账号和会话换成自己的：

| 字段 | 说明 |
| --- | --- |
| `ws_url` | AstrBot 的反向 WS 地址 |
| `self_id` / `self_nickname` | 被驱动的 QQ 号及其昵称（用于 @ 识别） |
| `chats[]` | 要读写的每个会话：`kind` 为 `private`/`group`，`id` 是 QQ 号，`name` 是会话列表里显示的名字 |
| `members` / `chats[].members` | QQ 号 -> 昵称，作为昵称解析的回退表 |
| `read_mode` | `watch`（默认）/ `active` / `cycle` |
| `window_account` | 同机登录两个 QQ 时，用左侧栏昵称指定要驱动的那个窗口 |
| `force_read_seconds` / `force_read_idle_seconds` | 惰性巡检周期，以及只有在人离开键盘多久之后才执行 |
| `mute_seconds` / `mute_keywords` / `mute_trigger_names` | 闭麦闸门 |
| `keep_awake` | 是否阻止系统睡眠 |
| `state_path` | 已汇报记账文件，避免重启后重放旧消息 |

`bridge-config.example.json` 里的 `_readme` 逐项说明了全部字段。

## 运行

```powershell
# 语法自检：对照当前 QQ 会话列表检查配置，然后退出
powershell -File scripts\start-bridge.ps1 -SelfCheck

# 只读模式：读消息、不发送，先观察一轮
powershell -File scripts\start-bridge.ps1 -DryRun

# 正式启动
powershell -File scripts\start-bridge.ps1
```

重复启动会被拒绝（退出码 3）：一个配置同时只能有一份桥接在跑，先 `stop-bridge.ps1` 再启动。

日志写在 `logs\uia-bridge.log`，Python 解释器默认取仓库根目录的 `.venv`，
也可以用 `-Python` 指定。

后台保活与主动停止：

```powershell
# 隐藏启动看门狗
wscript.exe scripts\watch-bridge-detached.vbs

# 主动停止（看门狗不会把它拉回来，直到再次 start）
powershell -File scripts\stop-bridge.ps1
```

## 已知限制

- QQ 必须处于登录状态；未登录时桥接会直接退出，看门狗此时不会硬拉。
- 同一套 AstrBot 请不要同时挂 NapCat / Lagrange / SnowLuma：两个 OneBot
  客户端会对同一条消息各回一次。
- 依赖 QQ 客户端的 UIA 树结构（`消息列表`、`会话列表`、`ProseMirror` 编辑器等）。
  QQ 大版本改版后可能需要重新适配控件名称。
- 窗口被最小化或完全遮挡时靠 `force_read_seconds` 兜底，兜底间隔内会有延迟。

## 免责声明

本项目仅用于个人自动化与研究。请自行确认使用方式符合腾讯软件许可协议及所在地
法律法规；因滥用导致的账号风险由使用者自负。

## License

MIT，见 [LICENSE](LICENSE)。