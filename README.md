# 微信 Agent 任务插件

独立的 AstrBot 任务插件，提供 Thread/Ticket、证据审核、单次订阅、无工具模型 Worker 和后台任务工作台。
不注册微信平台、不连接 Hook HTTP 服务、不修改采集配置。

**任务页面和 Worker 只生成候选和草稿，不直接发送微信。** 连接器的普通聊天发送可单独开启，
但不会自动发送任务候选或历史草稿。页面和 Worker 需要同时安装
[微信 Hook 连接插件](https://github.com/abincn/astrbot_plugin_wechat_hook) v0.3.0 及以上。
本地 CLI 可以单独管理已有 Inbox，不需要加载 AstrBot 或连接插件。

## 安装

在独立 AstrBot 的插件管理中，通过仓库地址安装：

```text
https://github.com/abincn/astrbot_plugin_wechat_agents
```

也可 **Code → Download ZIP**，解压后将插件目录命名为 `astrbot_plugin_wechat_agents`，
放入 `data/plugins/`。`main.py` 和 `metadata.yaml` 必须直接位于该目录下。
保留 `views/` 和 `.astrbot-plugin/`。本插件无额外 Python 依赖，使用 AstrBot 自带接口。
后台需支持插件视图、`AstrBotPluginView` bridge 和 `astrbot.api.web`；接线参考 AstrBot 4.29.4 对应的已审阅 API。

从旧合并版升级时，先将旧连接插件目录移到 `plugins` 目录之外，再重新安装独立连接插件和本插件，
避免旧工作台文件残留。已有 Inbox、平台 ID、grant、任务配置与 `WGA_SUPERAGENT_*` 环境变量沿用，不迁移数据库。

## 配置任务工作区

先在 **AstrBot 后台 → 插件 → 微信 Agent 任务 → 配置** 填写：

- `platform_id`：所用微信连接平台的 ID，默认 `wechat_pc_hook`。
- `rooms`：启用群 Agent 的群 ID 列表，每项一个完整的 `群ID@chatroom`。
- “自动处理排队任务”和“自动唤醒到期跟进”：均使用布尔开关，默认关闭，不再手填 true/false。
- “视频抽帧分析”：布尔开关，默认开启；只在已配置群生效，需要 ffmpeg 和视觉模型。

这是群 Agent 的唯一启用名单，默认空列表表示不启用任何群 Agent；字符串不会被当作数组接受。
名单内应用按群文档、空工具策略，并允许在授权范围内管理任务；名单外不应用 Agent 策略，仍按 AstrBot 原生规则处理。
保存后完整重启独立实例，以一致更新页面、群策略和 Worker。
仅使用按群指令策略不需要开启任务 Worker；任务工作台另需下述数据库/授权配置。

### 图片、视频与引用

连接插件 v0.3.1 提供原生 `Image`、`Video` 与 `Reply` 消息；本插件不重复下载，也不直接调用 Hook。
在配置群内，视频可沿用旧 Group Agents 的方式，从验证后的本地视频提取约 0、3、9 秒的最多三帧，
经 AstrBot 原生图片输入交给模型；每次解码最多 12 秒、每帧最多 1.5 MB、画面宽度不超过 960 像素。
这不包含音轨识别，也不能代表整段视频的全部画面。缺少 ffmpeg、解码失败或模型不支持图片时，不能承诺理解视频。

引用视频也可从 `Reply.chain` 读取抽帧；输入引用消息不等于支持发回微信时的原生引用。
完整对照见 [消息能力表](https://github.com/abincn/astrbot_plugin_wechat_hook/blob/main/docs/MESSAGE_CAPABILITIES.md)。
本次没有运行 ffmpeg 或真实模型来做实机验证。

先启动连接插件，确认独立 Inbox 已存在。然后在 AstrBot 的 `data/wga-superagent.json` 保存：

```json
{
  "inbox_db": "wechat_hook_inbox.sqlite3",
  "grant_file": "wga-superagent-grants.json",
  "owner": "YOUR_WXID",
  "provider_id": "YOUR_PROVIDER_ID"
}
```

路径相对于这份配置文件。数据库和账户必须与所选连接平台一致；群名单和平台 ID 从 Agent 原生配置读取，
连接器是否允许普通回复真实发送由其平台开关控制，不影响本工作台“保存草稿”的语义。
两个 Worker 默认关闭；要处理排队 Ticket 或到期订阅，在本插件配置页打开对应开关，并配置下面的凭据。

### 凭据与权限

在 AstrBot 工作目录运行三次，分别生成操作者、Ticket Worker、订阅 Worker 的独立凭据：

```powershell
python -m data.plugins.astrbot_plugin_wechat_agents.task_cli --generate-credential
```

输出 `bearer_token` 和 `token_sha256`。明文凭据仅保存到私有位置，grant 文件只保存哈希。
`data/wga-superagent-grants.json` 是 JSON 数组；每项格式如下，占位内容必须替换：

```json
[
  {
    "token_sha256": "REPLACE_WITH_GENERATED_SHA256",
    "subject": "operator",
    "owner": "YOUR_WXID",
    "rooms": ["YOUR_ROOM@chatroom"],
    "capabilities": [
      "thread:create", "thread:read", "thread:complete",
      "ticket:create", "ticket:read", "ticket:review", "ticket:retry",
      "subscription:manage", "subscription:read", "audit:read"
    ]
  }
]
```

启用 Worker 时在同一数组中再添加两个 grant，字段相同、哈希和 subject 不同：

- Ticket Worker：只授予 `ticket:dispatch`。
- 订阅 Worker：只授予 `subscription:dispatch`。

每个 Worker grant 必须覆盖 Agent 原生配置中的全部群。grant 的 `rooms` 仅限制该凭据权限，不是第二份群启用名单。
根据需要为审核者单独生成凭据；
现有审核规则要求独立证据，但不强制不同的人审核。

在启动独立 AstrBot 的同一个 PowerShell 窗口中设置：

```powershell
$env:WGA_SUPERAGENT_CONFIG = 'C:\YourAstrBot\data\wga-superagent.json'
$env:WGA_SUPERAGENT_TICKET_TOKEN = '<ticket-worker-bearer>'
$env:WGA_SUPERAGENT_SCHEDULER_TOKEN = '<scheduler-bearer>'
```

只有启用相应 Worker 才需要其明文变量。Ticket Worker 还需要 AstrBot 中已配置的模型提供方 ID。
完整重启独立 AstrBot 后，Worker 在平台加载完成时绑定。配置缺失不启动任何 Worker。

## 后台使用

入口：**插件 → 微信 Agent 任务 → 可视化视图 → 微信任务工作台**。

1. 输入操作者凭据连接，选择授权群。
2. “消息建单”：选中已有文本消息，填写本次处理目标。
3. “任务与审核”：添加具体任务，由已启用的 Worker 处理；手动刷新查看状态。
4. 选中任务查看候选，添加并人工核对证据，然后审核通过。
5. 点击“保存为草稿”，返回草稿编号；不会发送微信。
6. 在目标详情中可安排一次性跟进，在“定时跟进”中暂停、恢复或取消。

页面凭据只留在当前页面内存，刷新或断开后需重填。Dashboard 登录不代替原有账户/群/能力鉴权。
原始消息和模型结果按纯文本展示，不作为 HTML 执行。Worker 不使用工具、MCP、Hermes 或任意命令。

## 本地控制

在单独的操作者终端中设置 `WGA_SUPERAGENT_OPERATOR_TOKEN`，用 JSON 标准输入调用：

```powershell
'{"action":"list_review_tickets","owner":"YOUR_WXID","room":"YOUR_ROOM@chatroom"}' |
  python -m data.plugins.astrbot_plugin_wechat_agents.task_cli --config data/wga-superagent.json
```

只提供人工控制操作，不暴露直接领取、旧版无证据审核或全库恢复。
CLI 也读取 Agent 原生配置，默认文件为任务配置同级的
`config/astrbot_plugin_wechat_agents_config.json`。任务配置不在 AstrBot 的 data 目录时，
用 `--plugin-config` 指定这份配置文件；没有配置时不会退回旧文件名单。
更多界面操作与展示限制见 [工作台说明](docs/TASK_UI.md)。

### v0.2.0 升级

同时更新两个插件，再把原群 ID 列表填入 Agent 原生配置的 `rooms`，并用控件重新选择两个 Worker 开关。
旧任务 JSON 中的 `rooms`、`platform_id`、`run_ticket_worker` 和 `run_subscription_worker` 可删除；
保留时也会被忽略，不会覆盖原生插件配置。现有 Inbox、grant、任务记录和按群文档目录不移动。
连接器不再读取按群文档，Agent 沿用 `data/wechat_hook_workspaces/` 下的原命名空间。

## 打包与限制

**Actions → Build plugin package** 提供安装 ZIP；也可在仓库根目录运行
`python tools/package_plugin.py`，输出 `dist/astrbot_plugin_wechat_agents.zip`。
构建清单不含凭据、数据库或缓存。

禁用 Agent 插件会停止其 Worker，连接插件继续接收消息和记录普通回复草稿。
停止连接平台也会停止其绑定 Worker；平台重载后需完整重启独立实例来重绑。
崩溃任务不自动重跑，恢复仍需确认旧 Worker 已退出后人工处理。

v0.3.0 仅做语法检查、静态核对和 ZIP 构建，未编写测试代码或运行本地测试，未部署到生产或连接真实微信/模型。
界面/启停/升级尚需实机验收；无 Worker 心跳、全库搜索、真实发送回执或多代理执行。
