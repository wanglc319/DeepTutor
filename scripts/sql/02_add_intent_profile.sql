-- ============================================
-- AI Sales CRM — 意向度打分字段
-- 文档参考: https://jcnmgzcga30e.feishu.cn/wiki/HS8swfTOriiJykkH2zJcZfPInlf
-- 安全重跑: 所有 ALTER 用 IF NOT EXISTS
-- ============================================

-- ---------- enum: intent_temperature ----------
DO $$ BEGIN
    CREATE TYPE intent_temperature AS ENUM ('unknown', 'freezing', 'cold', 'cool', 'warm', 'hot', 'blazing');
EXCEPTION WHEN duplicate_object THEN null; END $$;

-- ---------- enum: next_action ----------
DO $$ BEGIN
    CREATE TYPE next_action AS ENUM (
        'none',
        'proactive_live_push',     -- 推直播链接（凉档标准培育）
        'deepen_discovery',         -- 温档：主动铺开其他交付维度
        'targeted_objection',       -- 热档：定向解决已追问维度的疑虑
        'closing_nudge',            -- 极热：临门一脚
        'low_freq_maintenance'      -- 冷档：低频维护
    );
EXCEPTION WHEN duplicate_object THEN null; END $$;

-- ---------- ALTER customers ----------
ALTER TABLE customers ADD COLUMN IF NOT EXISTS profile JSONB DEFAULT '{}'::jsonb;
COMMENT ON COLUMN customers.profile IS
    '完整意向度档案 JSON，结构见文档第 5.4 节: customer_profile { intent_signals, timeline, profile, intent_temperature, next_action }';

ALTER TABLE customers ADD COLUMN IF NOT EXISTS profile_updated_at TIMESTAMPTZ;

ALTER TABLE customers ADD COLUMN IF NOT EXISTS intent_temperature intent_temperature DEFAULT 'unknown';
COMMENT ON COLUMN customers.intent_temperature IS
    '冗余索引列，= profile ->> intent_temperature，方便 WHERE / ORDER BY';

ALTER TABLE customers ADD COLUMN IF NOT EXISTS next_action next_action DEFAULT 'none';

-- 迁移现有 intent_level 到 intent_temperature（varchar → enum）
-- intent_level 是之前的 high/medium/low/unknown，粗略映射:
UPDATE customers SET intent_temperature = CASE intent_level
    WHEN 'high'   THEN 'hot'
    WHEN 'medium' THEN 'warm'
    WHEN 'low'    THEN 'cool'
    ELSE 'unknown'
END::intent_temperature,
profile_updated_at = COALESCE(profile_updated_at, now())
WHERE intent_temperature = 'unknown';

-- ---------- indexes ----------
CREATE INDEX IF NOT EXISTS idx_customers_temp   ON customers (intent_temperature);
CREATE INDEX IF NOT EXISTS idx_customers_action ON customers (next_action);
CREATE INDEX IF NOT EXISTS idx_customers_profile_gin ON customers USING gin (profile);
