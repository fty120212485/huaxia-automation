# wechat-login-harvester

用于自动完成“打开微信小程序 → 输入账号密码 → 点击登录”，并通过本地代理抓取登录响应，提取 Token。

## 命令

```bash
python -m wechat_login_harvester login
python -m wechat_login_harvester capture
python -m wechat_login_harvester harvest
python -m wechat_login_harvester reconcile
python -m wechat_login_harvester watch
python -m wechat_login_harvester bot
```

- `login`：执行 UI 登录流程。
- `capture`：启动抓包代理，监听登录请求并提取 Token。
- `harvest`：同时启动抓包代理和 UI 登录。
- `reconcile`：按 `USER_FILE` 对账，删除已标记跳过用户的本地 `.account` 和 OSS 目录。
- `watch`：持续监控 `USER_FILE`，有变更时自动对账，并对新增用户重新采集 token。
- `bot`：仅启动企业微信智能机器人长连接，处理用户增删改查指令（`watch` 会自动附带启动机器人）。

`watch` 还会轮询 OSS 的 `hxacc/account/{idCard}/relogin` 重新登录信号：
integration-harness 遇到视频 401 时写入该信号，`watch` 检测到后对对应用户重新执行
登录并采集新 token，成功后删除 `relogin` 信号与 `stop` 停止标记以恢复学习。若重新
登录连续失败超过 2 次，会推送企业微信要求马上修复，并删除 `relogin` 信号停止自动
重试（`stop` 标记保留，账户保持停止等待人工处理）。

## 配置

复制并填写：

```bash
cp .env.example .env
```

关键配置：

- `USER_FILE`：账号密码 JSON 文件
- `ACCOUNT_DIR`：写入 token/账号信息的目标目录
- `CAPTURE_PORT`
- `LOGIN_URL_MARKER`
- `WATCH_INTERVAL_SECONDS`：`watch` 命令的轮询间隔，默认 5 秒
- `RELOGIN_POLL_INTERVAL_SECONDS`：重新登录信号检查间隔，默认 15 秒，与用户文件检查分开；实际还包含当前采集和网络耗时。
- `OSS_DIRECTORY_CACHE_SECONDS`：账号目录完整列表的缓存时间，默认 60 秒；新增、删除目录最多额外等待约 60 秒被发现。
- `LOGIN_LOGOUT_DELAY_SECONDS`：每次登录、登出前等待的秒数，默认 30 秒
- `WECOM_WEBHOOK_URL`：企业微信机器人 Webhook 地址
- `WECOM_BOT_ID` / `WECOM_BOT_SECRET`：企业微信智能机器人长连接凭证，用于通过机器人消息增删改查用户
- `OSS_BUCKET` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT`：删除 OSS 账号目录所需

## 企业微信智能机器人（长连接）

在智能机器人管理后台开启「API 模式」并选择「长连接」方式，把得到的 `BotID` 和 `Secret` 写入 `.env` 的 `WECOM_BOT_ID` 和 `WECOM_BOT_SECRET`。机器人通过 `wss://openws.work.weixin.qq.com` 建立长连接，`watch` 运行时会自动在后台维护连接（30 秒心跳、断线自动重连）。

在单聊或群聊（@机器人）中发送文字命令即可操作 `USER_FILE`，`watch` 检测到 `USER_FILE` 变化后会立即对账并采集新用户 token。

支持的命令：

```text
新增 姓名:张三 身份证:440682198001010011
删除 身份证:440682198001010011
修改 身份证:旧号码 姓名:新名字 身份证:新号码
查询
查询 身份证:440682198001010011
未学习
学习状态
停止 身份证:440682198001010011
恢复 身份证:440682198001010011
恢复 身份证:440682198001010011,440682198210063620
恢复学习+440682198001010011,440682198210063620
批量恢复 身份证:440682198001010011,440682198210063620
自动下一门 身份证:440682198001010011 开关:true
自动下一门 身份证:440682198001010011 开关:false
全局自动下一门 开关:true
全局自动下一门 开关:false
帮助
```

说明：

- `新增`/`添加`/`增加`：新增用户；身份证已存在时会返回“已存在”。
- `删除`/`移除`：按身份证删除用户。
- `修改`/`更新`：`身份证:` 第一次出现表示原身份证，第二次出现表示新身份证；也可单独修改姓名或 `skip`（如 `skip:true`）。
- `查询`/`列表`：列出全部用户；带身份证时查询单个用户。
- `未学习`：列出当前没有在学习的用户（有 `user` 记录、未完成、且无有效 `.process` 运行标记）。
- `学习状态`：列出全部用户的学习状态（学习中 / 未在学习 / 已完成 / 已跳过）。
- `停止 身份证:xxx`：向 OSS 写入 `hxacc/account/{idCard}/stop` 标记，integration-harness 检测到后停止该学员的学习进程。
- `恢复 身份证:xxx`：删除上述 `stop` 标记，学习任务重新恢复。
- `恢复学习+身份证1,身份证2`：批量恢复指定用户，身份证用英文逗号分隔（也支持中文逗号）；重复号码只处理一次，回复列出成功和失败结果。
- `恢复 身份证：身份证1,身份证2`：同样支持批量恢复，可直接粘贴逗号分隔的身份证列表。
- `自动下一门 身份证:xxx 开关:true/false`：直接修改对应 `.account` 的 `autoNextCourse`；integration-harness 的 watch 检测到文件变化后会自动重启该账号进程。
- `全局自动下一门 开关:true/false`：修改 integration-harness `.env` 的 `AUTO_NEXT_COURSE`，integration-harness 的 watch 检测到后重启全部运行中的学习进程。
- `自动下一门开关`：显示续学开关说明；全局在 integration-harness 的 `.env` 配置 `AUTO_NEXT_COURSE`。
- 中文冒号、英文冒号、`=` 均可作为分隔符。

学习状态通过读取 OSS `hxacc/account/{idCard}/` 目录下的标记判断：

- `finished` 存在 → 已完成；
- `{idCard}.process` 存在且 `expires_at` 未过期 → 学习中；
- 无 `.process` 或已过期 → 未在学习；
- `user` 里 `skip: true` → 已跳过。

`USER_FILE` 示例：

```json
[
  {
    "name": "梁敏仪",
    "id_card": "440682197801283620",
    "skip": false
  },
  {
    "name": "梁映仪",
    "id_card": "440682198210063620",
    "skip": true
  }
]
```

`skip` 为 `true` 的用户不会登录、不会生成 `.account`，并会在 `reconcile`/`watch` 时删除对应的本地与 OSS 文件。

登录成功后会把捕获到的 `appId`、`token`、`deviceId`、`idCard`、`name`、`tokenExpiresAt` 写入：

```text
../account/{idCard}.account
```

`tokenExpiresAt` 是 JWT 中 `exp` 的 Unix 时间戳（秒）。

同时，每次成功采集 token 后会向 OSS 上传：

```text
hxacc/account/{idCard}/{idCard}.account
hxacc/account/{idCard}/account.json
```

其中 `{idCard}.account` 是与本地账户目录相同的完整账户文件；`account.json` 是账号索引信息，内容：

```json
{
  "username": "姓名",
  "id_card": "身份证号码"
}
```

OSS 凭证需配置在 `.env` 中（`OSS_BUCKET`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_ENDPOINT`）。

上传 OSS 文件时会打印开始/成功/失败日志；失败时同时推送企业微信。登录、登出、抓包代理、OSS 删除等失败操作也会推送企业微信。

## OSS 调用统计与缓存

进程有活动统计时，每分钟最多输出一条简短的 `OSS_STATS` 摘要，退出时补充最后一个窗口。默认显示组件、节点（未配置时使用主机）、PID、窗口秒数、总请求、成功、未找到、异常、缓存命中和正文上下行流量。设置 `OSS_STATS_FORMAT=json` 可输出完整诊断 JSON，包含按操作、用途分组的次数、状态、字节和耗时；`cache_hits` 不是 OSS 请求。不同客户端实例在同一进程内合并计数，不输出对象路径、账号信息、token 或异常原文。短命令和学习子进程退出时也会各自输出汇总。

这是程序发起的 SDK 调用统计，不包含 SDK 内部重试次数、HTTP/XML 开销，也不等同于计费请求数。流量字段统计成功读取的正文和成功提交的上传正文，不等同于账单外网流量；认证程序的调用仍需在对应项目中单独统计。

目录缓存只接收完整成功的分页结果；刷新失败会报错并跳过本轮登录信号处理，不用旧目录伪装成刷新成功，也不把错误当成空目录。所有缓存仅保存在进程内，重启后首次读取会访问 OSS。上述秒数可设为 `0`，目录缓存将关闭，登录信号则恢复为每次 watch 循环检查；负数和非有限值无效。修改配置后重启进程生效。

## 官网接口登录与验证码 OCR

新增 `web-login`，无需打开微信或启动抓包代理。程序从地区官网获取 SVG 验证码，先移除独立的无填充描边干扰线路径，保留文字填充路径，再放大四倍，使用 macOS Vision 在本机 OCR。关闭语言纠错；仅接受四位字母数字。识别失败或服务器明确提示验证码错误时刷新验证码，最多三次；账号、地区、网络等其他错误直接停止，不无限尝试。不通过企业微信发送验证码，也不调用外部识别服务。

先更新依赖：

```bash
.venv/bin/python -m pip install -e .
```

在 `.env` 中设置账号报名地区对应的官网（广东账号已实测；其他地区尚未验证）：

```dotenv
WEB_LOGIN_BASE_URL=https://guangdong.hxacc.com
```

单独测试 user 文件中的一个未跳过账号：

```bash
.venv/bin/python -m wechat_login_harvester web-login --user <身份证号>
```

此命令会更新本地 `.account`；已配置 OSS 凭据时，也会沿用原流程上传账户及 account.json。登录可能使旧会话失效，上传后学习服务可能接手该账号。

要让 `login`、`harvest` 和 `watch` 中的新增账号采集、自动重登都使用接口方式，在 `.env` 中设置：

```dotenv
LOGIN_BACKEND=web
```

默认 `LOGIN_BACKEND=ui` 保留现有微信界面流程。`web-login` 始终使用接口方式。一个进程使用一个地区官网，混合地区账号应分开配置；不会根据身份证籍贯推断报名地区。

设备标识按账号保存在 `TOKEN_OUTPUT_DIR/web-login/`，后续登录复用，避免每次随机换设备。当前使用持久化随机标识，单账号已验证可用；与网页 FingerprintJS 的设备指纹生成方式不同，长期稳定性仍需观察。官网提示其他设备已登录时按官网重新登录流程继续。成功后核对身份和设备，并只读验证 `getTaskList`；全部通过才原子替换账户文件，失败保留旧文件。不会在登录代码中启动学习。凭据文件权限为 0600，不记录验证码或 token。

HTTPS 使用系统证书信任，不关闭校验；如需指定 CA，配置 `WEB_LOGIN_CA_FILE=/绝对路径/ca.pem`。OCR 需要 macOS 的图像和 Vision 服务。OCR 不保证每次成功，连续三次失败会明确报错，等待下一次操作或监控重试周期。

## 金山表格自动新增账号

使用独立 Chrome 配置目录保存金山登录状态，不使用收费开放平台 API，也不需要复制 Cookie。需安装本机 Google Chrome，并更新依赖：

```bash
.venv/bin/python -m pip install -e .
```

在 `.env` 配置：

```dotenv
KDOCS_URL=https://www.kdocs.cn/l/cm89y8KI1qbf
KDOCS_PROFILE_DIR=runs/kdocs-browser
KDOCS_SHEET=
KDOCS_POLL_SECONDS=5
```

首次登录或会话过期时运行下列命令，在专用 Chrome 窗口中自行登录。程序确认能够导出读取后会自动关闭并保存状态；不会新增账号：

```bash
.venv/bin/python -m wechat_login_harvester kdocs-login
```

只读检查表格及待新增数量：

```bash
.venv/bin/python -m wechat_login_harvester kdocs-check
```

启动原有 `watch` 即会同时监控金山表格：

```bash
.venv/bin/python -m wechat_login_harvester watch
```

后台每隔至少五秒检查文档版本，有变化才重新导出 Excel。连续两次成功读取到姓名、证件号相同的待处理记录后，才批量追加到 `user`；新增后由原有 user 监控触发后续登录及凭据上传。登录过程不阻塞表格检测线程。首次启用会导入当前表内符合条件且本地不存在的账号，不仅限于启用之后新插入的行。

过滤规则：

- 只读取指定工作表；`KDOCS_SHEET` 留空时，自动选择唯一包含完整表头的工作表。无法唯一选择时停止导入，不猜测。
- 必须有姓名、证件号（支持“身份证号码”等名称）、已同步、认证完成四列。状态列任一标注“已同步”“认证完成”或“已删除”就跳过；备注中出现这些字样不作为状态。
- 同一证件号出现多行时，只要任一行已同步、认证完成或已删除，就不新增该证件号。姓名冲突的重复记录也不导入。
- 姓名、证件号缺失或不安全时跳过；香港证件号按文本保留，不限制十八位。证件列请设置成文本，超过十五位的数字型单元格可能已被 Excel 截断，因此跳过。
- 证件号按大小写不敏感去重；仅追加账号，不删除、不覆盖本地姓名、skip 等配置，不写回在线表格。
- 已处理证件号记录在 `TOKEN_OUTPUT_DIR/kdocs/`，重启不重复导入；手动从 user 删除的已导入账号也不会再次自动加入。
- 读取失败时不改 user，且重新计算连续稳定扫描。后台会重试，会话失效或权限不足时提醒重新运行 `kdocs-login`，无需复制 Cookie。

也可手动执行一次导入（两次扫描后只新增 user，不主动执行登录；若 watch 正在运行，它会接手）：

```bash
.venv/bin/python -m wechat_login_harvester kdocs-sync
```

`kdocs-login/check/sync` 均支持 `--url` 覆盖本次分享链接。专用浏览器会话与读写状态不应提交 Git；默认目录均在已忽略的 runs 下。不要同时运行多个使用同一专用目录的登录或监控进程。若 watch 正占用会话，先退出 watch，再运行 kdocs-login，完成后重新启动 watch。

这里使用金山网页自身的导出接口，页面接口变化、账号权限变化和服务器主动注销都可能影响可用性；Cookie 上的到期时间不代表服务器保证会话始终有效。

## OSS / Local 存储切换（登录端与学习端）

两端使用同样的配置项：

```dotenv
STORAGE_MODE=Local
SHARED_ROOT=/Users/fty/共享/hxacc
```

`STORAGE_MODE=OSS` 保留原 OSS 行为；`Local` 不创建 OSS 客户端，无需 OSS 凭据。选择 Local 后两端统一使用 `SHARED_ROOT/credentials` 的 `.account`，忽略旧 `ACCOUNT_DIR`，不会在每个进程下生成独立账户目录。其他机器需独立部署配置，本次不调整认证端。

目录布局保留原对象 key：`hxacc/account/证件号/...` 映射为 `SHARED_ROOT/account/证件号/...`；二维码、success、停止/恢复、完成/每日上限、考试状态等均在此读写。`credentials` 是两端共同使用的运行凭据目录，`account/证件号/证件号.account` 保留对象布局副本。切换回 OSS 后仍可使用原实现。

显式迁移命令（仍使用保留的 OSS 凭据读取源）：

```bash
.venv/bin/python -m wechat_login_harvester.migrate_local --root /Users/fty/共享/hxacc
```

迁移保留 OSS 原件，同步所有账户凭据和状态及未完成账号的运行资料，不下载已完成账号的历史二维码。清单在根目录 `.migration-login-learning.json`，包含源版本和目标哈希，不含 token。重跑会校验并续传；已有本地变更优先保留，摘要会报告冲突。**先预同步，再暂停登录端/学习端写入，最后同步一次后切换两端并重启。** 不应让两端长期使用不同后端；认证端应由其自身配置使用一致的目录，本文不替它部署。

Local 要求目录已经初始化（存在 `.hxacc-store.json`），路径错误时直接报错，不会回退 OSS 或悄悄创建另一个本地库。写文件使用临时文件及原子替换；凭据和状态文件权限 0600。学习端使用进程文件锁，退出子进程后才释放账户锁，避免两个学习监控同时接管同一账户。Local 模式每轮直接读目录和状态，不使用 OSS 的目录/状态缓存。

运行日志和本机 SQLite 学习记录仍保留在各项目原 runs 目录，不清理原 `../account`。迁移清单不等于学习进程已经重启，切换需确认旧进程退出。
