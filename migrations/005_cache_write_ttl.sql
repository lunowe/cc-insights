-- Cache writes have two prices, because they have two lifetimes.
--
-- Anthropic charges 1.25x base input for a cache write that lives five
-- minutes and 2x for one that lives an hour. On Fable-tier models that is
-- $12.50 against $20 per MTok. Migration 003 had one rate and one token
-- column, so every write was priced as a five-minute one.
--
-- That is not a rounding error on this corpus: 144.7M of 351.2M cache-write
-- tokens are one-hour writes -- 41% -- and pricing them short understates the
-- total by about $1,085. The split is not random. Claude Code puts every
-- request in one of two buckets and picks a TTL per bucket: the main
-- conversation gets the hour on a subscription within plan usage, everything
-- else (subagents, workflows, compaction) gets five minutes. Measured here:
-- main transcripts are 99% one-hour, subagent transcripts are 100% five.
--
-- WHY A "OF WHICH" COLUMN RATHER THAN TWO SIBLINGS.
-- `event.cache_write_tokens` already holds the total on 190,000 rows and is
-- correct. Splitting it into `cache_write_5m_tokens` + `cache_write_1h_tokens`
-- would make every existing row read as "all five-minute", which is a claim
-- the database cannot support -- the logs knew, the schema never asked. So
-- the total stays where it is and this column records the part of it that
-- lived an hour. NULL therefore means *the TTL is unknown*, which is exactly
-- true of every row written before this migration, and is distinguishable
-- from 0, which means "all of it was a five-minute write".
--
-- Rows already stored keep NULL until `cci backfill` re-reads the logs that
-- are still on disk. What has aged out stays unknown, and `cci cost` says how
-- much of the total rests on the five-minute assumption rather than quietly
-- absorbing it.

ALTER TABLE event ADD COLUMN cache_write_1h_tokens INTEGER;

-- Currency units per million tokens for a one-hour cache write. NULL is "not
-- known", not "same as the five-minute rate": a model whose one-hour rate we
-- cannot look up reports those tokens unpriced, like any other component.
ALTER TABLE model_price ADD COLUMN cache_write_1h_mtok REAL;

-- Its own component in the derived ledger, so a breakdown can show what the
-- long TTL actually costs rather than blending it into cache writes.
ALTER TABLE event_cost ADD COLUMN cache_write_1h_nano INTEGER NOT NULL DEFAULT 0;
