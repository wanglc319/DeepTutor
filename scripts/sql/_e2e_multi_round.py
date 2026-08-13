"""saleChat 多轮会话 E2E 测试 (HTTP 层 + 真实 Shirley MCP)

流程:
  1. 文本 → 2. 文本 → 3. 图片（多模态）→ 4. 文本（续聊）
  每轮都走完整链路: 5.1画像查询 → 5.2历史 → 销售策略 → 6.回复推送 → 5.3画像写回
  指标: 所有 MCP 接口都调通 (get_user_profile / query_user_profile_chat_history /
        mark_qywx_customer_tags / reply_lisa_message / save_user_profile_analysis)
"""
import asyncio, json, logging, re, sys, time, threading, queue
from collections import defaultdict

# ── 日志采集: 抓 Shirley MCP 调用 ──
_mcp_calls = defaultdict(list)  # tool_name -> [ {ts, ok, elapsed_ms, err} ]
_mcp_lock = threading.Lock()

_history_logs: list[str] = []
_reply_logs: list[str] = []
_fallback_hits: list[str] = []
_soul_logs: list[str] = []
_soul_fallback_hits: list[str] = []
_sent_texts: list[str] = []
_kb_logs: list[str] = []
_live_logs: list[str] = []
_vision_logs: list[str] = []
_kb_degrade_logs: list[str] = []
_reject_push_logs: list[str] = []


class _BizCapture(logging.Handler):
    """抓业务关键日志: 动态 soul / 历史正序 / LLM 回复 / 兜底触发 / KB / 直播"""
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "sale_chat.soul]" in msg:
            if "loaded SOUL.md" in msg:
                _soul_logs.append(msg)
            else:
                _soul_fallback_hits.append(msg)
        elif "_fetch_history]" in msg and "turns=" in msg:
            _history_logs.append(msg)
        elif "_llm_reply ←✓]" in msg:
            _reply_logs.append(msg)
        elif "sale_chat.fallback]" in msg:
            _fallback_hits.append(msg)
        elif "sale_chat.kb_degrade" in msg:
            _kb_degrade_logs.append(msg)
        elif "sale_chat.push_reject" in msg:
            _reject_push_logs.append(msg)
        elif "sale_chat.kb" in msg:
            _kb_logs.append(msg)
        elif "[live]" in msg:
            _live_logs.append(msg)
        elif "sale_chat.vision" in msg or "sale_chat.image" in msg:
            _vision_logs.append(msg)


class _McpCapture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "[Shirley MCP" not in msg:
            return
        # 解析模式: [Shirley MCP →] tool_name | args=...
        #           [Shirley MCP ←✓] tool_name | elapsed_ms=NNN | result=...
        #           [Shirley MCP ←✗] tool_name | elapsed_ms=NNN | ...
        if "→]" in msg:
            tool = msg.split("→]")[1].split("|")[0].strip()
            with _mcp_lock:
                _mcp_calls[tool].append({"ts": time.time(), "dir": "→"})
            # 从 reply_lisa_message 的入参里抠出实际推送的句子
            if tool == "reply_lisa_message" and "args=" in msg:
                raw = msg.split("args=", 1)[1]
                m = re.search(r"['\"]content['\"]\s*:\s*['\"](.*?)['\"]\s*[,}]", raw)
                if m:
                    _sent_texts.append(m.group(1))
        elif "←" in msg:
            tool = msg.split("]")[1].split("|")[0].strip() if "]" in msg else "?"
            ok = "←✓" in msg
            elapsed = 0
            for part in msg.split("|"):
                part = part.strip()
                if part.startswith("elapsed_ms="):
                    try: elapsed = int(part.split("=")[1])
                    except: pass
            err = None
            if not ok:
                for part in msg.split("|"):
                    part = part.strip()
                    if part.startswith("tool_isError="):
                        err = part.split("=",1)[1]
                        break
            with _mcp_lock:
                if _mcp_calls[tool] and _mcp_calls[tool][-1].get("dir") == "→":
                    _mcp_calls[tool][-1].update(ok=ok, elapsed_ms=elapsed, err=err)

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(levelname)-7s %(name)s: %(message)s")
logging.getLogger("deeptutor.services.shirley.client").addHandler(_McpCapture())
logging.getLogger("deeptutor.services.partners.sale_chat").addHandler(_BizCapture())
logging.getLogger("deeptutor.sales.actions").addHandler(_BizCapture())
# 压掉 httpcore/httpx 的 DEBUG 噪音，只留业务日志
for n in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection",
          "httpcore.proxy", "uvicorn", "starlette", "apscheduler", "mcp",
          "openai", "asyncio"):
    logging.getLogger(n).setLevel(logging.WARNING)

# ── 环境准备 ──
from deeptutor.services.config import ensure_runtime_settings_files, export_runtime_settings_to_env
ensure_runtime_settings_files(); export_runtime_settings_to_env(overwrite=True)

from deeptutor.api.main import app
from deeptutor.api.routers import sale_chat as sc_router
from deeptutor.services.partners import sale_chat as sc_svc

# 缩短抖动窗口加快测试（生产是 10s）
sc_svc.DEBOUNCE_SECONDS = 2.0
from fastapi.testclient import TestClient

# 用用户给的真实参数
REAL_PRIME = {
    "thirdSaleUuid": "7193d1f0b350ed5f80573a079758cb60",
    "thirdUserId": 7881300293924638,
    "originalUserId": "wm5oClDAAApbXPEi1o0HjaS6ScAbpBtQ",
    "customerName": "超人不会流眼泪",
    "corpid": "ww4d3304c455d3b6d1",
    "vid": 1688854828862873,
    "qywxUserid": "WenHui",
    "isProd": True,
}

PATH = "/api/v1/partners/lisa/saleChat"

# ── 多轮测试脚本: AI 英语教育真实销售场景, 意向度逐轮升温 ──
# 覆盖画像字段: child_name / grade / pain_points / level_self_report
#              school_english_start / external_classes / price_sensitivity
#              coaching_ability / available_time / decision_makers
ROUNDS = [
    {  # Round 1: 首次接触 — 报娃昵称 + 年级 + 英语痛点
        "name": "Round 1 文本-开场+娃昵称年级+背单词痛点",
        "payload": {
            "content": "你好 Lisa 老师，我家孩子小名叫豆包，今年三年级。"
                       "他英语单词总是背了就忘，上次单元测只考了 62 分，愁人",
            "session_id": None, "msgType": 2, "reqType": "chat",
            "primeInfo": REAL_PRIME,
        },
    },
    {  # Round 2: 加深痛点 — 发音/听力 + 学校进度 + 家长辅导能力
        "name": "Round 2 文本-发音听力差+我自己辅导不了",
        "payload": {
            "content": "而且他不敢开口读，音标也没学过，听力基本靠猜。"
                       "学校三年级才开始上英语，进度赶不上。我自己英语也一般，"
                       "想辅导也不知道从哪下手",
            "session_id": None, "msgType": 2, "reqType": "chat",
            "primeInfo": REAL_PRIME,
        },
    },
    {  # Round 3: msgType=101 图片消息 — content 是图片 URL, 走多模态识别
        "name": "Round 3 图片-msgType=101多模态识别",
        "payload": {
            "content": "http://wx.qlogo.cn/mmhead/Q3auHgzwzM7hyc99efRvCKJLO4uQP0ATZRdFibXJ4vBXG86QzM123dQ/0",
            "session_id": None, "msgType": 101, "reqType": "chat",
            "primeInfo": REAL_PRIME,
        },
    },
    {  # Round 4: 报过班 + 价格敏感 + 决策人
        "name": "Round 4 文本-报过线下班+问价格+要和孩子爸商量",
        "payload": {
            "content": "之前在外面报过线下英语班，一年两万多，效果一般就没续。"
                       "你们这个 AI 课怎么上啊？大概多少钱？我得跟孩子爸商量一下",
            "session_id": None, "msgType": 2, "reqType": "chat",
            "primeInfo": REAL_PRIME,
        },
    },
    {  # Round 5: 高意向 — 问上课时间 + 想试听（应升温到 warm/hot）
        "name": "Round 5 文本-高意向+问时间+想试听",
        "payload": {
            "content": "听起来还不错。我们平时就周末和晚上七点后有空，"
                       "能不能先安排个试听看看孩子适不适应？",
            "session_id": None, "msgType": 2, "reqType": "chat",
            "primeInfo": REAL_PRIME,
        },
    },
    {  # Round 6: 含链接请求 — 家长甩竞品链接, Lisa 要识别链接并回答
        "name": "Round 6 链接-家长发竞品链接求对比",
        "payload": {
            "content": "对了，我朋友给我发了个这个 https://mp.weixin.qq.com/s/abc123xyz "
                       "说是学英语的，你帮我看看这种跟你们的有啥区别？",
            "session_id": None, "msgType": 2, "reqType": "chat",
            "primeInfo": REAL_PRIME,
        },
    },
    {  # Round 7: KB 答不上 → 降级转人工 (119)
        # 命中关键词"费用"但问的是增值税专票税点, KB 召回弱且 LLM judge 判不能答 → kb_degrade
        "name": "Round 7 KB答不上-问增值税专票应转人工",
        "payload": {
            "content": "你们这个费用能不能开增值税专用发票啊？税点是多少？",
            "session_id": None, "msgType": 2, "reqType": "chat",
            "primeInfo": REAL_PRIME,
        },
    },
]

# 映射 MCP tool 名 → Shirly 接口号
TOOL_MAP = {
    "get_user_profile":                     "5.1 画像查询",
    "query_user_profile_chat_history":      "5.2 历史查询",
    "mark_qywx_customer_tags":               "2  企微打标签",
    "reply_lisa_message":                   "6  Lisa消息推送",
    "save_user_profile_analysis":           "5.3 画像写回",
    "get_mantis_promoter_weekly_live_links":"4.1 直播周链接",
    "get_mantis_live_link":                 "4  直播链接",
    "get_qywx_external_detail_v2":          "7  企微客户详情",
}

# 硬指标: 必须调通; 其余接口失败只降级跳过
CRITICAL_TOOLS = {"reply_lisa_message"}
# 非关键: 灰度/依赖外部状态, 失败允许跳过
SKIPPABLE_TOOLS = {"save_user_profile_analysis", "mark_qywx_customer_tags"}


def print_mcp_summary() -> bool:
    print("\n" + "="*72)
    print(" MCP 接口调用汇总")
    print("="*72)
    critical_ok = True
    with _mcp_lock:
        keys = sorted(_mcp_calls.keys())
        for k in keys:
            calls = _mcp_calls[k]
            label = TOOL_MAP.get(k, k)
            ok_count = sum(1 for c in calls if c.get("ok"))
            fail_count = sum(1 for c in calls if c.get("ok") is False)

            if k in CRITICAL_TOOLS:
                mark = "[核心]"
                if fail_count > 0 or ok_count == 0:
                    critical_ok = False
            elif k in SKIPPABLE_TOOLS:
                mark = "[可跳过]"
            else:
                mark = "      "

            status = f"✓{ok_count}" + (f"  ✗{fail_count}" if fail_count else "")
            last_err = next((c["err"] for c in reversed(calls) if c.get("err")), None)
            hint = f"\n{'':>26s}└ err: {last_err[:70]}" if last_err else ""
            print(f"  {mark:<8s} {label:<20s} total={len(calls):2d}  {status}{hint}")

    print()
    if critical_ok:
        print("  ✓✓✓ 核心指标达成: reply_lisa_message 全部调通 ✓✓✓")
    else:
        print("  ✗✗✗ 核心指标未达成: reply_lisa_message 有失败 ✗✗✗")
    print("="*72)
    return critical_ok

def run_round(client: TestClient, rnd: dict, round_idx: int) -> None:
    name = rnd["name"]
    payload = rnd["payload"]
    print(f"\n{'─'*60}")
    print(f"▶ Round {round_idx} — {name}")
    print(f"{'─'*60}")
    print(f"  content = {payload['content']!r}")

    t0 = time.perf_counter()
    resp = client.post(PATH, json=payload, timeout=10)
    dt_ms = int((time.perf_counter() - t0) * 1000)

    print(f"  HTTP {resp.status_code}  elapsed={dt_ms}ms")
    if resp.status_code == 200:
        body = resp.json()
        print(f"  body = {json.dumps(body, ensure_ascii=False)[:200]}")
    else:
        print(f"  body = {resp.text[:300]}")

async def main() -> bool:
    print("=" * 72)
    print(" saleChat 多轮会话 E2E 测试")
    print(f" 目标用户: {REAL_PRIME['customerName']} ({REAL_PRIME['originalUserId']})")
    print(f" 接口: POST {PATH}")
    print("=" * 72)

    # 清空之前的调用记录
    with _mcp_lock:
        _mcp_calls.clear()

    with TestClient(app, raise_server_exceptions=False) as client:
        for i, rnd in enumerate(ROUNDS, 1):
            run_round(client, rnd, i)
            # 等 debounce + LLM + 逐句推送 + 5.3 跑完再发下一轮
            print("  ⏳ 等待链路跑完...", end=" ", flush=True)
            await asyncio.sleep(sc_svc.DEBOUNCE_SECONDS + 40)
            print("done")

    critical_ok = print_mcp_summary()

    # 最终查一下 5.1 看画像是否被写回
    from deeptutor.services.shirley import profile
    raw = await profile.fetch_profile(REAL_PRIME["corpid"], REAL_PRIME["originalUserId"])
    ai = (raw or {}).get("aiAnalysis") or {}
    attrs = ai.get("attributes") or []
    summary = profile.summarize_profile(raw)
    intent = ai.get("intentLevel") or "—"
    version = ai.get("version") or "—"
    print("\n" + "="*72)
    print(" 最终画像状态 (5.1 回读)")
    print("="*72)
    print(f"  aiAnalysis.version = {version}")
    print(f"  intentLevel        = {intent}")
    print(f"  attributes count   = {len(attrs)}")
    print()
    print(summary)

    # ── 业务链路校验 ──
    print("\n" + "="*72)
    print(" 业务链路校验")
    print("="*72)

    if _soul_logs:
        print(f"\n[动态 Lisa soul] SOUL.md 命中 {len(_soul_logs)} 次 ✓")
        print(f"  {_soul_logs[-1].split('] ', 1)[-1]}")
    if _soul_fallback_hits:
        print(f"\n[动态 Lisa soul] ✗ 回落兜底人格 {len(_soul_fallback_hits)} 次:")
        for s in _soul_fallback_hits:
            print(f"  {s.split('] ', 1)[-1]}")

    print(f"\n[历史正序] 采集 {len(_history_logs)} 条:")
    for h in _history_logs:
        print(f"  {h.split('] ', 1)[-1]}")

    print(f"\n[LLM 真实回复] 采集 {len(_reply_logs)} 条:")
    for r in _reply_logs:
        print(f"  {r.split('] ', 1)[-1]}")

    print(f"\n[Lisa 实际推送内容] 共 {len(_sent_texts)} 句:")
    leaked: list[str] = []
    for i, s in enumerate(_sent_texts, 1):
        is_leak = any(t in s for t in ("tool_name", "partner_memorize", "```",
                                       '\\"', "arguments", "{", "}"))
        if is_leak:
            leaked.append(s)
        print(f"  {'✗' if is_leak else ' '} {i:2d}. {s}")

    if leaked:
        print(f"\n[推送洁净度] ✗ {len(leaked)} 句含工具调用 JSON 残留 —— 会漏给真实客户")
        critical_ok = False
    else:
        print("\n[推送洁净度] ✓ 无 JSON/代码块残留，全部是自然语言")

    if _fallback_hits:
        print(f"\n[兜底触发] {len(_fallback_hits)} 次 (LLM 返回空):")
        for f in _fallback_hits:
            print(f"  {f.split('] ', 1)[-1]}")
    else:
        print("\n[兜底触发] 0 次 — LLM 全部正常返回 ✓")

    # ── qdrant 知识库检索校验 ──
    kb_hits = [k for k in _kb_logs if "←✓" in k]
    print(f"\n[qdrant 知识库] 检索日志 {len(_kb_logs)} 条, 命中 {len(kb_hits)} 条:")
    for k in _kb_logs[:12]:
        print(f"  {k.split('] ', 1)[-1][:110]}")
    if not kb_hits:
        print("  ✗ 没有一次 KB 检索命中 —— 检查 qdrant / KB 配置")
        critical_ok = False
    else:
        print("  ✓ KB 检索已接入并有命中")

    # ── 直播链接校验 ──
    print(f"\n[直播链接] 日志 {len(_live_logs)} 条:")
    for l in _live_logs[:10]:
        print(f"  {l.split('] ', 1)[-1][:130]}")
    live_hardcoded = any("xl.shirleyclass.com/s/64Y8vGI" in s for s in _sent_texts)
    if live_hardcoded:
        print("  ✗ 推送里出现了写死的 mock 直播链接!")
        critical_ok = False
    else:
        print("  ✓ 推送中无写死的 mock 链接")

    # ── 链接完整性校验 ──
    broken_url = [s for s in _sent_texts if ("http" in s and len(s) < 25)]
    print(f"\n[链接完整性] 疑似被截断的链接句: {len(broken_url)}")
    for b in broken_url:
        print(f"  ✗ {b}")
    if not broken_url:
        print("  ✓ 推送里的链接都是完整的")

    # ── 图片多模态识别校验 (msgType=101) ──
    print(f"\n[图片多模态] vision/image 日志 {len(_vision_logs)} 条:")
    for v in _vision_logs[:8]:
        print(f"  {v.split('] ', 1)[-1][:110]}")
    vision_ok = any("←✓" in v for v in _vision_logs)
    if vision_ok:
        print("  ✓ msgType=101 图片已走多模态识别")
    else:
        print("  ⚠ 未观测到图片识别成功日志 (可能图片下载失败, 已降级)")

    # ── KB 置信度降级转人工校验 ──
    print(f"\n[KB降级转人工] kb_degrade 日志 {len(_kb_degrade_logs)} 条, push_reject(119) {len(_reject_push_logs)} 条:")
    for d in _kb_degrade_logs[:6]:
        print(f"  {d.split('] ', 1)[-1][:110]}")
    for r in _reject_push_logs[:6]:
        print(f"  {r.split('] ', 1)[-1][:110]}")
    if _kb_degrade_logs and any("OK" in r for r in _reject_push_logs):
        print("  ✓ KB 答不上已触发降级转人工 (msgType=119)")
    else:
        print("  ⚠ 未观测到 KB 降级转人工 (Round 7 可能 KB 召回足够, 未触发)")

    print("=" * 72)
    return critical_ok


_ok = asyncio.run(main())
sys.exit(0 if _ok else 1)
