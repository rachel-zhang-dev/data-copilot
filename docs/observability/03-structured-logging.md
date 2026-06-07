# W3 · 结构化日志 + W3C TraceContext 决策日志

> Status: Shipped · Date: 2026-06-07 · Commit: `0ae451a`
>
> Roadmap reference: [日志与Metric系统学习清单 §三 + §六.2](https://github.com/rachel-zhang-dev/interview-notes/blob/main/日志与Metric系统学习清单.md)
>
> 前置：[01-current-state.md](./01-current-state.md)

---

## 一、Context（为什么这周做这件事）

W1-2 审计发现：data-copilot 已经装了 `structlog` 和 `prometheus-fastapi-instrumentator`，但**只装没启用**——日志还是普通 `logging.getLogger` 输出的纯文本。这意味着：

- 同一次请求的所有日志没有共同 ID 串联
- 同一次对话的多个 turn 散落在日志流里没法 grep 出来
- 后续 W4 接 Loki / W10 接 Jaeger 时**没有任何 ID 可以挂**

W3 的任务是把"日志能被机器解析 + 跨服务可追溯"这件事做完，给后面 10 周的所有改造打地基。

---

## 二、Goals

| # | 目标 | 验收 |
|---|------|------|
| 1 | 所有日志变成结构化（JSON 或键值对） | `docker compose logs api` 每行都能 `jq` 解析 |
| 2 | 每条日志带 `trace_id` / `span_id` / `request_id` | 响应 header 出现 `traceparent` |
| 3 | `/ask` 路径每条日志带 `conversation_id` | 能 grep 出一次对话的全部生命周期 |
| 4 | 不破坏现有功能 | 577/577 测试零回归 |
| 5 | W11 接 OTel SDK 时无返工 | trace_id 已是 W3C 32-hex 格式 |

---

## 三、4 个核心决策

### 决策 1：dev 彩色 / prod JSON，按 `APP_ENV` 切换

**选择**：`APP_ENV=development` → `ConsoleRenderer(colors=True)` · `APP_ENV=production` → `JSONRenderer()`

**为什么不永远 JSON**：开发时 `docker compose up` 看一屏 JSON 字符串眼睛会瞎。`ConsoleRenderer` 用 ANSI 色给 `info`/`warning`/`error` 上色 + 字段名青色 + 时间戳灰色，**信息密度和可读性都最高**。

**为什么不永远文本**：生产日志要被 Loki / ELK 按字段索引，纯文本只能全文检索，等于退化到 `grep`。

**实现取舍**：用同一条 `shared_processors` 流水线（merge_contextvars → add_log_level → TimeStamper → redact_secrets），**只在最后一个 processor（renderer）切换**。换句话说，日志的**语义**在两个模式下完全一致，只是**渲染**不同。

### 决策 2：stdlib + structlog 共存（bridge 模式）

**选择**：所有 `logging.getLogger(__name__)` 通过 `ProcessorFormatter` 走进同一条处理链。

**关键代码**：
```python
formatter = structlog.stdlib.ProcessorFormatter(
    foreign_pre_chain=shared_processors,  # 让 stdlib 记录享受同样的丰富化
    processor=renderer,
)
```

**为什么不直接换掉 stdlib**：FastAPI、SQLAlchemy、httpx、LangChain 全是用 `logging.getLogger(__name__)`，要全部改成 structlog 既不现实也不必要。bridge 让"业务代码用 structlog 享受类型化字段、第三方库继续用 stdlib"成为可能，**两边的日志渲染样式完全一致**。

**踩到的具体好处**：跑测试时 LangChain 自己打的 retry 日志现在也带 `trace_id` + `conversation_id`，**完全免费**。

### 决策 3：现在就上 W3C TraceContext，不用 UUID 占位

**选择**：装 `opentelemetry-api`（50KB，**只 API 不 SDK**），用 `format_trace_id` / `format_span_id` 生成符合 W3C 规范的 ID。

**最初的诱惑**：UUID 简单，4 行代码就完事：
```python
request_id = str(uuid.uuid4())
```

**为什么放弃**：W10-W11 接 OTel SDK 时，SDK 会强制 W3C 格式（32 hex trace_id + 16 hex span_id）。**现在不上 W3C，W10 要重写中间件 + 重写所有日志查询 + 跟下游约定新格式**。多花 1 小时学 `traceparent` header 格式，省 W10 一整天返工，性价比极高。

**附带收益**：现在已经实现了 trace 续接 —— 上游服务带 `traceparent` 进来，我们保留 `trace_id` 不变、生成新 `span_id`；下游也能继续续。这是 OTel 的核心能力之一，**提前 8 周白嫖**。

```
请求进来    traceparent: 00-aabb...8899-0011...6677-01
                       │       │              │
                       │       trace_id        incoming span_id
                       │       (保留)          (作为我们的 parent_id，但当前实现没存)
                       │
   我们的处理 ─────────┴─→ trace_id 保留 + 新 span_id 用于这一跳
                       │
响应出去    traceparent: 00-aabb...8899-{new_span}-01
```

### 决策 4：脱敏只覆盖 secret 字段名，不扫 PII

**选择**：正则匹配字段**名**：`*_key` / `*_token` / `*_secret` / `password` / `passwd` / `pwd`，命中则值替换为 `"***"`。不扫日志**值**里的 PII。

**关键代码（22 行）**：
```python
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)^(.*_)?(api_?key|key|token|secret|password|passwd|pwd)$"
)

def _redact_secrets(_logger, _method, event_dict):
    for k in list(event_dict.keys()):
        if isinstance(k, str) and _SECRET_KEY_PATTERN.match(k):
            event_dict[k] = "***"
    return event_dict
```

**为什么不扫值里的 PII**：
- data-copilot 是 Text-to-SQL 工具，prompt 是"销售额最高的客户"、"上季度库存"，PII 概率低
- 扫值需要正则匹配手机号 / 邮箱 / 身份证 / 银行卡，**误伤风险大**（订单号 `13812345678` 长得跟手机号一模一样）
- 真发现问题，W6 做日志降本时再升级

**为什么放在 processor 链里而不是在 log 调用现场手动脱**：靠人手动脱敏 = 早晚会漏。Processor 是**最后一道防线**，哪怕业务代码写了 `log.info("settings dump", **settings.model_dump())`，敏感字段也会被自动 `***`。

---

## 四、值得记录的实现细节

### 4.1 `setup_logging()` 的调用时机

放在 `main.py` 所有 import 之后、所有模块级代码（`log = logging.getLogger(__name__)`、`_BOOT_TIME = time.time()`、`log.info("CORS allow_origins=...")`）之前。

```python
from copilot.security import security_middleware

setup_logging()  # ← 在这里！

log = logging.getLogger(__name__)
_BOOT_TIME = time.time()
```

如果放在 lifespan 里，模块导入期间的日志（CORS 配置、security 配置）会用 stdlib 默认格式输出 —— 这些早期日志在排查启动问题时其实最重要。

### 4.2 `log_context` 用 contextmanager，不用 try/finally 堆代码

```python
@contextmanager
def log_context(**fields):
    tokens = structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
```

`/ask` 端点直接 `with log_context(conversation_id=conversation_id):` 包住整段处理逻辑。比手动 bind / reset 干净。

### 4.3 `/ask/stream` 是特殊情况：异步生成器里直接 bind，不用 with

`/ask/stream` 把执行委托给 `_stream_ask` 这个**异步生成器**。如果在端点函数里用 `with log_context`，`with` 块会在 `return StreamingResponse(...)` 那一刻**立刻退出**，生成器后续 `__anext__()` 时上下文已经被清掉。

解决：在生成器函数体的开头 `structlog.contextvars.bind_contextvars(...)`，不用 `with`。理由：

- Starlette 给每个 HTTP 请求开一个独立的 asyncio task
- 每个 task 有自己独立的 `contextvars` 拷贝
- task 结束时拷贝自然销毁，**不会泄漏到下一个请求**

### 4.4 W3C `traceparent` 解析正则

```python
_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[\da-f]{2})-"
    r"(?P<trace_id>[\da-f]{32})-"
    r"(?P<span_id>[\da-f]{16})-"
    r"(?P<flags>[\da-f]{2})$"
)
```

W3C 规范要求**小写 hex** + **严格固定长度**。还额外校验全零 `trace_id`（W3C 规定为非法），命中也走新生成路径。

---

## 五、踩坑记录

### 坑 1：`structlog` warning — `format_exc_info` 与 ConsoleRenderer 冲突

跑 pytest 时看到：
```
UserWarning: Remove `format_exc_info` from your processor chain
if you want pretty exceptions.
```

**原因**：现代 `ConsoleRenderer` / `JSONRenderer` 自己会处理 `exc_info` 字段并漂亮地渲染 traceback。如果在它们前面就用 `format_exc_info` 把 exception **提前 stringify** 了，渲染器拿到的就是个字符串而不是 exception object，**所有的颜色高亮和折叠都失效**。

**修复**：从 `shared_processors` 里删掉 `structlog.processors.format_exc_info`。保留 `StackInfoRenderer()` 用于显式的 `stack_info=True`。

### 坑 2：在 `/ask/stream` 用 `with log_context` 时缩进改错

我最初想把 `_stream_ask` 异步生成器里的 `async with conversation_lock(...)` 整个用 `with log_context(...)` 包起来，但 `StrReplace` 只改了前 3 行的缩进，导致 `while True` 循环跑到了 lock 外面。

**修复**：放弃 `with log_context` 嵌套，改成在生成器开头直接 `bind_contextvars()`（task 隔离天然清理）。

**教训**：在重构异步生成器时，**先看完整个函数体的缩进结构**再改，不要一次只改局部。

### 坑 3：Uvicorn access log 双写

Uvicorn 默认有自己的 access log handler，跟我们的 root logger 一起会双写。

**修复**：在 `setup_logging()` 里清空 `uvicorn` / `uvicorn.error` / `uvicorn.access` 三个 logger 的 handler，让它们 `propagate=True` 到 root。

```python
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.propagate = True
```

---

## 六、验收

| 验收项 | 结果 |
|---|---|
| structlog 工作 | ✅ |
| Secret 自动脱敏 → `***` | ✅ |
| `log_context` 注入 + 退出清理 | ✅ |
| stdlib bridge（第三方库日志结构化） | ✅ |
| JSON 模式（生产配置） | ✅ |
| W3C `traceparent` 解析 + 生成 | ✅ |
| Ruff lint（含 import 排序） | ✅ |
| Mypy strict | ✅ |
| **现有测试套件 577/577** | ✅ |

---

## 七、这周解锁了什么（给 W4 用）

接 Loki 之前的"裸奔体验"：

```bash
# 跑服务
docker compose up api postgres

# 1. 看一次对话的所有日志
docker compose logs api | jq 'select(.conversation_id=="c-test-001")'

# 2. 看某个 trace 的完整生命周期
docker compose logs api | jq 'select(.trace_id=="aabbccddeeff...")'

# 3. 看 LLM 调用的 token 分布（埋点完整后）
docker compose logs api | jq 'select(.event=="llm.call") | .tokens_in'

# 4. 异常聚合
docker compose logs api | jq 'select(.level=="error") | .event' | sort -u

# 5. 续接上游 trace
curl -v \
  -H "traceparent: 00-aabbccddeeff00112233445566778899-0011223344556677-01" \
  http://localhost:8000/ask \
  -d '{"question":"test","conversation_id":"c-99"}'
# 响应 header 里的 traceparent 会保留 trace_id，只换 span_id
```

W4 接 Loki 后这些查询全部变成 LogQL，shape 一模一样，只是数据持久化 + 全文加速。

---

## 八、Open questions / 留给后续周

| 问题 | 留到哪周 |
|------|---------|
| SLO 数字（P99 < 5s / 99.5%）需要根据真实流量校准 | W2 末或 W8 |
| Histogram 桶设计（默认桶在 LLM 5-30s 延迟范围太粗） | W7 |
| 高频调试日志（如 `coverage_check` 每次都打的 schema dump）是否要采样 | W6 |
| Parent span 当前没记录（W3C 续接时丢了 incoming span_id） | W10 真正上 Trace 时补 |
| LangSmith 跟 OTel 同时跑会不会双写 | W11 OTel SDK 接入时一并验证 |
| PII 脱敏要不要从字段名扩展到字段值 | W6 真发现问题时再决定 |

---

## 九、下周（W4）预告

把 Loki + Promtail + Grafana 起到 `docker-compose.observability.yml`，让上面那堆 `jq` 查询通过 Grafana Explore 跑起来，**第一次拥有"日志后端"**。

具体改动点：
- 新增 `docker-compose.observability.yml`（独立文件，按需启）
- 新增 `infra/loki/loki-config.yaml`
- 新增 `infra/promtail/promtail-config.yaml`
- Grafana datasource provisioning
- 配两个 Loki label：`service`、`level`（**注意不要把高基数字段当 label**）

---

*Reviewed by: Rachel · Implementation commit: `0ae451a`*
