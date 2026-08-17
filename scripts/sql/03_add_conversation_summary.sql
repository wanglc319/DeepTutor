-- ============================================
-- AI Sales CRM — memory layer migration
-- ============================================
-- 引入 conversation_summary 表作为长会话压缩层:
--   当某会话累计消息超过 MEMORY_WINDOW_HARD 阈值时,
--   自动对前半段做一次 LLM 摘要, 存入本表;
--   LLM 推理时 messages = summary(摘要版) + recent(滑窗版).
-- 运行:  psql -d sales_crm -f scripts/sql/03_add_conversation_summary.sql
-- ============================================

-- ---------- conversation_summary ----------
CREATE TABLE IF NOT EXISTS conversation_summary (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

    -- 摘要覆盖的消息范围 [lo_seq, hi_seq] inclusive
    lo_seq          INTEGER NOT NULL,
    hi_seq          INTEGER NOT NULL,
    turn_count      INTEGER NOT NULL,              -- 覆盖了多少条 messages

    summary         TEXT NOT NULL,                  -- LLM 生成的段落摘要
    key_points      JSONB DEFAULT '[]'::jsonb,      -- LLM 结构化提炼: ["三年级", "背单词难", "..."]

    llm_model       VARCHAR(128),
    token_count     INTEGER,
    latency_ms      INTEGER,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (lo_seq <= hi_seq)
);

CREATE INDEX IF NOT EXISTS idx_cs_conv      ON conversation_summary (conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_seq_range ON conversation_summary (conversation_id, lo_seq, hi_seq);
