# WebSocket v2 接口文档 — 逐句打字机流式

> 端点：`ws://<host>:8001/api/v2/ws/partners/{partner_id}/chat`  
> 版本：2026-08-10  
> 认证：**已关闭**（auth.enabled = false），无需 token

---

## 1. 接口概述

AI 完整回复被拆成独立句子，按打字节奏逐句推送，模拟真人聊天体验。

### 1.1 端点

| 端点 | 说明 |
|---|---|
| `ws://<host>:8001/api/v2/ws/partners/{partner_id}/chat` | **推荐**，partner_id 写在 URL 路径中 |
| `ws://<host>:8001/api/v2/ws/chat` | 等价端点，partner_id 默认 `lisa` |

### 1.2 路径参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `partner_id` | string | 是 | AI 伙伴 ID，如 `lisa`、`tutor_math` |

### 1.3 认证

当前环境认证已关闭，**直接连接即可，无需 token**。

---

## 2. 客户端 → 服务端

所有消息使用 JSON 文本帧。

### 请求格式

```json
{
  "user_id": "user42",
  "content": "三年级小朋友不爱学习英语"
}
```

### 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `content` | string | **是** | — | 用户输入文本 |
| `user_id` | string | 否 | `anon_xxxxxx` | 用户标识。建议传入，用于会话关联 |
| `partner_id` | string | 否 | URL 路径值 / `"lisa"` | AI 伙伴 ID |
| `chat_id` | string | 否 | `c2c_{partner_id}_{user_id}` | 会话 ID，空值自动按 partner+user 关联 |
| `message_id` | string | 否 | `m_xxxxxxxxxxxx` | 本轮消息 ID |

---

## 3. 服务端 → 客户端

扁平 JSON，`type` 字段区分事件类型。

### 3.1 事件类型

| type | 说明 |
|---|---|
| `ack` | 消息已接收，开始处理 |
| `patch` | 逐句推送的回复片段（独立句，无重叠） |
| `finish` | 本轮结束，附带完整全文 |
| `error` | 错误 |
| `pong` | 心跳响应 |

### 3.2 ack — 接收确认

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
| `turn_id` | string | 预留给未来扩展，当前为空字符串 |
| `chat_id` | string | 实际使用的会话 ID |

### 3.3 patch — 逐句推送

每条 patch 的 `content` 是**独立完整句**，客户端直接 append 即可。

```json
{
  "type": "patch",
  "message_id": "m_a1b2c3d4e5f6",
  "seq": 1,
  "content": "三年级确实是个让家长头疼的坎儿~"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 固定 `"patch"` |
| `message_id` | string | 本轮消息 ID |
| `seq` | int | 序号，从 1 开始递增 |
| `content` | string | 当前这句的完整文本，不包含之前推送的内容 |

### 3.4 finish — 结束

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
| `content` | string | AI 回复的**完整全文**，可作为最终落库内容 |
| `finish_reason` | string | 正常为 `"stop"` |

### 3.5 error — 错误

```json
{
  "type": "error",
  "message_id": "m_a1b2c3d4e5f6",
  "code": "SEND_FAILED",
  "msg": "partner send_message failed: ..."
}
```

| code | 说明 |
|---|---|
| `EMPTY_TEXT` | 客户端发了空文本 |
| `PARTNER_DOWN` | partner 启动失败或不存在 |
| `SEND_FAILED` | partner 调用 LLM 失败 |
| `EMPTY_REPLY` | partner 返回了空回复 |
| `CANCELLED` | 本轮被客户端取消 |
| `UNKNOWN_EVENT` | 无法识别的事件类型 |
| `INTERNAL` | 服务端内部异常 |

### 3.6 pong — 心跳

```json
{ "type": "pong" }
```

---

## 4. 控制消息

| 操作 | 发送格式 | 效果 |
|---|---|---|
| 取消当前轮 | `{"type": "cancel"}` | 终止正在进行的回复 |
| 心跳探活 | `{"type": "ping"}` | 服务端返回 `{"type":"pong"}` |

同一连接上连续发新消息会**自动取消**上一轮未完成的回复。

---

## 5. 消息分割与延迟

**分割流程**：
1. 按 `\n\n`（空行）分段
2. 每段按句末标点（`。！？!?；;`）切句
3. 英文段落额外处理 `.!?` 后接大写字母也算句末
4. 保证每条 patch ≥ 1 字符

**打字延迟**：

```
delay = max(1000ms, 句长 × 150ms) ± 12% 抖动
```

约 **6-7 字/秒**，比正常打字快一倍。

---

## 6. 完整交互示例

### 发送

```json
{ "user_id": "u_demo", "content": "你好，能简单介绍一下自己吗" }
```

### 接收（逐条）

```json
{ "type": "ack", "message_id": "m_74aca02ad486", "status": "accepted", "turn_id": "", "chat_id": "c2c_lisa_u_demo" }

{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 1, "content": "在呢～我是Lisa老师" }

{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 2, "content": "咱们雪梨英语的班主任" }

{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 3, "content": "平时主要是帮您看看孩子在英语学习上有没有什么卡壳的地方" }

{ "type": "patch", "message_id": "m_74aca02ad486", "seq": 4, "content": "您家宝贝现在几年级啦？" }

{ "type": "finish", "message_id": "m_74aca02ad486", "content": "在呢～我是Lisa老师\n\n咱们雪梨英语的班主任\n\n平时主要是帮您看看孩子在英语学习上有没有什么卡壳的地方\n\n您家宝贝现在几年级啦？", "finish_reason": "stop" }
```

---

## 7. 调用示例

### Python

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
            t = evt["type"]
            if t == "ack":
                print(f"✅ ack  msg={evt['message_id']}")
            elif t == "patch":
                print(f"📝 patch #{evt['seq']}  {evt['content']}")
            elif t == "finish":
                print(f"🏁 finish  全文:\n{evt['content']}")
                break
            elif t == "error":
                print(f"❌ error  {evt['code']}: {evt['msg']}")
                break

asyncio.run(main())
```

### JavaScript（浏览器）

```javascript
const ws = new WebSocket('ws://127.0.0.1:8001/api/v2/ws/partners/lisa/chat');
let accumulated = '';

ws.onopen = () => {
  ws.send(JSON.stringify({ user_id: 'u_demo', content: '你好，Lisa！' }));
};

ws.onmessage = (evt) => {
  const msg = JSON.parse(evt.data);
  switch (msg.type) {
    case 'ack':
      accumulated = '';
      break;
    case 'patch':
      accumulated += msg.content;
      // messageEl.textContent = accumulated;
      break;
    case 'finish':
      console.log('完成:', msg.content);
      break;
    case 'error':
      console.error('错误:', msg.code, msg.msg);
      break;
  }
};
```

---

*文档基于代码实际实现生成，最后同步：2026-08-10*
