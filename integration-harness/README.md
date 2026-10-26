# Integration Harness

真实环境集成测试 harness，用于按已抓包得到的正常接口时序，驱动“华夏会计网”微信小程序完成课程学习和微信认证链路。

## 安全边界

- 仅使用授权测试账号。
- 仅走正常业务路径，不包含绕过、爆破、批量并发或攻击行为。
- 不自动获取或猜测 token；token 由测试人员手动从已授权抓包会话中提供。
- 不修改业务数据，不保存其他用户数据。
- 原始 token、deviceId 只从运行参数读取，不写入日志或 SQLite。

## 安装

```bash
cd integration-harness
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

## 配置

运行所需账户从外部目录加载：

```text
../account
```

目录下每个 `.account` 文件代表一个用户，文件名为身份证号，内容为 JSON：

```json
{
  "idCard": "440682198210063620",
  "name": "张三",
  "replay": false,
  "autoNextCourse": false,
  "appId": "60101caa0874ec17548b9822",
  "token": "your-token",
  "deviceId": "your-device-id"
}
```

其他可选配置可使用 `.env`：

```bash
cp .env.example .env
```

可选配置包括：

- `HEARTBEAT_INTERVAL_SECONDS`：心跳间隔，默认 60 秒
- `VERIFY_POLL_INTERVAL_SECONDS`：认证结果轮询间隔，默认 5 秒
- `QR_PAGE_PORT`：本地二维码页面端口，默认 8000
- `VIDEO_QUALITY`：视频清晰度，默认 `FD`
- `VIDEO_MAX_RETRIES`：视频下载最大重试次数，默认 5
- `RUN_START_HOUR` / `RUN_END_HOUR`：允许运行时间窗口，默认 5 点到 22 点
- `WECOM_WEBHOOK_URL`：企业微信机器人 Webhook 地址
- `WECOM_NOTIFY_TAG`：企业微信通知前缀，默认 `【integration-harness】`
- `AUTO_NEXT_COURSE`：任务完成后是否自动继续下一门；默认 `false`
- `ACCOUNT_DIR`：账户目录，默认 `../account`（相对当前项目目录）
- `OSS_BUCKET` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT`：阿里云 OSS 上传配置
- `HXACC_SSL_VERIFY`：是否校验真实后端 TLS 证书；当前环境默认设为 `false`，仅在无法验证本地代理证书时使用

## 运行

默认读取账户目录：

```bash
.venv/bin/python -m integration_harness run
```

也可以显式指定：

```bash
.venv/bin/python -m integration_harness run \
  --account-dir ../account
```

多个 `.account` 文件会并行执行，默认最多 3 个并发；可通过 `--max-workers` 调整：

```bash
.venv/bin/python -m integration_harness run --max-workers 5
```

每个账户独立运行，互不影响。账户执行失败时仍会推送企业微信，并写入补偿队列：

```text
runs/.compensation_queue.jsonl
```

修复账户文件或外部问题后，可只补偿执行之前失败的账户：

```bash
.venv/bin/python -m integration_harness run --compensate
```

补偿执行同样支持并行：

```bash
.venv/bin/python -m integration_harness run --compensate --max-workers 5
```

也可以以守护模式监控 OSS 目录 `hxacc/account/`：

```bash
.venv/bin/python -m integration_harness watch
```

同时读取本地账户目录下的 `.account` 文件：

```bash
.venv/bin/python -m integration_harness watch \
  --account-dir ../account
```

扫描逻辑：

- 若 OSS 中存在 `hxacc/account/{idCard}/finished`，推送“已完成”到企业微信。
- 若 OSS 中存在 `hxacc/account/{idCard}/stop`，视为收到停止指令：终止该账户的学习进程并推送“已停止学习”。
- 若 OSS 中已存在 `hxacc/account/{idCard}/{idCard}.process`，说明该账户正在处理，跳过并记录日志。
- `.process` 文件带租约信息，`PROCESS_LEASE_SECONDS` 默认 120 秒；过期后会自动清理并重新接管，避免设备异常退出后永久跳过。
- OSS 请求连接超时默认 10 秒；单次扫描失败不会退出 watch，会等待下一轮重试，连续失败 3 次会推送企业微信。
- 否则读取本地 `{idCard}.account`，如果 `tokenExpiresAt` 已过期，推送“请重新获取 token”到企业微信。
- 每轮会比对本地 `{idCard}.account` 文件内容；配置变更后自动重启该账户的子进程，`autoNextCourse` 等单账号覆盖无需重启 watch。
- 如果未过期，则创建 `{idCard}.process` 后启动对应的课程学习子进程；进程结束或被杀前会删除该标记文件，删除失败会推送企业微信。

扫描间隔通过 `ACCOUNT_WATCH_INTERVAL_SECONDS` 配置，默认 2 秒。子进程日志写入：

多设备均衡通过槽位分配实现：

- `ACCOUNT_SLOT_COUNT`：固定槽位数，默认 16。
- `WORKER_ID`：当前设备 ID，默认 `device-01`。
- 节点名单存在 OSS `hxacc/workers`，`watch` 每轮扫描重读，扩缩容热加载。

每台设备只处理哈希后属于自己槽位的账户。修改节点名单：

```bash
.venv/bin/python -m integration_harness workers --list
.venv/bin/python -m integration_harness workers --add device-02     # 扩容
.venv/bin/python -m integration_harness workers --remove device-02  # 缩容
```

新增节点只需在 `.env` 里设好自己的 `WORKER_ID`，然后启动 `watch`；名单尚未在 OSS 建立
时，会 fallback 到 `WORKER_IDS` 环境变量（逗号分隔）。

```text
runs/watch/{idCard}.out.log
runs/watch/{idCard}.err.log
```

每个账户运行前，会把对应 `.account` 文件上传到：

```text
hxacc/account/{idCard}/{idCard}.account
```

任务播放完成后，会在该目录下新增：

```text
hxacc/account/{idCard}/yyyyMMddHHmmss.success
hxacc/account/{idCard}/finished
```

其中 `finished` 文件为空内容，表示该账户的全部任务已经看完。

认证二维码触发后，会生成并上传：

```text
hxacc/account/{idCard}/{pointCode}.png
hxacc/account/{idCard}/{pointCode}.json
```

`pointCode.json` 包含 `task_id`、`created_at`、`expires_at`、`sha256`。
其中时间使用 `Asia/Shanghai`（`+08:00`），`sha256` 是 PNG 图片字节内容的 SHA-256，用于防止重扫。

每个小节（doc）播放完成后，才通过 `/api/mycert/getTaskCert` 检查课程是否同步完成：匹配 `taskId` 后，`extra.synced=1` 视为已完成；`synced=0` 或缺失则继续定位下一小节观看。本地课程进度 `lp=100` 但只要 `synced != 1`，仍会继续定位 `courseList` 中的课程观看，不会误报“任务已全部看完”。

认证等待期间不轮询同步状态，认证通过后恢复播放。若认证未通过，等待 30 分钟后会推送企业微信“认证未通过，已停止学习”通知，并在 OSS 写入 `hxacc/account/{idCard}/stop` 停止标记，终止该账户的学习（不影响其他正在学习的学员）。

解决认证问题后，恢复学习：

```bash
.venv/bin/python -m integration_harness resume --id-card 440682198210063620
```

`resume` 会删除该账户的 `stop` 标记并推送“已恢复学习”。watch 模式会在下一轮扫描时自动重新接管该账户；直接 run 模式也可重新运行对应账户文件。

`replay=true` 时，允许选择已经 `finish` 的小节进行重复播放；默认 `false` 跳过已完结小节。

账户目录没有文件，或 OSS 上传失败时，会推送企业微信并跳过后续学习。

程序会：

1. 获取任务列表，按接口返回顺序逐个处理所有任务。
2. 已完结的任务、课程或小节会自动跳过；一次只启动一个视频。
3. 循环处理每个任务内未完结课程和小节，直到该任务看完为止。
4. 获取课程详情和课程小节。
5. 进入课程并开始学习。
6. 获取广东监管状态并恢复学习。
7. 按真实时间下载 HLS 分片并发送心跳，直到达到 `learned[docId].totalTime`。
8. 当心跳响应出现 `needPoint: true` 和 `qrcodeUrl` 时，暂停课程并打开本地二维码页面。
9. 测试人员用手机微信扫码。
10. 页面自动轮询认证结果，成功后恢复学习并继续完成该小节。

学习接口（心跳、课程等）或视频下载返回 401（token 失效）时，会在 OSS 写入
`hxacc/account/{idCard}/relogin` 重新登录信号并同时写入 `stop` 停止标记（避免
watch 在重新登录完成前反复重启该账户），由 wechat-login-harvester 的 `watch`
检测到后自动重新采集 token，成功后删除两个标记恢复学习。

运行过程中会向企业微信推送以下事件：

- 视频下载重试耗尽或运行异常
- 触发“您今天学习时长已经超过8小时，不继续累计时长！”
- 任务完成后（`AUTO_NEXT_COURSE=false` 时）列出已完成课程和下一门
- 账户全部课程完成后推送最终完成汇总
- 当前时间不在允许运行窗口

企业微信推送失败时会写入本地 outbox：

```text
runs/.wecom_outbox.jsonl
```

下一次运行启动时会先尝试补发 outbox 中未发送成功的通知。

## 企业微信使用说明

项目已经复用同一个企业微信群机器人，不需要新增机器人。只需在 `.env` 中配置：

```bash
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=已有机器人key
WECOM_NOTIFY_TAG=【integration-harness】
```

`AUTO_NEXT_COURSE` 和 `.account` 里的 `autoNextCourse` 一起决定任务完成后的行为：

| 配置 | 行为 |
| --- | --- |
| 全局 `AUTO_NEXT_COURSE=false`（默认） | 任务完成后发送企业微信清单，写 `stop`，等人工 `resume` |
| 全局 `AUTO_NEXT_COURSE=true` | 不逐任务通知，自动继续下一门；全部完成后发最终汇总 |
| 账号内 `autoNextCourse` 覆盖全局 | 单个账号可单独切换模式，watch 检测到文件变化后自动重启该账号进程 |

任务完成通知示例：

```text
✅ 任务已完成
任务：广东省2026年度继续教育-公需课
已完成课程：
- 人工智能赋能高质量发展
下一门：专业课：新《会计法》对财务人员的影响
```

人工恢复命令：

```bash
.venv/bin/python -m integration_harness resume --id-card 440682198210063620
```

## 运行产物

每次运行会创建：

```text
runs/<session_id>/
├── run.log
└── events.sqlite3
```

`events.sqlite3` 记录 API 调用和关键响应字段，`run.log` 记录人类可读日志。

## 二维码页面

默认地址：

```text
http://127.0.0.1:8000/qr
```

如果浏览器没有自动打开，可手动访问该地址。页面会每 3 秒更新显示，并自动轮询认证结果。

## 测试

```bash
pytest
```

## OSS 请求优化与观测

学习监控仍按 `ACCOUNT_WATCH_INTERVAL_SECONDS`（默认 2 秒）检查本地进程和账号文件，但 OSS 读取使用独立的进程内缓存：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `OSS_DIRECTORY_CACHE_SECONDS` | 60 | 完整账号目录列表；新增、删除账号的发现可额外延迟约 60 秒 |
| `OSS_WORKERS_CACHE_SECONDS` | 60 | 节点名单；归属变更的发现可额外延迟约 60 秒 |
| `OSS_STOP_POLL_SECONDS` | 10 | 停止标记及原因；普通停止/恢复约 10 秒内被下一次扫描发现 |
| `OSS_STATUS_POLL_SECONDS` | 60 | 完成、每日上限状态及空闲账号进程标记清理；解除每日上限可额外等待约 60 秒 |

周期按读取完成后计算，实际还包含循环、网络及进程启停耗时。设置为 `0` 关闭对应缓存；负数及非有限值无效。修改后重启 `watch` 生效，不需要更改生产 `.env` 才能使用默认值。

完整读取成功或明确的对象不存在才进入缓存。超时、权限、服务错误及非法节点名单均不刷新缓存、不冒充空名单；本轮远端扫描失败时不会新启动账号，但已有本机进程仍独立执行租约续期和退出回收。缓存有容量上限且不落盘。每日上限在本机日期跨日时强制重读，成功删除状态标记时清除本进程对应缓存；子进程退出时也清除该账号的状态缓存，避免短期内用旧状态重新启动已停止/完成的学习。

本机已管理的账号不重复读取远端租约；启动/接管前仍实时读取，不缓存执行权判断。120 秒租约及约 60 秒续期规则保持原配置。

`OSS_STATS` 按进程每分钟最多输出一次有数据的简短摘要，退出时补齐剩余窗口。默认显示组件、节点（未配置时使用主机）、PID、窗口秒数、总请求、成功、未找到、异常、缓存命中及正文上下行流量。设置 `OSS_STATS_FORMAT=json` 可输出完整诊断 JSON，按 SDK 操作、用途展示次数、状态、字节及累计与最大耗时；`cache_hits` 单列本地复用次数。多个 OSS 客户端实例共享本进程统计。汇总不含对象路径、身份证、token 或异常原文，不向 OSS 上传这些统计日志。

统计范围为当前学习程序，认证端仍需单独接入。它记录 SDK 调用而非内部重试或实际计费量，正文流量不含 LIST XML、HTTP 开销及失败时的部分传输；费用对比仍以云监控和账单为准。条件 GET、404 和成功请求的计费规则不同，不能直接把总调用数乘单价。
