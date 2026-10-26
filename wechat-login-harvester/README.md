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

进程有活动统计时，每分钟最多输出一条 `OSS_STATS` JSON 汇总，退出时补充最后一个窗口。字段包含组件、主机、节点、PID、统计窗口和按操作、用途分组的次数、成功/404/其他错误、正文上传下载字节及耗时；`cache_hits` 单独统计本地缓存复用次数，不是 OSS 请求。不同客户端实例在同一进程内合并计数，不输出对象路径、账号信息、token 或异常原文。短命令和学习子进程退出时也会各自输出汇总。

这是程序发起的 SDK 调用统计，不包含 SDK 内部重试次数、HTTP/XML 开销，也不等同于计费请求数。流量字段统计成功读取的正文和成功提交的上传正文，不等同于账单外网流量；认证程序的调用仍需在对应项目中单独统计。

目录缓存只接收完整成功的分页结果；刷新失败会报错并跳过本轮登录信号处理，不用旧目录伪装成刷新成功，也不把错误当成空目录。所有缓存仅保存在进程内，重启后首次读取会访问 OSS。上述秒数可设为 `0`，目录缓存将关闭，登录信号则恢复为每次 watch 循环检查；负数和非有限值无效。修改配置后重启进程生效。
