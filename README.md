# Kernel Runtime V5.5

V5.5 是基于已验收 V5.2 的预生产执行基础版本。默认使用 Fake LLM 和脱敏 Mock 资源，不产生 Token 费用、不读取真实店铺数据、不发送真实消息。

## V5.5 新增能力

- 异步排队、并发容量控制、同会话互斥、跨会话并行。
- 幂等提交、任务查询、取消、Worker 租约和过期恢复。
- 内存任务后端与 Redis 分布式任务后端。
- FastAPI 预生产接口与 Docker Compose 运行方式。
- 电商资源白名单、Schema 校验、超时、重试、并发限制和脱敏回放。
- 零 Token 并发压测及完整回归门禁。

## 本地验收

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python main.py
python load_tests/run_load.py --tasks 200 --concurrency 16
```

## 预生产 HTTP 服务

```bash
docker compose up --build
```

启动后访问 `GET /health`。任务接口为 `POST /v1/jobs`、`GET /v1/jobs/{job_id}` 和 `POST /v1/jobs/{job_id}/cancel`。

## 安全边界

- `.env.example` 只包含占位配置，不包含任何密钥。
- GitHub Actions 权限固定为 `contents: read`。
- 真实 LLM 默认关闭；真实店铺 Provider 和真实 Delivery 未启用。
- SQLite 仅作为单实例 Runtime Store；多实例共享 Store 将在后续版本迁移到 PostgreSQL。

> V5.2 已将电商 Dispatcher、Customer、Worker、Writer、Quality 拆分为独立模块，
> 并加入可替换的业务资源 Provider 接口。详情见 `docs/V5_MODULAR_REFACTOR.md`。

版本：5.0.1。四个LLM Prompt使用全局唯一文件名，避免手机上传时同名文件覆盖或错配。

这是“真实Provider接入前最终内部版”。V3、V4基线保持不变，V5合并Prompt Package、History、两阶段Customer、确定性Worker、规范校验、业务Quality恢复与Delivery安全状态。

核心边界：

- Runtime State：可信任务状态，只由 Runtime 写入。
- Business Store：应用业务数据，应用定义 Schema，Runtime 校验并写入。
- Runtime Audit：Runtime 真实执行事件，不接受 Agent 伪造。
- Business Log：业务模块执行记录，由模块提交、Runtime 持久化。
- Pipeline、业务字段、SOP、History和业务资源全部由应用注册，不写死在Runtime。

当前包含：

- 电商客服完整 Mock Pipeline。
- 第二个最小文档应用兼容性测试，用于证明新增行业不修改Runtime。
- 资源权限、越权写入、超时迟到结果隔离测试。
- GitHub Actions默认只打印脱敏后的结果摘要，不输出完整Audit。
- Identity失败时不创建Task，直接生成人工接管记录。
- Attempt、一次重试、失败阶段和Human Handoff。
- 短期执行授权；超时、失败或阶段切换后撤销。
- Secret字段写入阻断与通用日志脱敏入口。
- 通用Delivery状态、人工审批、可信身份收件人绑定、幂等和UNCERTAIN状态。
- Mock Email Provider，GitHub测试不会访问真实邮箱。
- 统一LLM Provider协议与应用侧LLM模块适配器。
- Fake LLM流式Provider，不访问外部网络。
- 连接、首次响应、无进展和总时长四层超时。
- Runtime逻辑取消、客户端协作取消和Provider取消接口。
- 鉴权、限流、临时故障、内容拒绝和格式错误分类。
- 可信身份和Environment不会进入LLM ModuleContext或Prompt。
- 电商最小业务Schema和证据一致性Quality检查。
- 跨Task、跨应用、Prompt Injection和事务失败测试。
- 用户提供的Dispatcher、Customer、Writer、Quality Base Prompt原文。
- 每个LLM模块的电商客服应用身份和V5静态修正规则。
- Prompt版本、静态checksum和动态上下文隔离。
- Customer与Writer使用同一个最近10条消息History快照。
- Dispatcher不接收History、Runtime Context或身份信息。
- Customer先生成并锁定Goal；需要资源时由Runtime注入结果后再生成Plan。
- Worker为确定性代码，按plan_request_group处理必需/可选资源。
- `information.items`与`result.evidence_refs`统一契约。
- Runtime规范化Quality前置检查和Quality业务失败一次恢复。
- 安全/越权类Quality问题不重试，直接人工接管。
- JSON Envelope精确字段校验、尾随文本拒绝和尖括号拒绝。
- 店铺API、SOP等真实业务系统的ResourceProviderAdapter接口。
- Worker规范原文位于`applications/ecommerce/worker_spec.txt`。

运行：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python main.py
```

尚未包含：真实LLM HTTP实现、真实店铺API实现、真实SOP/History服务、真实Gmail实现、生产数据库加密和部署平台密钥管理。这些通过现有Provider接口接入，默认关闭。

生产门禁：这些真实能力接入前，必须继续使用Mock Provider；真实外发不得在GitHub Actions中启用。

## 两条分支

1. 电商客服应用：模块、Pipeline、业务Schema、SOP和订单资源全部位于`applications/ecommerce`。
2. Runtime通用性：内核不认识订单、物流或退款；测试中注册第二个文档应用且不修改内核。

## 四类数据

- `runtime_tasks`：Runtime可信状态。
- `business_store`：应用业务数据。
- `runtime_audit` / `security_audit`：可信执行和安全审计。
- `business_log`：业务模块日志。
