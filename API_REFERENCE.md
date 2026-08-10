# DeepTutor Chat & WebSocket 接口文档

> 版本：2026-08-10  
> Base URL：`http://<host>:8001`  
> 所有 REST 接口返回 JSON；所有 WebSocket 传输使用 JSON 文本帧。

---

## 目录

- [1. 通用说明](#1-通用说明)
- [2. Partner 生命周期](#2-partner-生命周期)
- [3. REST Chat 接口](#3-rest-chat-接口)
  - [3.1 同步聊天 — POST /api/v1/partners/{partner_id}/chat](#31-同步聊天--post-apiv1partnerspartner_idchat)
  - [3.2 SSE 流式聊天 — POST /api/v1/partners/{partner_id}/chat/execute-stream](#32-sse-流式聊天--post-apiv1partnerspartner_idchatexecute-stream)
- [4. WebSocket v1 — 面向 Web 前端](#4-websocket-v1--面向-web-前端)
  - [4.1 端点](#41-端点)
  - [4.2 客户端 → 服务端消息](#42-客户端--服务端消息)
  - [4.3 服务端 → 客户端事件](#43-服务端--客户端事件)
- [5. WebSocket v2 — 逐句打字机流式（推荐对接第三方）](#5-websocket-v2--逐句打字机流式推荐对接第三方)
  - [5.1 端点](#51-端点)
  - [5.2 客户端 → 服务端消息](#52-客户端--服务端消息)
  - [5.3 服务端 → 客户端事件](#53-服务端--客户端事件)
  - [5.4 控制消息](#54-控制消息)
  - [5.5 消息分割与延迟策略](#55-消息分割与延迟策略)
  - [5.6 交互时序](#56-交互时序)
- [6. 接口对照表](#6-接口对照表)

---

## 1. 通用说明

### 1.1 认证

所有 WebSocket 端点在 `ws.accept()` 之前会调用 `ws_require_auth` 进行认证。REST 接口通过 `/api/v1/auth/login` 登录后在请求头携带 `Authorization: Bearer <token>`。

### 1.2 Session / Chat 概念

| 字段 | 说明 |
|---|---|
| `partner_id` | AI 伙伴 ID，如 `lisa`、`tutor_math` |
| `session_id` | 会话唯一标识；REST 接口中同 `chat_id`，WebSocket v2 中空值自动生成 `c2c_{partner_id}_{user_id}` |
| `chat_id` | REST 兼容字段，语义同 session_id |
| `session_key` | 可选的会话键，用于从外部系统关联会话（如飞书 channel + 用户的组合键） |
| `user_id` | 最终用户标识；WebSocket v2 中不填会自动生成 `anon_xxxxxx` |

### 1.3 附件

REST 和 WebSocket v1 均支持附件上传，结构如下：

```json
{
  "attachments": [
    {
      "type": "image",
      "filename": "homework.png",
      "mime_type": "image/png",
      "base64": "iVBORw0KGgoAAAANSUhEUgAA..."
    }
  ]
}
```

> WebSocket v2 暂不支持附件。

### 1.4 Partner 必须先启动

在调用 chat 接口前，partner 实例必须处于 running 状态。REST chat 接口内部会自动调用 `_ensure_running_partner`，首次调用时如未启动会等待启动完成；但为了减少首包延迟，建议显式调用启动接口。

---

## 2. Partner 生命周期

### 启动 Partner

```
POST /api/v1/partners/{partner_id}/start
```

**请求体**（可选，空 JSON `{}` 也可以）：

```json
{ "capability": null }
```

**响应**：

```json
{
  "partner_id": "lisa",
  "status": "running"
}
```

### 停止 Partner

```
POST /api/v1/partners/{partner_id}/stop
```

### 查询 Partner 列表

```
GET /api/v1/partners
```

**响应**：

```json
[
  {
    "id": "lisa",
    "name": "Lisa老师",
    "status": "running",
    "capability": "parent_tutor"
  }
]
```

---

## 3. REST Chat 接口

### 3.1 同步聊天 — `POST /api/v1/partners/{partner_id}/chat`

一次性返回完整回复，适合不需要流式体验的场景。

**请求体**：

```json
{
  "content": "三年级小朋友不爱学习英语",
  "session_id": "c2c_lisa_u42",
  "session_key": "feishu_oc_abc123:ou_user42",
  "chat_id": "c2c_lisa_u42",
  "llmSelection": null
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `content` | string | **是** | — | 用户输入文本；可与 attachments 二选一 |
| `session_id` | string | 否 | — | 会话 ID，留空自动创建新会话 |
| `chat_id` | string | 否 | — | 等价字段，session_id 优先 |
| `session_key` | string | 否 | — | 外部会话关联键 |
| `attachments` | array | 否 | `[]` | 附件列表（见 1.3） |
| `llmSelection` | object | 否 | — | 指定 LLM 覆盖默认选择 |

**响应**（HTTP 200）：

```json
{
  "partner_id": "lisa",
  "session_id": "c2c_lisa_u42",
  "content": "三年级确实是个让家长头疼的坎儿~ 您家宝贝具体是哪种表现呀？"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `partner_id` | string | 回显请求中的 partner_id |
| `session_id` | string | 实际使用的会话 ID（自动生成或回显） |
| `content` | string | AI 完整回复文本 |

**错误响应**：

| HTTP | detail |
|---|---|
| 400 | `"content is required"` — content 和 attachments 都为空 |
| 404 | `"partner 'xxx' not found"` — partner_id 不存在 |

**cURL 示例**：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/partners/lisa/chat \
  -H "Content-Type: application/json" \
  -d '{"content":"你好 Lisa","session_id":"test001"}'
```

**Python 示例**：

```python
import httpx
resp = httpx.post(
    "http://127.0.0.1:8001/api/v1/partners/lisa/chat",
    json={"content": "你好 Lisa", "session_id": "test001"},
    timeout=60,
)
print(resp.json()["content"])
```

---

### 3.2 SSE 流式聊天 — `POST /api/v1/partners/{partner_id}/chat/execute-stream`

以 Server-Sent Events 方式流式返回完整的推理轨迹（thinking → content → done）。

**请求体**：同 [3.1](#31-同步聊天--post-apiv1partnerspartner_idchat)。

**响应头**：

```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
```

**SSE 事件流**（每行一条，`data:` 前缀）：

```
event: session
data: {"partner_id":"lisa","session_id":"c2c_lisa_u42"}

event: thinking
data: {"content":"用户问的是三年级英语学习问题..."}

event: thinking
data: {"content":"Lisa 的角色是雪梨英语班主任，需要共情+引导..."}

event: content
data: {"content":"三年级确实是个让家长头疼的坎儿~ 您家宝贝具体是哪种表现呀？"}

event: done
data: {"partner_id":"lisa","session_id":"c2c_lisa_u42"}
```

**事件类型**：

| event | payload | 说明 |
|---|---|---|
| `session` | `{ partner_id, session_id }` | 首包，告知客户端会话信息 |
| `thinking` | `{ content }` | LLM 推理过程（仅当 partner 开启了 thinking 输出） |
| `content` | `{ content }` | AI 完整回复文本（只发一次，最终答案） |
| `done` | `{ partner_id, session_id }` | 结束标记 |
| `error` | `{ detail }` | 错误 |

**JavaScript 示例**：

```javascript
const resp = await fetch('http://127.0.0.1:8001/api/v1/partners/lisa/chat/execute-stream', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ content: '你好 Lisa', session_id: 'test001' }),
});

const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });

  const lines = buffer.split('\n');
  buffer = lines.pop();

  let currentEvent = '';
  for (const line of lines) {
    if (line.startsWith('event:')) currentEvent = line.slice(6).trim();
    else if (line.startsWith('data:')) {
      const data = JSON.parse(line.slice(5).trim());
      if (currentEvent === 'thinking') console.log('💭', data.content);
      if (currentEvent === 'content') console.log('📝', data.content);
      if (currentEvent === 'done') console.log('🏁 完成');
    }
  }
}
```

---

## 4. WebSocket v1 — 面向 Web 前端

完整 Web 聊天端点，实时输出 LLM 推理轨迹、工具调用、流式内容等。与 DeepTutor 前端产品直接对接。

### 4.1 端点

```
ws://<host>:8001/api/v1/partners/{partner_id}/ws
```

### 4.2 客户端 → 服务端消息

#### 发送用户消息

```json
{
  "content": "三年级小朋友不爱学习英语",
  "session_id": "c2c_lisa_u42",
  "chat_id": "web",
  "session_key": null,
  "attachments": []
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string | **是** | 用户输入 |
| `session_id` | string | 否 | 会话 ID |
| `chat_id` | string | 否 | 默认 `"web"` |
| `session_key` | string | 否 | 外部关联键 |
| `attachments` | array | 否 | 附件列表 |

#### 控制消息

| action | 说明 |
|---|---|
| （无 action 字段） | 发送用户消息（默认行为） |
| `"action": "stop"` | 停止当前正在进行的回复 |
| `"action": "attach"` | 重连并重新订阅（页面刷新后恢复未完成的 turn） |

重连示例：

```json
{ "action": "attach", "session_id": "c2c_lisa_u42" }
```

### 4.3 服务端 → 客户端事件

所有事件为扁平 JSON，通过 `type` 字段区分。

| type | 说明 |
|---|---|
| `user_echo` | 重连恢复时先把用户的原始输入回显给前端 |
| `resuming` | 重连恢复中的标记 |
| `stream_event` | 核心流式事件 — 包含 thinking、tool_call、content chunk、sources 等完整轨迹 |
| `content` | AI 最终完整回复 |
| `done` | 本轮完成 |
| `stopped` | 被用户 stop 指令终止 |
| `proactive` | Partner 主动推送（定时关怀等） |
| `error` | 错误 |

**stream_event 示例**：

```json
{
  "type": "stream_event",
  "event": {
    "type": "thinking",
    "content": "让我想想这个问题该怎么回答..."
  }
}
```

```json
{
  "type": "stream_event",
  "event": {
    "type": "content",
    "content": "三年级确实是个让家长头疼的坎儿~"
  }
}
```

**完整完成示例**：

```json
{ "type": "content", "content": "完整回复..." }
{ "type": "done" }
```

---

## 5. WebSocket v2 — 逐句打字机流式（推荐对接第三方）

面向第三方集成的简洁协议。把 AI 完整回复拆成独立句子，按打字节奏逐句推送。适合嵌入到飞书、企微、App 等场景中，模拟真人聊天的打字感。

### 5.1 端点

两个完全等价，任选其一：

| 端点 | 说明 |
|---|---|
| `ws://<host>:8001/api/v2/ws/partners/{partner_id}/chat` | **推荐**，partner_id 在 URL 路径中 |
| `ws://<host>:8001/api/v2/ws/chat` | partner_id 从消息中取，默认 `lisa` |

### 5.2 客户端 → 服务端消息

**支持多种格式**，越精简越好。所有字段都有自动默认值。

#### 格式一：极简（推荐）

```json
{ "content": "三年级小朋友不爱学习英语" }
```

也兼容旧字段名 `text`：

```json
{ "text": "三年级小朋友不爱学习英语" }
```

#### 格式二：简化信封

```json
{
  "user_id": "user42",
  "chat_id": "c2c_user42",
  "content": "三年级小朋友不爱学习英语"
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `content` / `text` | string | **是** | — | 用户输入文本 |
| `partner_id` | string | 否 | URL 路径值 / `"lisa"` | AI 伙伴 ID |
| `chat_id` | string | 否 | `c2c_{partner_id}_{user_id}` | 会话 ID，空值时自动按 partner+user 关联 |
| `user_id` | string | 否 | `anon_xxxxxx` | 用户标识 |
| `message_id` | string | 否 | `m_xxxxxxxxxxxx` | 本轮消息 ID |

#### 格式三：旧飞书格式（完全向后兼容）

```json
{
  "schema": "2.0",
  "header": { "event_type": "im.message.receive_v1" },
  "event": {
    "message": {
      "message_id": "m_old_001",
      "content": "{\"text\":\"三年级小朋友不爱学习英语\"}"
    },
    "sender": {
      "sender_id": { "open_id": "legacy_user_abc" }
    }
  }
}
```

旧格式中的 `open_id` 会被自动映射为 `user_id`。

### 5.3 服务端 → 客户端事件

统一扁平 JSON 结构，通过 `type` 字段区分。

#### 事件总览

| type | 说明 |
|---|---|
| `ack` | 消息已接收，开始处理 |
| `patch` | 逐句推送的回复片段（独立句，无重叠） |
| `finish` | 本轮回复结束，附带完整全文 |
| `error` | 错误 |
| `pong` | 心跳响应 |

#### ack — 接收确认

```json
{
  "type": "ack",
  "message_id": "m_a1b2c3d4e5f6",
  "status": "accepted",
  "turn_id": "",
  "chat_id": "c2c_lisa_user42"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 固定 `"ack"` |
| `message_id` | string | 本轮消息 ID（客户端传了就回显，没传就自动生成） |
| `status` | string | 固定 `"accepted"` |
| `turn_id` | string | 轮次 ID（当前为空字符串，预留给未来扩展） |
| `chat_id` | string | 实际使用的会话 ID |

#### patch — 逐句推送

```json
{
  "type": "patch",
  "message_id": "m_a1b2c3d4e5f6",
  "seq": 1,
  "content": "三年级确实是个让家长头疼的坎儿~"
}
```

```json
{
  "type": "patch",
  "message_id": "m_a1b2c3d4e5f6",
  "seq": 2,
  "content": "您家宝贝具体是哪种表现呀？"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 固定 `"patch"` |
| `message_id` | string | 本轮消息 ID |
| `seq` | int | 序号，从 1 开始递增 |
| `content` | string | **当前这句的完整文本**，不包含之前推送的内容 |

> **重要**：每条 patch 的 `content` 是**独立完整句**，客户端只需要 append 即可，不需要做 diff 或增量拼接。

#### finish — 结束

```json
{
  "type": "finish",
  "message_id": "m_a1b2c3d4e5f6",
  "content": "三年级确实是个让家长头疼的坎儿~\n\n您家宝贝具体是哪种表现呀？",
  "finish_reason": "stop"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 固定 `"finish"` |
| `message_id` | string | 本轮消息 ID |
| `content` | string | AI 回复的**完整全文**（可作为最终落库内容） |
| `finish_reason` | string | 结束原因，正常为 `"stop"` |

#### error — 错误

```json
{
  "type": "error",
  "message_id": "m_a1b2c3d4e5f6",
  "code": "SEND_FAILED",
  "msg": "partner send_message failed: ..."
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 固定 `"error"` |
| `message_id` | string | 本轮消息 ID（可能为 `"?"` 表示无法识别） |
| `code` | string | 错误码 |
| `msg` | string | 人类可读的错误信息 |

**错误码**：

| code | 说明 |
|---|---|
| `EMPTY_TEXT` | 客户端发了空文本 |
| `PARTNER_DOWN` | 指定 partner 启动失败或不存在 |
| `SEND_FAILED` | partner 内部调用 LLM 失败 |
| `EMPTY_REPLY` | partner 返回了空回复 |
| `CANCELLED` | 本轮被客户端取消 |
| `UNKNOWN_EVENT` | 无法识别的事件类型 |
| `INTERNAL` | 服务端内部异常 |

#### pong — 心跳

```json
{ "type": "pong" }
```

### 5.4 控制消息

| 触发条件 | 发送格式 | 效果 |
|---|---|---|
| 发送消息 | 见 [5.2](#52-客户端--服务端消息) | 触发一轮新对话 |
| 取消当前轮 | `{"type": "cancel"}` 或 `{"header":{"event_type":"im.cancel_v1"}}` | 终止正在进行的回复 |
| 心跳 ping | `{"type": "ping"}` 或 `{"header":{"event_type":"im.ping_v1"}}` | 服务端返回 `{"type":"pong"}` |
| 关闭连接 | `{"header":{"event_type":"im.close_v1"}}` | 服务端断开连接 |

> **空闲心跳**：连接空闲 120 秒后，服务端会发送旧飞书格式的 ping（`{"schema":"2.0","header":{"event_type":"im.ping_v1"}}`），客户端应忽略它或回复任意 ping/pong。

### 5.5 消息分割与延迟策略

**分割策略**：
1. 先按 `\n\n`（空行）分段
2. 每段内部再按句末标点（`。！？!?；;`）切句
3. 英文段落额外处理：`.!?` 后接大写字母/引号/括号也算句末
4. 保证每条 patch 至少 1 个字符

**打字延迟**：

```
delay = max(1000ms, 句长 × 150ms) ± 12% 抖动
```

- 最少 **1 秒**，保证节奏自然
- 约 **6-7 字/秒**，比正常打字快一倍
- 加入 ±12% 随机抖动，避免机械感

### 5.6 交互时序

```
客户端                                   服务端
  │                                        │
  │──── WebSocket connect ────────────────►│  认证 + accept
  │                                        │
  │──── {"content":"你好"} ────────────────►│
  │                                        │─── 加载 partner / SOUL.md
  │◀─── ack ───────────────────────────────│
  │                                        │─── 调用 LLM 拿完整回复
  │◀─── patch seq=1 "在呢～我是Lisa老师" ───│
  │                                        │    (等待 ~1.5s)
  │◀─── patch seq=2 "咱们雪梨英语的班主任" ──│
  │                                        │    (等待 ~2s)
  │◀─── patch seq=3 "您家宝贝现在几年级啦？" │
  │                                        │
  │◀─── finish (完整全文) ──────────────────│
  │                                        │
```

### 5.7 完整交互示例

**请求**：
```json
{ "user_id": "u_demo", "content": "你好，能简单介绍一下自己吗" }
```

**服务端返回**（逐条收到）：

```json
// 1. ack
{ "type": "ack", "message_id": "m_74aca02ad486", "status": "accepted",
  "turn_id": "", "chat_id": "c2c_lisa_u_dbg" }

// 2. patch #1 — 独立句
{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 1,
  "content": "在呢～我是Lisa老师" }

// 3. patch #2 — 独立句
{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 2,
  "content": "咱们雪梨英语的班主任" }

// 4. patch #3
{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 3,
  "content": "平时主要是帮您看看孩子在英语学习上有没有什么卡壳的地方" }

// 5. patch #4
{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 4,
  "content": "您家宝贝现在几年级啦？" }

// 6. finish — 完整全文
{ "type": "finish", "message_id": "m_74aca02ad486",
  "content": "在呢～我是Lisa老师\n\n咱们雪梨英语的班主任\n\n平时主要是帮您看看孩子在英语学习上有没有什么卡壳的地方\n\n您家宝贝现在几年级啦？",
  "finish_reason": "stop" }
```

### 5.8 JavaScript 调用示例

```javascript
const ws = new WebSocket('ws://127.0.0.1:8001/api/v2/ws/partners/lisa/chat');

ws.onopen = () => {
  ws.send(JSON.stringify({
    user_id: 'u_demo',
    content: '你好，Lisa！'
  }));
};

ws.onmessage = (evt) => {
  const msg = JSON.parse(evt.data);
  switch (msg.type) {
    case 'ack':
      console.log('✅ 已接收', msg.message_id, '会话:', msg.chat_id);
      break;
    case 'patch':
      console.log(`📝 patch #${msg.seq}: ${msg.content}`);
      // UI: messageEl.textContent += msg.content;
      break;
    case 'finish':
      console.log('🏁 完成，共收到完整全文');
      console.log('全文:', msg.content);
      break;
    case 'error':
      console.error('❌', msg.code, msg.msg);
      break;
    case 'pong':
      // 心跳响应，忽略
      break;
  }
};

ws.onerror = (err) => console.error('WS error:', err);
ws.onclose = () => console.log('WS closed');
```

### 5.9 Python 调用示例

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect(
        "ws://127.0.0.1:8001/api/v2/ws/partners/lisa/chat"
    ) as ws:
        await ws.send(json.dumps({
            "user_id": "u_demo",
            "content": "你好，Lisa！"
        }))
        while True:
            evt = json.loads(await ws.recv())
            if evt["type"] == "ack":
                print(f"✅ ack  msg={evt['message_id']}")
            elif evt["type"] == "patch":
                print(f"📝 patch #{evt['seq']}  {evt['content']}")
            elif evt["type"] == "finish":
                print(f"🏁 finish  全文:\n{evt['content']}")
                break
            elif evt["type"] == "error":
                print(f"❌ error  {evt['code']}: {evt['msg']}")
                break

asyncio.run(main())
```

### 5.10 并发与取消

- 同一连接上连续发多条消息时，上一轮未完成的回复会被**自动取消**（相当于隐式 cancel）
- 主动取消可发送 `{"type":"cancel"}`
- 取消后会收到一条 `error` 事件，`code: "CANCELLED"`

---

## 6. 接口对照表

### 按场景选择

| 场景 | 推荐接口 | 说明 |
|---|---|---|
| 内部服务调用、不需要流式 | `POST /api/v1/partners/{id}/chat` | 同步 REST，一次拿完整回复 |
| 前端网页、需要 LLM 完整 trace | `ws://.../api/v1/partners/{id}/ws` | v1 WebSocket，输出 thinking/tool_call/content 全量轨迹 |
| 第三方对接、打字机效果 | `ws://.../api/v2/ws/partners/{id}/chat` | **v2 WebSocket**，逐句流式，极简协议 |
| HTTP + 流式 | `POST /api/v1/partners/{id}/chat/execute-stream` | SSE，不依赖 WebSocket |

### 接口清单

| # | 方法 | 路径 | 类型 | 核心特点 |
|---|---|---|---|---|
| 1 | POST | `/api/v1/partners/{partner_id}/start` | REST | 启动 partner 实例 |
| 2 | POST | `/api/v1/partners/{partner_id}/stop` | REST | 停止 partner 实例 |
| 3 | GET | `/api/v1/partners` | REST | 列出所有 partner |
| 4 | POST | `/api/v1/partners/{partner_id}/chat` | REST | 同步聊天，一次返回完整回复 |
| 5 | POST | `/api/v1/partners/{partner_id}/chat/execute-stream` | REST (SSE) | HTTP 流式，服务端推 SSE 事件 |
| 6 | WS | `/api/v1/partners/{partner_id}/ws` | WebSocket v1 | 产品前端用，输出完整 LLM trace |
| 7 | WS | `/api/v2/ws/partners/{partner_id}/chat` | WebSocket v2 | **第三方推荐**，逐句打字机流式 |
| 8 | WS | `/api/v2/ws/chat` | WebSocket v2 | 同上，partner_id 从信封取 |

---

## 7. 附录：字段名对照历史

本项目经历了多轮协议迭代，以下是字段名映射表，便于理解兼容性设计：

| 概念 | WebSocket v2 当前字段 | 旧飞书格式 | 说明 |
|---|---|---|---|
| 用户消息文本 | `content` / `text` | `event.message.content.text` | v2 同时接受 content 和 text |
| 用户标识 | `user_id` | `event.sender.sender_id.open_id` | v2 自动把 open_id 映射为 user_id |
| 轮次消息 ID | `message_id` | `event.message.message_id` | — |
| 会话 ID | `chat_id` | — | — |
| 事件类型 | `type` | `header.event_type` | v2 用扁平 type 字段 |
| patch 内容 | `content` | — | v2 patch 是独立完整句 |

---

*文档基于代码实际实现生成，最后同步：2026-08-10*
