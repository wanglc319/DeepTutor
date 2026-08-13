-- ============================================
-- AI Sales CRM — Schema Initialization
-- ============================================
-- Runs once by Postgres on first container start.
-- Safe to re-apply: all statements use CREATE/ALTER IF NOT EXISTS.
-- ============================================

-- ---------- extensions ----------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";       -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";        -- 模糊/中文全文搜索加速

-- zhparser 需要额外安装的 postgres 中文分词扩展，官方镜像不带。
-- 用 PL/pgSQL 块安全跳过：没有就没有，pg_trgm + gin(trgm) 已经足够中文模糊搜索。
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS "zhparser";
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'zhparser not available (non-fatal) — falling back to pg_trgm for CJK search';
END $$;

-- ---------- enums ----------
DO $$ BEGIN
    CREATE TYPE channel_type AS ENUM ('wecom', 'wechat', 'website', 'taobao', 'douyin', 'manual', 'other');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE conversation_stage AS ENUM (
        'new',               -- 刚进来，打招呼
        'warmup',            -- 暖场
        'need_discovery',    -- 需求挖掘
        'product_match',     -- 产品匹配
        'objection_handle',  -- 异议处理
        'quote',             -- 报价
        'follow_up',         -- 留资/预约
        'handoff',           -- 已转人工
        'closed_won',        -- 成单
        'closed_lost'        -- 流失
    );
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE message_sender AS ENUM ('customer', 'ai', 'agent', 'system');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE handoff_reason AS ENUM (
        'customer_request',    -- 客户主动要人工
        'ai_low_confidence',   -- AI 连续低置信度
        'price_sensitive',     -- 价格/折扣问题
        'refund_complaint',    -- 退款/投诉
        'complex_consult',     -- 复杂咨询
        'manual_override'      -- 销售主动接管
    );
EXCEPTION WHEN duplicate_object THEN null; END $$;


-- ============================================
-- 1. customers — 客户档案
-- ============================================
CREATE TABLE IF NOT EXISTS customers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     VARCHAR(128) UNIQUE,                    -- 渠道侧原始 ID（企微 userId / 微信 openid / 官网 session 等）
    channel         channel_type NOT NULL,
    nickname        VARCHAR(128),
    avatar_url      TEXT,

    -- 销售信息
    intent_level    VARCHAR(16) DEFAULT 'unknown',          -- high / medium / low / unknown
    tags            JSONB DEFAULT '[]'::jsonb,               -- ['学霸营意向', '三年级', '预算5k+']
    source          VARCHAR(256),                             -- 来源备注：朋友圈广告 / 朋友推荐 / 自然流量
    notes           TEXT,                                     -- 销售手动备注

    -- 时间线
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_customers_external  ON customers (external_id);
CREATE INDEX IF NOT EXISTS idx_customers_channel   ON customers (channel);
CREATE INDEX IF NOT EXISTS idx_customers_intent    ON customers (intent_level);
CREATE INDEX IF NOT EXISTS idx_customers_active    ON customers (last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_customers_tags_gin  ON customers USING gin (tags);


-- ============================================
-- 2. conversations — 对话会话
-- ============================================
CREATE TABLE IF NOT EXISTS conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    partner_id      VARCHAR(64) NOT NULL DEFAULT 'lisa',     -- AI 角色（多销售人设预留）

    -- 状态机
    stage           conversation_stage NOT NULL DEFAULT 'new',
    ai_active       BOOLEAN NOT NULL DEFAULT TRUE,            -- false = 已转人工
    agent_id        VARCHAR(64),                              -- 接管的销售账号
    handoff_reason  handoff_reason,

    -- LLM / RAG 上下文
    llm_model       VARCHAR(128),
    kb_ids          JSONB DEFAULT '[]'::jsonb,                -- 本次会话使用的 KB

    -- 时间
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conv_customer     ON conversations (customer_id);
CREATE INDEX IF NOT EXISTS idx_conv_stage        ON conversations (stage);
CREATE INDEX IF NOT EXISTS idx_conv_active       ON conversations (ai_active);
CREATE INDEX IF NOT EXISTS idx_conv_opened       ON conversations (opened_at DESC);


-- ============================================
-- 3. messages — 消息流水
-- ============================================
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender          message_sender NOT NULL,
    seq             INTEGER NOT NULL,                         -- 会话内递增序号，用于排序

    -- 消息内容（纯文本 + 可选富 JSON）
    content         TEXT NOT NULL,
    content_jsonb   JSONB DEFAULT '{}'::jsonb,                -- 卡片/图片/文件/工具调用结果

    -- AI 专属
    llm_model       VARCHAR(128),
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens INTEGER,
    latency_ms      INTEGER,
    confidence      REAL,                                     -- AI 置信度 0~1
    rag_kb_ids      JSONB DEFAULT '[]'::jsonb,               -- 本条检索用了哪些 KB
    rag_hits        JSONB DEFAULT '[]'::jsonb,               -- 检索结果摘要 [{score, chunk_preview}]

    -- 渠道
    channel_msg_id  VARCHAR(128),                             -- 渠道侧消息 ID（去重用）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (conversation_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_msg_conv_seq   ON messages (conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_msg_time       ON messages (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_sender     ON messages (sender);
CREATE INDEX IF NOT EXISTS idx_msg_content_gin ON messages USING gin (to_tsvector('simple', content));


-- ============================================
-- 4. conversation_events — 系统事件 / 状态流转
-- ============================================
CREATE TABLE IF NOT EXISTS conversation_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    event_type      VARCHAR(64) NOT NULL,                     -- stage_transition / handoff / auto_reply_disabled / ...
    from_stage      conversation_stage,
    to_stage        conversation_stage,
    payload         JSONB DEFAULT '{}'::jsonb,                -- 事件详情
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evt_conv   ON conversation_events (conversation_id);
CREATE INDEX IF NOT EXISTS idx_evt_type   ON conversation_events (event_type);
CREATE INDEX IF NOT EXISTS idx_evt_time   ON conversation_events (created_at DESC);


-- ============================================
-- 5. kb_versions — 知识库版本发布
-- ============================================
CREATE TABLE IF NOT EXISTS kb_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_name         VARCHAR(128) NOT NULL,
    version         VARCHAR(32) NOT NULL,                     -- v2026.08.12-01
    change_summary  TEXT,
    embedding_model VARCHAR(128),
    chunk_count     INTEGER,
    vector_count    INTEGER,
    published_by    VARCHAR(64),
    published_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (kb_name, version)
);

CREATE INDEX IF NOT EXISTS idx_kb_name ON kb_versions (kb_name, published_at DESC);


-- ============================================
-- updated_at trigger helper
-- ============================================
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_customers_touch ON customers;
CREATE TRIGGER trg_customers_touch BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- ============================================
-- 种子数据：一个测试客户 + 一轮对话
-- ============================================
INSERT INTO customers (external_id, channel, nickname, intent_level, tags, source, notes)
VALUES
    ('wx_demo_001', 'wecom', '王先生', 'high',  '["学霸营意向", "四年级", "预算5k+"]'::jsonb, '朋友圈广告', '电话沟通中，意向高'),
    ('web_demo_002','website','李女士', 'medium','["产品咨询", "二年级"]'::jsonb,  '官网SEO',    '还没问具体产品')
ON CONFLICT (external_id) DO NOTHING;

INSERT INTO conversations (customer_id, partner_id, stage, ai_active, opened_at)
SELECT id, 'lisa', 'need_discovery', TRUE, now() - interval '2 hours'
FROM customers WHERE external_id = 'wx_demo_001';

INSERT INTO messages (conversation_id, sender, seq, content, llm_model, latency_ms, confidence)
SELECT c.id, 'customer', 1, '你好，请问学霸营是什么？', NULL, NULL, NULL
FROM conversations c JOIN customers cu ON cu.id = c.customer_id WHERE cu.external_id = 'wx_demo_001';

INSERT INTO messages (conversation_id, sender, seq, content, llm_model, latency_ms, confidence,
                      rag_kb_ids, rag_hits)
SELECT c.id, 'ai', 2,
       '学霸营其实就是个综合型课程，每天带娃做精读泛读、精听泛听和口语输出，把词汇、读写听说这六大板块全练到位。您家孩子目前几年级呀？',
       'qwen3.7-flash', 7830, 0.92,
       '["lisa_chat_history"]'::jsonb,
       '[{"score": 0.91, "preview": "学霸营是综合型课程，每天包含精读、泛读、精听、泛听和口语输出..."}, {"score": 0.87, "preview": "六大板块：词汇、句型、阅读、口语、听力、写作..."}]'::jsonb
FROM conversations c JOIN customers cu ON cu.id = c.customer_id WHERE cu.external_id = 'wx_demo_001';

INSERT INTO conversation_events (conversation_id, event_type, from_stage, to_stage, payload)
SELECT c.id, 'stage_transition', 'new', 'warmup', '{"reason": "customer_first_message"}'::jsonb
FROM conversations c JOIN customers cu ON cu.id = c.customer_id WHERE cu.external_id = 'wx_demo_001';

INSERT INTO conversation_events (conversation_id, event_type, from_stage, to_stage, payload)
SELECT c.id, 'stage_transition', 'warmup', 'need_discovery', '{"reason": "ai_asked_grade"}'::jsonb
FROM conversations c JOIN customers cu ON cu.id = c.customer_id WHERE cu.external_id = 'wx_demo_001';

INSERT INTO kb_versions (kb_name, version, change_summary, embedding_model, chunk_count, vector_count, published_by)
VALUES
    ('lisa_chat_history', 'v2026.08.11-01', '初始版本：产品资料 + FAQ + 历史对话清洗入库', 'bge-m3', 4820, 4820, 'system'),
    ('ai_english_sales',  'v2026.08.11-01', '销售话术库：应对常见异议和询价场景',             'bge-m3', 1240, 1240, 'system')
ON CONFLICT (kb_name, version) DO NOTHING;
