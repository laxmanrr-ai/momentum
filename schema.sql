-- ============================================================
-- QM Research Database Schema
-- File: /workspaces/momentum/data/qm_research.db
-- ============================================================
-- Design principles:
--   holdings       = what the model knew at snapshot time
--   weekly_health  = how signals change over time
--   forward_returns= what happened later (never in holdings)
--   price_daily    = raw OHLCV, single source of truth
-- ============================================================

PRAGMA journal_mode = WAL;       -- safe concurrent reads
PRAGMA foreign_keys = ON;

-- ============================================================
-- TABLE 1: snapshots
-- One row per weekly Wednesday run.
-- Grain: snapshot_date
-- ============================================================
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date          TEXT    NOT NULL,   -- YYYY-MM-DD Wednesday
    run_timestamp          TEXT    NOT NULL,   -- ISO datetime of run
    universe_size          INTEGER,            -- tickers considered after ADV filter
    candidates_after_step2 INTEGER,            -- after outlier removal
    candidates_after_step3 INTEGER,            -- after 12_2 top-100
    stocks_selected        INTEGER,            -- final portfolio size (≤50)
    spy_close              REAL,               -- SPY close on snapshot date
    spy_200dma             REAL,               -- SPY 200-day MA (regime flag)
    spy_above_200dma       INTEGER,            -- 1=bull regime, 0=bear regime
    notes                  TEXT,               -- free text, e.g. "first run"
    PRIMARY KEY (snapshot_date)
);

-- ============================================================
-- TABLE 2: holdings
-- One row per ticker per snapshot.
-- Grain: snapshot_date + ticker
-- Contains ONLY information known at snapshot time.
-- Forward returns live in forward_returns table.
-- ============================================================
CREATE TABLE IF NOT EXISTS holdings (
    snapshot_date              TEXT    NOT NULL,
    ticker                     TEXT    NOT NULL,

    -- ── Selection metadata ──────────────────────────────────
    final_rank                 INTEGER,         -- 1=best FIP in top-100 mom
    rank_momentum              INTEGER,         -- rank by 12_2 among survivors
    rank_fip                   INTEGER,         -- rank by FIP among top-100
    selected_flag              INTEGER NOT NULL DEFAULT 1,  -- 1=in portfolio

    -- ── Momentum horizon signals ────────────────────────────
    mom_1m                     REAL,            -- 1-month raw (signal = -mom_1m computed in views)
    mom_3m                     REAL,            -- 3-month return (63d skip 21d)
    mom_6m                     REAL,            -- 6-month return (126d skip 21d)
    mom_9m                     REAL,            -- 9-month return (189d skip 21d)
    mom_12_2                   REAL,            -- 12-month skip 1 (252+21 days)

    -- ── Momentum ranks (within survived universe) ───────────
    rank_mom_1m                INTEGER,
    rank_mom_3m                INTEGER,
    rank_mom_6m                INTEGER,
    rank_mom_9m                INTEGER,
    rank_mom_12_2              INTEGER,

    -- ── Acceleration / deterioration ───────────────────────
    -- positive = 3M rank better than 12_2 rank = accelerating
    -- negative = 3M rank worse = decelerating
    momentum_acceleration_rank INTEGER,        -- rank_mom_12_2 - rank_mom_3m
    momentum_acceleration_3m_vs_12_2 REAL,    -- mom_3m / mom_12_2 (ratio)
    momentum_trend_score       REAL,           -- composite: avg of normalized ranks

    -- ── FIP quality momentum ────────────────────────────────
    fip_score                  REAL,           -- sign(mom)*(%neg-%pos)
    pct_positive_days          REAL,           -- % up days in lookback
    pct_negative_days          REAL,           -- % down days in lookback

    -- ── Momentum bucket ─────────────────────────────────────
    momentum_bucket            TEXT,           -- '0-50%','50-100%','100-200%' etc

    -- ── Price structure ─────────────────────────────────────
    price                      REAL,
    price_52w_high             REAL,
    price_52w_low              REAL,
    dist_52w_high              REAL,           -- (price-52wH)/52wH, negative=below
    dist_52w_low               REAL,           -- (price-52wL)/52wL, positive=above
    atr_20                     REAL,
    atr_pct                    REAL,
    volatility_20d             REAL,           -- annualised
    volatility_63d             REAL,
    max_daily_move_252d        REAL,
    largest_gap_up_252d        REAL,
    up_days_252d               INTEGER,
    down_days_252d             INTEGER,
    consecutive_up_days_max    INTEGER,
    consecutive_dn_days_max    INTEGER,
    mean_up_day                REAL,
    mean_down_day              REAL,
    largest_up_day             REAL,
    largest_down_day           REAL,

    -- ── Liquidity ───────────────────────────────────────────
    market_cap                 REAL,
    adv_20d                    REAL,           -- avg dollar volume 20 days
    adv_63d                    REAL,           -- avg dollar volume 63 days
    adv_to_mcap                REAL,           -- adv_20d / market_cap
    shares_outstanding         REAL,
    float_shares               REAL,
    turnover_ratio             REAL,

    -- ── Exposure ────────────────────────────────────────────
    sector                     TEXT,
    industry                   TEXT,
    industry_clean             TEXT,
    sic_code                   TEXT,
    country                    TEXT,
    exchange                   TEXT,
    sector_weight              REAL,           -- sector count / portfolio size
    industry_weight            REAL,

    -- ── Relative strength vs SPY ────────────────────────────
    rs_vs_spy_20d              REAL,           -- stock 20d return - SPY 20d return
    rs_vs_spy_63d              REAL,
    rs_vs_spy_126d             REAL,

    -- ── Up/down volume ratio ────────────────────────────────
    up_volume_20d              REAL,           -- avg volume on up days
    down_volume_20d            REAL,           -- avg volume on down days
    up_down_volume_ratio       REAL,           -- up_volume / down_volume

    -- ── Price vs moving averages ────────────────────────────
    price_vs_50dma             REAL,           -- (price/50dma)-1
    price_vs_200dma            REAL,           -- (price/200dma)-1
    above_50dma                INTEGER,        -- 1/0
    above_200dma               INTEGER,        -- 1/0

    -- ── Equal weight ────────────────────────────────────────
    equal_weight_pct           REAL,

    PRIMARY KEY (snapshot_date, ticker),
    FOREIGN KEY (snapshot_date) REFERENCES snapshots(snapshot_date)
);

-- ============================================================
-- TABLE 3: price_daily
-- Raw OHLCV. Single source of truth for all price lookups.
-- Grain: ticker + date
-- ============================================================
CREATE TABLE IF NOT EXISTS price_daily (
    ticker      TEXT    NOT NULL,
    date        TEXT    NOT NULL,   -- YYYY-MM-DD
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL    NOT NULL,
    volume      REAL,
    adjusted    INTEGER NOT NULL DEFAULT 1,   -- 1=split-adjusted
    source      TEXT    DEFAULT 'massive',
    PRIMARY KEY (ticker, date)
);

-- ============================================================
-- TABLE 4: weekly_health
-- Signal trends computed each Wednesday for held stocks.
-- Grain: snapshot_date + ticker
-- Links back to the snapshot when the stock was first selected.
-- ============================================================
CREATE TABLE IF NOT EXISTS weekly_health (
    week_date                      TEXT    NOT NULL,  -- Wednesday YYYY-MM-DD
    ticker                         TEXT    NOT NULL,

    -- ── Current signal values ───────────────────────────────
    current_rank                   INTEGER,
    current_fip                    REAL,
    current_mom_12_2               REAL,
    current_mom_3m                 REAL,
    current_pct_positive_days      REAL,
    current_price                  REAL,
    current_adv_20d                REAL,

    -- ── Rank changes ────────────────────────────────────────
    rank_change_1w                 INTEGER,    -- current - last week
    rank_change_4w                 INTEGER,    -- current - 4 weeks ago
    rank_change_12w                INTEGER,    -- current - 12 weeks ago

    -- ── FIP trend ───────────────────────────────────────────
    fip_change_1w                  REAL,
    fip_change_4w                  REAL,
    fip_trend_direction            TEXT,       -- 'improving','deteriorating','stable'

    -- ── Momentum trend ──────────────────────────────────────
    mom_3m_change_1w               REAL,
    mom_12_2_change_1w             REAL,
    momentum_acceleration_change_4w REAL,

    -- ── Persistence ─────────────────────────────────────────
    weeks_in_top_50                INTEGER,    -- consecutive weeks in top 50
    weeks_in_top_100               INTEGER,    -- consecutive weeks in top 100
    weeks_since_peak_rank          INTEGER,    -- weeks since best rank achieved
    peak_rank                      INTEGER,    -- best rank ever achieved

    -- ── Relative strength vs SPY ────────────────────────────
    rs_vs_spy_1w                   REAL,
    rs_vs_spy_4w                   REAL,
    rs_vs_spy_8w                   REAL,
    rs_vs_spy_trend                TEXT,       -- 'positive','negative','deteriorating'

    -- ── Volume health ───────────────────────────────────────
    up_down_volume_ratio_current   REAL,
    up_down_volume_ratio_4w_ago    REAL,
    volume_trend                   TEXT,       -- 'accumulation','distribution','neutral'

    -- ── Price vs moving averages ────────────────────────────
    price_vs_50dma                 REAL,
    price_vs_200dma                REAL,
    above_50dma                    INTEGER,
    above_200dma                   INTEGER,

    -- ── Drawdown tracking ───────────────────────────────────
    peak_price_since_entry         REAL,       -- highest close since entry

    -- ── Deterioration metrics (exit-signal research) ─────────
    -- (current_price / highest_price_since_entry) - 1
    -- Negative = drawdown from peak. 0 = at new high.
    drawdown_from_peak_pct         REAL,

    -- (current_price / entry_price) - 1
    -- Positive even while drawdown_from_peak is negative.
    -- Example: entry=$100, peak=$140, current=$126 → +26%
    drawdown_from_entry_pct        REAL,

    -- (peak_price_since_entry / entry_price) - 1
    entry_peak_gain_pct            REAL,

    -- Fraction of post-entry gain retraced.
    -- = (peak_price - current_price) / (peak_price - entry_price)
    -- 0%=at peak  100%=fully retraced to entry  >100%=below entry
    -- NULL when peak_price = entry_price (no gain yet to retrace).
    -- Example: entry=$100, peak=$140, current=$126 → 35%
    momentum_retracement_pct       REAL,

    -- ── Momentum Health Score ───────────────────────────────
    -- Composite — only meaningful after exit research validates weights
    -- Stored as NULL until weights are empirically determined
    -- momentum_health_score deferred to future health_scores table

    -- ── Accumulation urgency metrics ──────────────────────────────────────
    -- Measure whether buying pressure is becoming more urgent.
    -- Rising urgency may indicate institutional accumulation acceleration.

    -- current_volume / 20-day avg volume  (>1.0 = elevated buying interest)
    relative_volume_20d            REAL,

    -- current_volume / 63-day avg volume  (longer-term volume baseline)
    relative_volume_63d            REAL,

    -- avg volume on up days / avg volume on down days over trailing 20d
    -- >1.0 = more volume on up days = accumulation signal
    -- <1.0 = more volume on down days = distribution signal
    up_down_volume_ratio           REAL,

    -- avg ((close-low)/(high-low)) over trailing 20d
    -- 1.0 = closes at high every day, 0.0 = closes at low
    -- floor: if high==low, use 0.5
    close_location_value_20d       REAL,

    -- trading days since last 52-week closing high
    -- 0 = at new high today, rising = trend aging
    days_since_new_high            INTEGER,

    -- trading days from pullback low to recovery close
    -- pullback defined as >5% decline from most recent local closing high
    -- NULL = currently in pullback (not yet recovered)
    -- 0    = no >5% pullback in trailing 252d
    pullback_recovery_days         INTEGER,

    -- change in relative_volume_20d vs 4 weeks ago
    -- positive = volume accelerating, negative = fading
    volume_acceleration_4w         REAL,

    -- change in momentum_acceleration_rank vs 4 weeks ago
    -- positive = acceleration improving, negative = deteriorating
    momentum_acceleration_rank_change_4w INTEGER,

    -- ── Exit signal flags ───────────────────────────────────
    -- Each flag = 1 if signal fires, 0 if not
    flag_rank_below_75             INTEGER DEFAULT 0,
    flag_rank_below_100            INTEGER DEFAULT 0,
    flag_fip_worsened_005          INTEGER DEFAULT 0,   -- FIP up by 0.05
    flag_rs_negative_4w            INTEGER DEFAULT 0,
    flag_volume_ratio_below_08     INTEGER DEFAULT 0,
    flag_below_50dma               INTEGER DEFAULT 0,
    flag_below_200dma              INTEGER DEFAULT 0,
    flag_drawdown_15pct            INTEGER DEFAULT 0,   -- from peak

    -- ── Entry reference ─────────────────────────────────────
    entry_snapshot_date            TEXT,       -- when first selected
    entry_price                    REAL,       -- price at first selection
    entry_rank                     INTEGER,    -- rank at first selection
    entry_fip                      REAL,       -- FIP at first selection

    -- week_date is any monitoring Wednesday, not necessarily a snapshot date
    -- No FK to snapshots — week_date is free-standing
    PRIMARY KEY (week_date, ticker)
);

-- ============================================================
-- TABLE 5: forward_returns
-- Future outcomes filled in by backfill runs.
-- Grain: snapshot_date + ticker + horizon
-- NEVER mutates holdings. Lookahead-safe by design.
-- ============================================================
CREATE TABLE IF NOT EXISTS forward_returns (
    snapshot_date       TEXT    NOT NULL,
    ticker              TEXT    NOT NULL,
    horizon             TEXT    NOT NULL,   -- '1M','3M','6M','12M'

    -- ── Price anchors ────────────────────────────────────────
    start_price         REAL,               -- close on snapshot_date
    end_price           REAL,               -- close on snapshot_date + horizon
    start_date          TEXT,               -- actual start date used
    end_date            TEXT,               -- actual end date used

    -- ── SPY benchmark ────────────────────────────────────────
    spy_start_price     REAL,
    spy_end_price       REAL,

    -- ── Return metrics ───────────────────────────────────────
    absolute_return     REAL,               -- (end/start) - 1
    spy_return          REAL,               -- SPY return over same period
    excess_return_vs_spy REAL,             -- absolute_return - spy_return

    -- ── Drawdown ─────────────────────────────────────────────
    max_drawdown        REAL,               -- worst intraperiod drawdown
    max_gain            REAL,               -- best intraperiod gain

    -- ── Status ───────────────────────────────────────────────
    completed           INTEGER DEFAULT 0,  -- 1 when end_date has passed
    computed_at         TEXT,               -- ISO datetime of backfill run

    PRIMARY KEY (snapshot_date, ticker, horizon),
    FOREIGN KEY (snapshot_date, ticker)
        REFERENCES holdings(snapshot_date, ticker)
);

-- ============================================================
-- TABLE 6: backtest_runs
-- One row per backtest configuration.
-- Grain: run_id
-- ============================================================
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              TEXT    NOT NULL,   -- UUID or slug e.g. 'qm_weekly_10bps'
    created_at          TEXT    NOT NULL,
    description         TEXT,

    -- ── Parameters ───────────────────────────────────────────
    start_date          TEXT    NOT NULL,   -- backtest start YYYY-MM-DD
    end_date            TEXT    NOT NULL,
    rebalance_freq      TEXT    NOT NULL,   -- 'weekly','monthly','quarterly'
    cost_bps            REAL    NOT NULL,   -- round-trip cost in bps
    universe_type       TEXT    NOT NULL,   -- 'curated_2016','forward_only'
    portfolio_size      INTEGER NOT NULL DEFAULT 50,
    benchmark           TEXT    NOT NULL DEFAULT 'SPY',
    replacement_logic   TEXT    NOT NULL,   -- 'cash','next_best','spy'
    survivorship_bias   TEXT    NOT NULL,   -- 'moderate','none'
    notes               TEXT,

    -- ── Summary results (filled after run) ───────────────────
    cagr                REAL,
    sharpe              REAL,
    sortino             REAL,
    max_drawdown        REAL,
    volatility          REAL,
    total_return        REAL,
    win_rate_periods    REAL,
    avg_turnover        REAL,
    total_cost_drag     REAL,
    benchmark_cagr      REAL,
    benchmark_sharpe    REAL,
    alpha               REAL,
    beta_to_benchmark   REAL,
    information_ratio   REAL,

    PRIMARY KEY (run_id)
);

-- ============================================================
-- TABLE 7: backtest_portfolio
-- Daily equity curve per backtest run.
-- Grain: run_id + date
-- ============================================================
CREATE TABLE IF NOT EXISTS backtest_portfolio (
    run_id              TEXT    NOT NULL,
    date                TEXT    NOT NULL,   -- YYYY-MM-DD
    portfolio_value     REAL    NOT NULL,   -- dollar value (start=100)
    benchmark_value     REAL,              -- SPY rebased to 100
    daily_return        REAL,
    benchmark_return    REAL,
    excess_return       REAL,
    drawdown            REAL,              -- from portfolio peak
    cash_pct            REAL DEFAULT 0,   -- % in cash (when stock exits early)
    n_holdings          INTEGER,

    PRIMARY KEY (run_id, date),
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
);

-- ============================================================
-- TABLE 8: backtest_trades
-- Every entry and exit with costs.
-- Grain: run_id + date + ticker + direction
-- ============================================================
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id              TEXT    NOT NULL,
    date                TEXT    NOT NULL,
    ticker              TEXT    NOT NULL,
    direction           TEXT    NOT NULL,   -- 'BUY' or 'SELL'

    -- ── Trade details ─────────────────────────────────────────
    price               REAL    NOT NULL,
    shares              REAL,
    gross_value         REAL,
    cost_bps            REAL,
    cost_dollars        REAL,
    net_value           REAL,

    -- ── Context ──────────────────────────────────────────────
    reason              TEXT,               -- 'rebalance','exit_rule_rank_75' etc
    rank_at_trade       INTEGER,
    fip_at_trade        REAL,
    mom_12_2_at_trade   REAL,

    -- ── For SELL only ────────────────────────────────────────
    entry_date          TEXT,               -- matching BUY date
    entry_price         REAL,
    holding_days        INTEGER,
    gross_return        REAL,               -- (sell_price/buy_price) - 1
    net_return          REAL,               -- after round-trip costs

    PRIMARY KEY (run_id, date, ticker, direction),
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
);

-- ============================================================
-- TABLE 9: sector_health
-- Sector-level momentum aggregated from candidate sets.
-- Grain: snapshot_date + sector
-- Derived from holdings table — NOT from raw price data.
-- top-100 = raw momentum candidates, top-50 = FIP-filtered portfolio
-- Key research question: does FIP filter favor/exclude certain sectors?
-- ============================================================
CREATE TABLE IF NOT EXISTS sector_health (
    snapshot_date              TEXT    NOT NULL,
    sector                     TEXT    NOT NULL,

    -- ── Candidate counts ─────────────────────────────────────────────────
    stock_count_top100         INTEGER,    -- stocks in top-100 momentum pool
    stock_count_top50          INTEGER,    -- stocks surviving FIP filter

    -- ── Momentum aggregates (top-100 pool) ───────────────────────────────
    avg_mom_3m_top100          REAL,
    avg_mom_6m_top100          REAL,
    avg_mom_12_2_top100        REAL,
    avg_fip_top100             REAL,
    avg_acceleration_rank_top100 REAL,

    -- ── Momentum aggregates (top-50 final portfolio) ─────────────────────
    avg_mom_3m_top50           REAL,
    avg_mom_6m_top50           REAL,
    avg_mom_12_2_top50         REAL,
    avg_fip_top50              REAL,
    avg_acceleration_rank_top50 REAL,

    -- ── Concentration weights ─────────────────────────────────────────────
    -- sector_count / total_count at each stage
    sector_weight_top100       REAL,
    sector_weight_top50        REAL,

    -- ── FIP filter effect ─────────────────────────────────────────────────
    -- positive = sector survives FIP filter at higher rate than average
    -- negative = FIP filter removes this sector disproportionately
    fip_filter_retention_rate  REAL,   -- stock_count_top50 / stock_count_top100

    PRIMARY KEY (snapshot_date, sector)
);

-- ============================================================
-- TABLE 10: industry_health
-- Industry-level momentum aggregated from candidate sets.
-- Grain: snapshot_date + industry
-- Same design as sector_health but at finer granularity.
-- ============================================================
CREATE TABLE IF NOT EXISTS industry_health (
    snapshot_date              TEXT    NOT NULL,
    industry                   TEXT    NOT NULL,
    sector                     TEXT,           -- parent sector for joins

    -- ── Candidate counts ─────────────────────────────────────────────────
    stock_count_top100         INTEGER,
    stock_count_top50          INTEGER,

    -- ── Momentum aggregates (top-100) ────────────────────────────────────
    avg_mom_3m_top100          REAL,
    avg_mom_6m_top100          REAL,
    avg_mom_12_2_top100        REAL,
    avg_fip_top100             REAL,
    avg_acceleration_rank_top100 REAL,

    -- ── Momentum aggregates (top-50) ─────────────────────────────────────
    avg_mom_3m_top50           REAL,
    avg_mom_6m_top50           REAL,
    avg_mom_12_2_top50         REAL,
    avg_fip_top50              REAL,
    avg_acceleration_rank_top50 REAL,

    -- ── Concentration weights ─────────────────────────────────────────────
    industry_weight_top100     REAL,
    industry_weight_top50      REAL,

    -- ── FIP filter effect ─────────────────────────────────────────────────
    fip_filter_retention_rate  REAL,   -- stock_count_top50 / stock_count_top100

    PRIMARY KEY (snapshot_date, industry)
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Holdings: fast lookup by ticker across time
CREATE INDEX IF NOT EXISTS idx_holdings_ticker
    ON holdings(ticker, snapshot_date);

-- Holdings: sector analysis
CREATE INDEX IF NOT EXISTS idx_holdings_sector
    ON holdings(sector, snapshot_date);

-- Holdings: momentum bucket analysis
CREATE INDEX IF NOT EXISTS idx_holdings_bucket
    ON holdings(momentum_bucket, snapshot_date);

-- Price daily: fast time-series retrieval
CREATE INDEX IF NOT EXISTS idx_price_daily_ticker_date
    ON price_daily(ticker, date);

-- Weekly health: ticker trend queries
CREATE INDEX IF NOT EXISTS idx_weekly_health_ticker
    ON weekly_health(ticker, week_date);

-- Weekly health: exit flag queries
CREATE INDEX IF NOT EXISTS idx_weekly_health_flags
    ON weekly_health(flag_rank_below_75, flag_fip_worsened_005, week_date);

-- Forward returns: horizon analysis
CREATE INDEX IF NOT EXISTS idx_forward_returns_horizon
    ON forward_returns(horizon, snapshot_date);

-- Forward returns: ticker lookup
CREATE INDEX IF NOT EXISTS idx_forward_returns_ticker
    ON forward_returns(ticker, horizon);

-- Sector health: time-series per sector
CREATE INDEX IF NOT EXISTS idx_sector_health_sector
    ON sector_health(sector, snapshot_date);

-- Industry health: time-series per industry
CREATE INDEX IF NOT EXISTS idx_industry_health_industry
    ON industry_health(industry, snapshot_date);

-- Industry health: by parent sector
CREATE INDEX IF NOT EXISTS idx_industry_health_sector
    ON industry_health(sector, snapshot_date);

-- Backtest portfolio: equity curve retrieval
CREATE INDEX IF NOT EXISTS idx_backtest_portfolio_run
    ON backtest_portfolio(run_id, date);

-- Backtest trades: ticker P&L analysis
CREATE INDEX IF NOT EXISTS idx_backtest_trades_ticker
    ON backtest_trades(ticker, run_id);

-- ============================================================
-- VIEWS
-- ============================================================

-- Latest snapshot holdings with health flags (most recent week)
CREATE VIEW IF NOT EXISTS v_current_portfolio AS
SELECT
    h.ticker,
    h.sector,
    h.industry_clean,
    h.final_rank,
    h.mom_12_2,
    h.mom_3m,
    h.fip_score,
    h.pct_positive_days,
    h.momentum_acceleration_rank,
    h.momentum_bucket,
    h.price,
    h.adv_20d,
    h.market_cap,
    wh.current_rank,
    wh.rank_change_1w,
    wh.rank_change_4w,
    wh.fip_change_4w,
    wh.weeks_in_top_50,
    wh.rs_vs_spy_4w,
    wh.up_down_volume_ratio_current,
    wh.drawdown_from_peak_pct,
    -- Any exit flag firing
    (wh.flag_rank_below_75 + wh.flag_fip_worsened_005 +
     wh.flag_rs_negative_4w + wh.flag_below_50dma +
     wh.flag_drawdown_15pct) AS exit_flags_count
FROM holdings h
LEFT JOIN weekly_health wh
    ON h.ticker = wh.ticker
    AND wh.week_date = (
        SELECT MAX(week_date) FROM weekly_health WHERE ticker = h.ticker
    )
WHERE h.snapshot_date = (SELECT MAX(snapshot_date) FROM holdings);

-- Forward return summary by momentum bucket
CREATE VIEW IF NOT EXISTS v_returns_by_bucket AS
SELECT
    h.momentum_bucket,
    fr.horizon,
    COUNT(*)                        AS n,
    ROUND(AVG(fr.absolute_return),4)  AS avg_return,
    ROUND(AVG(fr.excess_return_vs_spy),4) AS avg_excess_vs_spy,
    ROUND(AVG(fr.max_drawdown),4)   AS avg_max_drawdown,
    ROUND(MIN(fr.absolute_return),4)  AS worst_return,
    ROUND(MAX(fr.absolute_return),4)  AS best_return
FROM forward_returns fr
JOIN holdings h
    ON fr.snapshot_date = h.snapshot_date
    AND fr.ticker = h.ticker
WHERE fr.completed = 1
GROUP BY h.momentum_bucket, fr.horizon
ORDER BY h.momentum_bucket, fr.horizon;

-- Sector rotation: where is FIP filtering happening?
CREATE VIEW IF NOT EXISTS v_sector_rotation AS
SELECT
    sh.sector,
    sh.stock_count_top100,
    sh.stock_count_top50,
    ROUND(sh.fip_filter_retention_rate, 3)  AS fip_retention,
    ROUND(sh.sector_weight_top100, 3)       AS weight_top100,
    ROUND(sh.sector_weight_top50, 3)        AS weight_top50,
    ROUND(sh.sector_weight_top50 - sh.sector_weight_top100, 3) AS weight_shift,
    ROUND(sh.avg_mom_12_2_top50, 3)         AS avg_mom_12_2,
    ROUND(sh.avg_fip_top50, 4)              AS avg_fip,
    ROUND(sh.avg_acceleration_rank_top50, 1) AS avg_accel_rank
FROM sector_health sh
WHERE sh.snapshot_date = (SELECT MAX(snapshot_date) FROM sector_health)
ORDER BY sh.sector_weight_top50 DESC;

-- Rank velocity leaderboard (current week)
CREATE VIEW IF NOT EXISTS v_rank_velocity AS
SELECT
    ticker,
    current_rank,
    rank_change_1w,
    rank_change_4w,
    weeks_in_top_50,
    fip_change_4w,
    rs_vs_spy_4w,
    exit_flags_count,
    CASE
        WHEN rank_change_4w <= -10 THEN 'accelerating'
        WHEN rank_change_4w >= 10  THEN 'decelerating'
        ELSE 'stable'
    END AS momentum_direction
FROM v_current_portfolio
ORDER BY rank_change_4w ASC;
