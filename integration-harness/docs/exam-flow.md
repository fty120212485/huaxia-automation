# 学习中的正式考试分支

任务入口和每个小节结束后，先刷新 `getMytaskDetail`。当
`data.taskInfo.examId` 存在且 `data.mytaskInfo.examStatus` 为 `0`（兼容字符串 `"0"`）时执行考试分支；
其他值继续原有学习流程。`examStatus=0` 不等于任务完成。

考试通过与任务完成分开处理：正式考试通过后刷新任务，仍以
`getTaskCert` 对应任务的 `extra.synced=1` 判断完成。`synced=0` 时继续原有课程定位、
分片下载和心跳上报，不因考试通过而写入账户 `finished`。已通过的考试只查询状态，
不会重复答题或调用 LLM。

## 考试 ID 与配置

已从学习中心页面确认 `taskInfo.examId` 是考试入口使用的考试 ID。
程序支持 `taskInfo.examConfig` 内明确命名的 `examId` / `examIds` 字段，以及
`taskInfo.examId` / `taskInfo.examIds`。不会将配置项的 `id` / `_id` 猜成考试 ID。
多个明确的考试 ID 按顺序处理，每场均查询正式考试结果。

如实际响应结构无法识别，可在 `.env` 设置按任务 ID 的明确映射（此项优先）：

```dotenv
EXAM_TASK_MAP_JSON={"实际任务ID":["实际考试ID"]}
EXAM_BASE_URL=https://guangdong.hxacc.com
EXAM_HEARTBEAT_SECONDS=20
EXAM_POLL_SECONDS=3
EXAM_POLL_ATTEMPTS=30
EXAM_SUBMIT_MIN_SECONDS=30
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_MODEL=glm-4.7-flash
LLM_API_KEY=填写你的密钥
LLM_TIMEOUT_SECONDS=60
LLM_RETRIES=3
```

不新增 `autoExam` 开关，分支由 `taskInfo.examId` 和 `mytaskInfo.examStatus=0` 共同控制。`autoNextCourse` 保留原语义。
缺少映射或 LLM 密钥会报告明确原因并写 `stop`，不盲目开考。
不需要答题的已通过考试、只需查分的考试不要求 LLM 密钥。

## 状态查询与执行

所有考试接口使用现有账号 token 作为 `sid`，POST JSON 到 `/api/newexam/`：

1. `postViewExam` 获取规则、`myexamInfo` 和模拟考试信息。
2. `getGradeList` 分页获取当前 `examId` 的历史记录，正式和模拟考试分开判断。
3. `myexamInfo.isPassed` 或正式历史记录确认通过时返回学习流程。
4. 正式考试正在进行时，用 `getExamState` 获取当前 `mygradeId`，查询 `getMygradeDetail`。
5. 未开考或已有明确不及格成绩时，重新获取任务详情；只有 `learnedStatus=2` 才调用 `checkStartCondition`，并且 `code=0`、`data.pass=true` 才调用 `postStartExam`。学习未完成或开考条件未满足时返回原有学习流程，不写 `stop`。正在考试与待成绩恢复不受此开考前置条件限制。
6. 按题提交 `postSubmitAnswer`；答题与等待期间通过 `postHeartCheck` 维持会话。
7. 所有题目有有效答案且满足配置的最短交卷等待后，调用 `postSubmitExam`。
8. 用 `getMygradeDetail(type=checkState)` 等待 `gradeResult`，保存分数与通过状态。

通过标记采用明确布尔转换，`"false"` 不会被视为通过。仅在没有明确通过标记时，
才使用有效分数与有效及格线比较。缺少成绩不能判为不及格。
未知历史状态通过成绩详情的 `action` 进一步确认，不猜测未验证的状态枚举。

次数是否允许由 `checkStartCondition` 最终决定，不将参考项目某次考试的
`normalLimitNumber=1` 固化成所有考试不可重考。明确不及格后刷新规则再检查重考条件；
开考条件拒绝（包括次数用尽）时保留考试进度并回到学习流程；人脸认证等阻塞仍写 `stop`，通过现有异常通知上报。

## 断点恢复与 LLM 调用

恢复记录按账号、任务、考试隔离，存放于：

```text
runs/exam_states/<身份摘要>.json
hxacc/account/<账号>/exam/<身份摘要>.json
```

包含 `taskId`、`examId`、`mygradeId`、正式考试类型、阶段、逐题答案、题目摘要、
提交状态、各次考试成绩和递增版本。文件不含 token 或 LLM 密钥。
本地原子写入后同步 OSS；上传失败暂停后续考试操作。跨设备恢复依赖现有账户租约互斥。

恢复时先读服务端 `myAnswers`，有效已提交答案直接跳过。服务端未收到的答案，
只有题干、题型、选项和题号的摘要匹配时才复用缓存。LLM 输出先保存，再提交，
因此请求未送达或提交响应丢失后都可恢复。LLM 使用独立客户端，不携带账号登录信息。

重考创建新试卷，不复用旧卷的已答状态或未经验证的旧答案。当前支持判断、单选和多选；
未知题型、无效答案会保留进度，不提交不完整的试卷。

开考、逐题提交、交卷不会自动进行网络层重试。发生未知结果时，下次执行先查询服务端：
进行中的考试继续答题；已交卷只查分；开考结果尚无法定位时保持待恢复状态。
查分超时保留记录并通过现有失败队列处理，不当成不及格或成功，不写 `finished`。

配置或认证问题修复后，使用原有 `resume --id-card ...` 清除停止标记恢复。
watch 下一轮接管时会查询服务端并加载恢复记录。

## 验证边界

测试通过模拟服务端的实际状态变化，覆盖提交成功但响应丢失、跨设备恢复、
待成绩恢复、历史通过记录、重考、次数用尽及学习同步分支。开发测试不调用真实考试、
LLM、OSS 或企业微信接口。部署前需用实际任务响应核对考试 ID 映射和历史记录结构。

GLM-4.7 系列显式开启深度思考，只从 `content` 提取最终选项。
`LLM_MAX_TOKENS` 默认 8192，包含推理额度；遇到 `finish_reason=length` 时不采用截断答案，
在现有重试次数内将额度翻倍，最高 `LLM_MAX_TOKENS_LIMIT`（默认 32768）。
