-- Asteria-Malf-Pas MVP SQLite schema (WAL)。
-- 分三库：market / malf_pas / backtest。本文件用 ATTACH 风格无关的纯表定义，
-- 由 db.py 针对各库连接分别执行对应段落（用 -- @db: 标记分库）。

-- ========================================================================
-- @db: market   行情库 market.sqlite
-- ========================================================================
CREATE TABLE IF NOT EXISTS instrument (
    symbol       TEXT PRIMARY KEY,   -- '600000.SH'
    name         TEXT,
    exchange     TEXT,               -- SH / SZ / BJ
    board        TEXT,               -- main / star / chinext / bse / unknown
    first_dt     TEXT,
    last_dt      TEXT,
    list_status  TEXT                -- active / unknown
);

CREATE TABLE IF NOT EXISTS price_bar (
    symbol      TEXT NOT NULL,
    bar_dt      TEXT NOT NULL,        -- ISO date 'YYYY-MM-DD'
    price_line  TEXT NOT NULL,        -- 'qfq_back' | 'raw_none'
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    PRIMARY KEY (symbol, bar_dt, price_line)
);
CREATE INDEX IF NOT EXISTS idx_price_bar_symbol_dt ON price_bar(symbol, bar_dt);

CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date  TEXT PRIMARY KEY,
    is_open     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ingest_source_file (
    source_file_key   TEXT PRIMARY KEY,   -- vendor|asset|price_line|symbol|hash16
    symbol            TEXT NOT NULL,
    asset_type        TEXT NOT NULL,
    price_line        TEXT NOT NULL,
    source_path       TEXT NOT NULL,
    source_size_bytes INTEGER,
    source_mtime      TEXT,
    source_content_hash TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    bar_count         INTEGER NOT NULL
);

-- ========================================================================
-- @db: malf_pas   结构/posture 快照库 malf_pas.sqlite (append-only)
-- ========================================================================
CREATE TABLE IF NOT EXISTS malf_core_snapshot (
    snapshot_id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                         TEXT NOT NULL,
    timeframe                      TEXT NOT NULL,
    bar_dt                         TEXT NOT NULL,
    system_state                   TEXT NOT NULL,
    active_wave_id                 INTEGER,
    old_wave_id                    INTEGER,
    direction                      TEXT,
    wave_core_state                TEXT,
    current_effective_guard_pivot_id INTEGER,
    current_effective_guard_price  REAL,
    progress_extreme_pivot_id      INTEGER,
    progress_extreme_price         REAL,
    open_transition_id             INTEGER,
    active_candidate_guard_pivot_id INTEGER,
    active_candidate_direction     TEXT,
    transition_boundary_high       REAL,
    transition_boundary_low        REAL,
    core_rule_version              TEXT,
    pivot_detection_rule_version   TEXT,
    source_run_id                  TEXT,
    UNIQUE(symbol, timeframe, bar_dt, source_run_id)
);
CREATE INDEX IF NOT EXISTS idx_core_snap_sym_dt ON malf_core_snapshot(symbol, timeframe, bar_dt);

-- 结构事件账本（pivot/wave/break/transition/candidate），供可视化叠加。
CREATE TABLE IF NOT EXISTS malf_pivot (
    pivot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- 'H' | 'L'
    extreme_bar_dt  TEXT NOT NULL,      -- 极值所在 bar
    confirm_bar_dt  TEXT NOT NULL,      -- 确认所在 bar（延迟 k 根）
    price           REAL NOT NULL,
    pivot_seq_in_bar INTEGER NOT NULL DEFAULT 0,
    primitive       TEXT,               -- 'HH'|'HL'|'LL'|'LH'|NULL
    pivot_detection_rule_version TEXT,
    source_run_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pivot_sym_dt ON malf_pivot(symbol, timeframe, confirm_bar_dt);

CREATE TABLE IF NOT EXISTS malf_wave (
    wave_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    direction       TEXT NOT NULL,      -- 'up' | 'down'
    start_bar_dt    TEXT NOT NULL,
    start_pivot_id  INTEGER,
    end_bar_dt      TEXT,               -- terminated 时填
    wave_core_state TEXT NOT NULL,      -- 'alive' | 'terminated'
    source_run_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_wave_sym ON malf_wave(symbol, timeframe);

CREATE TABLE IF NOT EXISTS malf_break (
    break_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    old_wave_id         INTEGER NOT NULL,
    old_direction       TEXT NOT NULL,
    broken_guard_pivot_id INTEGER,
    broken_guard_price  REAL,
    break_bar_dt        TEXT NOT NULL,
    break_price         REAL,
    source_run_id       TEXT
);

CREATE TABLE IF NOT EXISTS malf_transition (
    transition_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    old_wave_id         INTEGER NOT NULL,
    old_direction       TEXT NOT NULL,
    open_bar_dt         TEXT NOT NULL,
    boundary_high       REAL,
    boundary_low        REAL,
    resolved_bar_dt     TEXT,           -- new wave 确认时
    new_wave_id         INTEGER,
    source_run_id       TEXT
);

CREATE TABLE IF NOT EXISTS malf_candidate (
    candidate_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    transition_id       INTEGER NOT NULL,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    candidate_pivot_id  INTEGER,
    candidate_direction TEXT,
    appear_bar_dt       TEXT NOT NULL,
    replaced            INTEGER NOT NULL DEFAULT 0,
    source_run_id       TEXT
);

CREATE TABLE IF NOT EXISTS wave_position (
    wave_position_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    bar_dt              TEXT NOT NULL,
    wave_id             INTEGER,
    old_wave_id         INTEGER,
    system_state        TEXT,
    wave_core_state     TEXT,
    direction           TEXT,
    new_count           INTEGER,
    no_new_span         INTEGER,
    transition_span     INTEGER,
    update_rank         REAL,
    stagnation_rank     REAL,
    life_state          TEXT,
    position_quadrant   TEXT,
    birth_type          TEXT,
    candidate_wait_span INTEGER,
    candidate_replacement_count INTEGER,
    confirmation_distance_abs REAL,
    confirmation_distance_pct REAL,
    sample_version      TEXT,
    lifespan_rule_version TEXT,
    source_run_id       TEXT,
    UNIQUE(symbol, timeframe, bar_dt, source_run_id)
);
CREATE INDEX IF NOT EXISTS idx_wavepos_sym_dt ON wave_position(symbol, timeframe, bar_dt);

CREATE TABLE IF NOT EXISTS wave_behavior_snapshot (
    wave_behavior_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    bar_dt              TEXT NOT NULL,
    wave_id             INTEGER,
    direction           TEXT,
    old_wave_id         INTEGER,
    open_transition_id  INTEGER,
    continuation_regime           TEXT,
    directional_continuity_regime TEXT,
    stagnation_regime             TEXT,
    boundary_pressure_regime      TEXT,
    transition_regime             TEXT,
    birth_quality_regime          TEXT,
    reason_codes        TEXT,
    lineage_hash        TEXT,
    malf_v1_4_rule_version TEXT,
    malf_v1_5_rule_version TEXT,
    source_run_id       TEXT,
    UNIQUE(symbol, timeframe, bar_dt, source_run_id)
);
CREATE INDEX IF NOT EXISTS idx_behavior_sym_dt ON wave_behavior_snapshot(symbol, timeframe, bar_dt);

CREATE TABLE IF NOT EXISTS pas_core_snapshot (
    core_snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    bar_dt              TEXT NOT NULL,
    malf_wave_position_id     INTEGER,
    wave_behavior_snapshot_id INTEGER,
    directional_premise TEXT,
    read_status         TEXT,
    strength_evidence   TEXT,
    weakness_evidence   TEXT,
    ambiguity_evidence  TEXT,
    tst_posture         TEXT,
    bof_posture         TEXT,
    bpb_posture         TEXT,
    pb_posture          TEXT,
    cpb_posture         TEXT,
    judgment_reason         TEXT,
    posture_derivation_reason TEXT,
    premise_mapping_branch  TEXT,
    posture_theorem_branch  TEXT,
    pas_core_rule_version   TEXT,
    source_run_id       TEXT,
    UNIQUE(symbol, timeframe, bar_dt, source_run_id)
);
CREATE INDEX IF NOT EXISTS idx_pas_core_sym_dt ON pas_core_snapshot(symbol, timeframe, bar_dt);

CREATE TABLE IF NOT EXISTS pas_lifespan_record (
    lifespan_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    bar_dt              TEXT NOT NULL,
    core_snapshot_id    INTEGER,
    setup_family        TEXT NOT NULL,
    lifespan_state      TEXT NOT NULL,
    state_reason        TEXT,
    pas_core_rule_version TEXT,
    malf_wave_position_id     INTEGER,
    wave_behavior_snapshot_id INTEGER,
    lifespan_rule_version TEXT,
    source_run_id       TEXT
);

CREATE TABLE IF NOT EXISTS pas_lifespan_transition (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lifespan_id         INTEGER NOT NULL,
    from_state          TEXT,
    to_state            TEXT,
    trigger             TEXT,
    trigger_reason      TEXT,
    bar_dt              TEXT,
    core_snapshot_id_at_transition INTEGER
);

-- ========================================================================
-- @db: backtest   回测库 backtest.sqlite
-- ========================================================================
CREATE TABLE IF NOT EXISTS param_set (
    param_set_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    params_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_run (
    run_id          TEXT PRIMARY KEY,
    param_set_id    INTEGER,
    group_name      TEXT,               -- initial | validation | holdout
    start_dt        TEXT,
    end_dt          TEXT,
    universe_filter TEXT,
    created_at      TEXT,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS bt_trade (
    trade_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    entry_dt        TEXT,
    exit_dt         TEXT,
    entry_price     REAL,
    avg_exit_price  REAL,
    qty             REAL,
    realized_pnl    REAL,
    R_multiple      REAL,
    exit_reason     TEXT,
    signal_candidate_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bt_trade_run ON bt_trade(run_id);

CREATE TABLE IF NOT EXISTS bt_equity_curve (
    run_id          TEXT NOT NULL,
    bar_dt          TEXT NOT NULL,
    equity          REAL,
    cash            REAL,
    open_positions  INTEGER,
    PRIMARY KEY (run_id, bar_dt)
);

CREATE TABLE IF NOT EXISTS bt_metrics (
    run_id          TEXT PRIMARY KEY,
    total_return    REAL,
    cagr            REAL,
    max_drawdown    REAL,
    sharpe          REAL,
    win_rate        REAL,
    avg_R           REAL,
    expectancy      REAL,
    trade_count     INTEGER,
    profit_factor   REAL
);

CREATE TABLE IF NOT EXISTS signal_candidate (
    signal_candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    symbol          TEXT NOT NULL,
    discover_dt     TEXT NOT NULL,
    setup_family    TEXT,
    directional_premise TEXT,
    read_status     TEXT,
    planned_entry   REAL,
    planned_stop    REAL,
    planned_target1 REAL,
    reward_risk     REAL,
    decision        TEXT,               -- accept | reject
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sigcand_run ON signal_candidate(run_id);
CREATE INDEX IF NOT EXISTS idx_sigcand_dt ON signal_candidate(discover_dt);
