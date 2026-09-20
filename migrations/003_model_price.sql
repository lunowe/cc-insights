-- Cost: a dated price table, and a derived per-event cost.
--
-- Two rules shape this migration, and both exist to stop a number that looks
-- authoritative from being wrong.
--
-- 1. PRICES ARE DATED. A rate is not a property of a model, it is a property
--    of a model *on a day*. Claude Sonnet 5 went from $2/$10 to $3/$15 per
--    MTok on 2026-09-01; pricing 2026-08 traffic at today's rate silently
--    rewrites history every time a vendor changes a number. So the key is
--    (model, effective_from) and an event is priced with the newest row not
--    later than the event itself.
--
-- 2. AN UNPRICED MODEL COSTS NULL, NEVER ZERO. A model with no row here gets
--    no `event_cost` row at all, and every total reports the tokens it could
--    not price alongside the money it could. A $0 that means "we don't know"
--    is the same failure as the "time saved" metric the roadmap bans: a
--    guess wearing the clothes of a measurement.
--
-- Rates are REAL, in currency units per MILLION tokens -- exactly the shape
-- vendors publish, so a human can diff this table against a pricing page.
-- Costs are INTEGER NANO-currency-units, so SUM() over a hundred thousand
-- events is exact rather than float-drifted. $20,000 is 2e13 nano, four
-- orders of magnitude inside a signed 64-bit integer (BIGINT in PostgreSQL;
-- Postgres INTEGER would overflow at $2.15, which is the whole reason this is
-- called out).

CREATE TABLE model_price (
    -- The model string as it appears in `event.model`, not a vendor's
    -- canonical id: that is the only key an event can be joined on.
    model            TEXT NOT NULL,
    -- Epoch ms UTC. 0 means "for as long as this table has records", which is
    -- what a vendor's current price means when no change is documented.
    effective_from   INTEGER NOT NULL,
    -- Currency units per million tokens. NULL is "not known", NOT "free":
    -- tokens in a component with a NULL rate are reported unpriced.
    input_mtok       REAL,
    output_mtok      REAL,
    cache_read_mtok  REAL,
    cache_write_mtok REAL,
    currency         TEXT NOT NULL DEFAULT 'USD',
    -- How this row got here:
    --   genai-prices  synced from the pydantic/genai-prices catalog
    --   manual        a human typed it. `cci price sync` never overwrites one,
    --                 the same rule project_group.origin = 'manual' follows.
    origin           TEXT NOT NULL,
    -- provider/model the catalog matched, so a surprising rate is traceable
    -- back to the row a human can go read.
    matched_id       TEXT,
    note             TEXT,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (model, effective_from)
);

-- Derived, and recomputed whole: `cci cost` clears and rebuilds it, exactly as
-- `cci derive` does for `span`. It must be rebuildable, because the price
-- table changes underneath it.
--
-- session_id / thread_id / ts / model are denormalized off `event` so every
-- aggregate this feeds is one indexed scan rather than a join back.
CREATE TABLE event_cost (
    event_id         TEXT PRIMARY KEY REFERENCES event(id),
    session_id       TEXT NOT NULL REFERENCES session(id),
    thread_id        TEXT NOT NULL REFERENCES thread(id),
    ts               INTEGER NOT NULL,
    -- The model this event was priced as. Not always `event.model`: Codex
    -- records usage on events that name no model, so the model is carried
    -- forward from the last event in the same thread that did. `attributed`
    -- marks those, so a breakdown can say how much of a total rests on it.
    model            TEXT NOT NULL,
    attributed       INTEGER NOT NULL DEFAULT 0,
    price_from       INTEGER NOT NULL,
    input_nano       INTEGER NOT NULL DEFAULT 0,
    output_nano      INTEGER NOT NULL DEFAULT 0,
    cache_read_nano  INTEGER NOT NULL DEFAULT 0,
    cache_write_nano INTEGER NOT NULL DEFAULT 0
);

-- The other half of the same answer, and the reason a total from this schema
-- can be quoted. An event whose tokens could not be priced leaves a row here
-- instead of vanishing, so every report can say what it could not see rather
-- than implying it saw everything.
--
-- An event can legitimately appear in BOTH tables: a model with input and
-- output rates but no cache-write rate is priced for what is known and
-- recorded here for what is not. `tokens` is the unpriced portion only.
CREATE TABLE event_unpriced (
    event_id   TEXT PRIMARY KEY REFERENCES event(id),
    session_id TEXT NOT NULL REFERENCES session(id),
    thread_id  TEXT NOT NULL REFERENCES thread(id),
    ts         INTEGER NOT NULL,
    -- Carried forward from the thread where the event names none. NULL means
    -- even that failed -- 46 Codex events on the measured corpus.
    model      TEXT,
    attributed INTEGER NOT NULL DEFAULT 0,
    tokens     INTEGER NOT NULL,
    -- no_model      nothing in the thread said which model ran
    -- no_rate       the model has no rate on file at that date
    -- no_component  the model is priced, but not for this token component
    reason     TEXT NOT NULL
);

CREATE INDEX idx_event_cost_ts      ON event_cost (ts);
CREATE INDEX idx_event_cost_session ON event_cost (session_id);
CREATE INDEX idx_event_cost_thread  ON event_cost (thread_id);
CREATE INDEX idx_event_cost_model   ON event_cost (model);

CREATE INDEX idx_event_unpriced_ts     ON event_unpriced (ts);
CREATE INDEX idx_event_unpriced_thread ON event_unpriced (thread_id);
CREATE INDEX idx_event_unpriced_model  ON event_unpriced (model);
