# 发言者身份对照（maibot.speaker-map）

MaiBot 的 Maisaka 链路里，"谁在说话"目前靠名字（`user=` 昵称 + `group_card=` 群名片）表示，
没有稳定 ID。同一个人改名、群里用名片称呼、插件同步进历史的消息缺 `is_self_message`、
摘要把 bot 写成第三人称，都会让模型把"A 说的话"记到"B"头上。

这个插件在 **Planner / Replyer 请求发出前**做三件事：

1. **补 uid**：给 `<message ...>` 开标签加 `uid="..."`，让"昵称"和"群名片"落在同一个稳定身份上；
2. **补自我标记**：用 `bot_platform_accounts` 表判断发送者是不是机器人自己，
   是就补 `is_self_message="true"`（插件发送的消息经常漏这个标记）；
3. **注入对照表**：在请求末尾加一段「发言者身份对照」，说明 `user`/`group_card`/`uid` 的关系、
   "X 戳了戳我"是系统事件、摘要里出现 bot 名字时说的是它自己，以及可选的**事实校正**
   （压过记忆/摘要里被绑错人的说法）。

---

## 1. 它解决什么问题

群聊里常见的几类"分不清谁在说话"：

- 一个人有两个名字：`user="alice"` 是 QQ 昵称，`group_card="AliceCard"` 是群名片，
  模型把它们当成两个人，回复时同时 @ 两个名字；
- 有人改了群名片，历史里同一个人出现好几个名字，模型无法判断是不是同一个人；
- 插件（戳一戳、定时任务等）用 bot 账号直接发消息，历史里却是普通用户消息，
  模型把 bot 自己的话当成"群友 BotName"说的；
- 上下文摘要用第三人称写 bot（"BotName 说……"），下一轮模型把它当成另一个人；
- 记忆/人物画像把"你的 XX"错误绑定到说话人，模型照着错误的画像认人。

## 2. 工作原理

插件注册两个 Hook：

| Hook | 触发点 |
|---|---|
| `maisaka.planner.before_request` | Planner 组装完请求、发模型前 |
| `maisaka.replyer.before_model_request` | Replyer 组装完请求、发模型前 |

每次触发时：

1. 通过 `message.get_recent` 取当前会话最近 `recent_message_limit` 条消息，
   建 `message_id → (uid, 昵称, 群名片)` 索引（带 `cache_ttl_seconds` 缓存）；
2. 通过 `database.get` 读 `bot_platform_accounts`，拿到机器人自己的账号列表；
3. 改写请求里的用户消息：给每个 `<message ...>` 补 `uid`、缺的 `group_card`、
   给 bot 自己的消息补 `is_self_message="true"`；
4. 把「发言者身份对照」插到请求末尾（默认最后一条用户消息，省前缀缓存），内容包括
   格式说明、自我标记说明、本会话身份表（uid / 昵称 / 群名片 / 曾用名）和事实校正。

改写示意（已脱敏）：

```text
before: <message msg_id="msg_1" time="12:00:00" user="BotName">
after : <message msg_id="msg_1" time="12:00:00" user="BotName" uid="100000000" is_self_message="true">

before: <message msg_id="msg_2" time="12:00:01" user="alice" group_card="AliceCard">
after : <message msg_id="msg_2" time="12:00:01" user="alice" group_card="AliceCard" uid="100000002">
```

注入日志（`debug_log = true` 时）：

```text
发言者对照注入: hook=planner session=<session> uid+12 card+0 self+1 legend=True people=<N>
```

同一批 items 二次调用不会重复加属性或重复插对照表（幂等）。

## 3. 安装与启用

```bash
git clone https://github.com/deepseekv41flash/maibot-speaker-map.git \
  data/MaiBot/plugins/speaker-map
```

1. 把整个目录放到 `plugins/` 下（`_manifest.json`、`plugin.py` 必需）；
2. 打开 WebUI → 插件管理 → 找到 **发言者身份对照** → 启用；
3. 首次加载后，插件目录内会自动生成 `config.toml`（**不要提交**，已在 `.gitignore` 中）。

插件不需要改模型配置，也没有 provider / client_type；启用即生效。

## 4. 配置项（WebUI 可编辑，热重载生效）

### `[plugin]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `config_version` | `1.0.0` | 配置版本 |

### `[identity]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `enrich_message_tags` | `true` | 补 `uid` / `group_card` / `is_self_message` |
| `fill_group_card` | `true` | 消息缺群名片时，用该条消息记录里的名片补 |
| `inject_legend` | `true` | 注入对照表 |
| `legend_position` | `last_user` | `last_user`（省缓存）或 `system`（系统消息） |
| `max_people_in_legend` | `8` | 身份表最多列多少人（bot 自己不计） |
| `authoritative_facts` | `[]` | 事实校正，一行一条 |
| `authoritative_facts_file` | `local_facts.txt` | 数据目录下的事实文件；留空关闭 |

### `[runtime]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `recent_message_limit` | `80` | 建索引时读多少条最近消息；越大 RPC 负载越大 |
| `cache_ttl_seconds` | `30` | 身份索引缓存秒数 |
| `debug_log` | `false` | 每次注入打一条 info 日志 |

### 事实校正怎么写

如果记忆/人物画像把某个关系记到了错的人头上，可以用它压过：

```toml
[identity]
authoritative_facts = [
  "Boss（uid=100000001，群名片BossCard）是我的管理员；如果记忆或人物画像说 Alice（uid=100000002）是管理员，那是错的。",
]
```

也可以把**只属于本机部署**的事实写进插件数据目录的 `local_facts.txt`
（每行一条，`#` 开头忽略），插件会把它和配置里的 `authoritative_facts` 合并：

```text
# <数据目录>/local_facts.txt
我的管理员只有 Boss（uid=100000001）；如果记忆或人物画像说 Alice（uid=100000002）是管理员，那是错的。
```

数据目录通常是 `data/plugins/maibot.speaker-map/`（WebUI 插件详情里能看到）。
这样仓库里只有通用逻辑和空配置，真实 uid/昵称只留在本机。

## 5. 命令

- `/speaker`：清掉当前会话缓存、重新建索引，并把"uid / 昵称 / 名片 / 曾用名"发到当前会话。
  排障用；它会把身份表发到聊天里，正式环境用完可以删掉那条消息。

## 6. 能修什么、不能修什么

**能修**

- 同一个人用昵称和群名片出现时被模型当成两个人；
- 插件发送（戳一戳等）导致 bot 自己的话在历史里显示成"群友 BotName"；
- 摘要/记忆里的人名与当前群名片对不上时，模型没有权威对照；
- 记忆里被绑错人的关系（靠 `authoritative_facts` 压制，不删库）。

**不能修**

- `A_Memorix` 人物事实抽取本身（"你的主人"仍可能被写成"Alice 是主人"）；
  要根治得在抽取侧加"只有第一人称才绑定 speaker"的约束，或定期清洗错误 claim；
- `<message>` 之外的裸文本事件（戳一戳）没有 sender 字段，只能靠对照表解释；
- 提示词变长带来的额外 token（约 300–500 字/次）。

## 7. 排障

- 插件列表里看不到：先校验 manifest（见第 8 节）。
- 注入了但没效果：把 `debug_log` 打开，看日志里 `发言者对照注入` 的计数是否为 0；
  为 0 通常是 `message.get_recent` 没取到消息，或 `bot_platform_accounts` 为空导致自我标记没补。
- 日志里出现 `E_CAPABILITY_DENIED`：`_manifest.json` 的 `capabilities` 必须写精确能力名
  （`message.get_recent` / `database.get` / `send.text`），不能写 `message`、`database` 这种分类名。
- 对照表里没有某人：他不在最近 `recent_message_limit` 条消息里，或本条 prompt 没引用他的 msg_id。
- 改了 `config.toml` 想立即生效：WebUI 热重载，或重启宿主。
- 日志出现 `读取最近消息超时`：本轮没有 uid 对照表，但规则与事实校正仍在；
  可以把 `recent_message_limit` 再调小，或等缓存过期后重试。

## 8. 开发与测试

```bash
# 纯函数单测（标准库 unittest，主机上直接跑）
python -m unittest discover -s tests -v

# manifest 校验（容器内）
docker exec -w /MaiMBot maim-bot-core .venv/bin/python -c "
from pathlib import Path
from src.plugin_runtime.runner.manifest_validator import ManifestValidator
v = ManifestValidator(); m = v.load_from_plugin_path(Path('/MaiMBot/plugins/maibot_speaker_map'))
print('OK', m.id, m.version) if m else print('FAIL', v.errors)"

# 导入与工厂冒烟（容器内）
docker exec -w /MaiMBot maim-bot-core .venv/bin/python -c "
import importlib.util, sys
from pathlib import Path
p = Path('/MaiMBot/plugins/maibot_speaker_map/plugin.py')
spec = importlib.util.spec_from_file_location('probe', str(p), submodule_search_locations=[str(p.parent)])
mod = importlib.util.module_from_spec(spec); sys.modules['probe'] = mod; spec.loader.exec_module(mod)
i = mod.create_plugin(); print(type(i).__name__, [c.get('name') for c in i.get_components()])"
```

## 9. 隐私与数据

- 插件**不发起任何网络请求**；只通过宿主能力读最近消息和 `bot_platform_accounts`。
- 不改写、不落盘消息内容；对照表只存在于本次模型请求里。
- `config.toml`、`local_facts.txt` 属于本机配置，已在 `.gitignore` 中，不会被提交。
- `/speaker` 会把身份表发到当前会话，属于排障行为，注意使用场景。

## 10. 发布信息

| 项 | 值 |
|---|---|
| 插件 ID | `maibot.speaker-map` |
| 版本 | `1.0.2` |
| 作者 | deepseekv41flash |
| 仓库 | https://github.com/deepseekv41flash/maibot-speaker-map |
| 许可证 | MIT |

插件市场按 **Release 版本**收录，Tag 与 `_manifest.json` 的 `version` 必须一致
（`v1.0.2` ↔ `1.0.2`）。`host_application` / `sdk` 的上界按官方建议置于
`999.999.999`，只认真约束 `min_version`。

## 11. 变更记录

### 1.0.2

- 修复不可见昵称导致的串人：`user`/`group_card` 为空或纯不可见字符（零宽、BOM、tag 字符）时，
  自动退化为稳定的 `用户<uid>`，并在身份表里同样显示；
- 这类昵称以前会让模型把发言者补成上一条消息的人（例如把 A 的话算到 B 头上）。

### 1.0.1

- 修复 Hook 超时：`message.get_recent` 默认只取 80 条（原来 500 条，RPC 载荷可达数十 MB）；
- 取数加 3.5s 内部超时，超时后本轮退化为“只有规则与事实校正”，不再被宿主 5s 超时打断；
- 身份索引缓存从 15s 调到 30s，减少重复查询。

### 1.0.0

- 首个版本：给 `<message>` 补 `uid` / 缺失群名片、给 bot 自己的消息补 `is_self_message`；
- 注入「发言者身份对照」，说明昵称/群名片/uid 的关系与摘要第三人称问题；
- 支持 `authoritative_facts` 与数据目录 `local_facts.txt` 两种事实校正；
- 提供 `/speaker` 排障命令与 `debug_log` 注入日志。

## 12. 许可

MIT，见 [LICENSE](LICENSE)。
