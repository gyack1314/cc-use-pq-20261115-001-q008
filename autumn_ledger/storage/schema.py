SCHEMA_VERSION = 1

STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS ledger_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL, revision INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO ledger_meta VALUES (1,1,0)",
    "CREATE TABLE IF NOT EXISTS audit (revision INTEGER PRIMARY KEY, action TEXT NOT NULL, identity TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS catalog (kind TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,id,version))",
    "CREATE INDEX IF NOT EXISTS catalog_revision ON catalog(kind,id,revision)",
    "CREATE TABLE IF NOT EXISTS calendars (id TEXT NOT NULL, version INTEGER NOT NULL, revision INTEGER NOT NULL, city TEXT NOT NULL, start_day TEXT NOT NULL, end_day TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(id,version))",
    "CREATE INDEX IF NOT EXISTS calendar_city_revision ON calendars(city,revision)",
    "CREATE TABLE IF NOT EXISTS coverage (source TEXT NOT NULL, city TEXT NOT NULL, day TEXT NOT NULL, version INTEGER NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL, PRIMARY KEY(source,city,day,version))",
    "CREATE INDEX IF NOT EXISTS coverage_revision ON coverage(revision,source,city,day)",
    "CREATE TABLE IF NOT EXISTS batches (source TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL, accepted INTEGER NOT NULL, duplicates INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(source,id))",
    "CREATE TABLE IF NOT EXISTS events (source TEXT NOT NULL, event_id TEXT NOT NULL, order_id TEXT NOT NULL, sequence INTEGER NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(source,event_id), UNIQUE(source,order_id,sequence))",
    "CREATE INDEX IF NOT EXISTS event_order ON events(source,order_id,sequence DESC)",
    "CREATE INDEX IF NOT EXISTS event_snapshot ON events(revision,source,order_id)",
    "CREATE TABLE IF NOT EXISTS editions (id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL, name TEXT NOT NULL, data_revision INTEGER NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','approved','rejected','published','retired')), version INTEGER NOT NULL, result TEXT NOT NULL, result_digest TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS edition_revision ON editions(data_revision,state)",
    "CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, edition_id TEXT NOT NULL REFERENCES editions(id), edition_version INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS channels (name TEXT PRIMARY KEY, edition_id TEXT NOT NULL REFERENCES editions(id), generation INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS publication_history (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, generation INTEGER NOT NULL, edition_id TEXT NOT NULL REFERENCES editions(id), actor TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(channel,generation))",
    "CREATE TABLE IF NOT EXISTS withdrawals (id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL, scope TEXT NOT NULL, scope_digest TEXT NOT NULL, orders_summary TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('processing','completed')), revision INTEGER NOT NULL, last_error TEXT, created_at TEXT NOT NULL, completed_at TEXT)",
    "CREATE TABLE IF NOT EXISTS withdrawal_orders (source TEXT NOT NULL, order_id TEXT NOT NULL, withdrawal_id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(source,order_id))",
    "CREATE INDEX IF NOT EXISTS withdrawal_order_revision ON withdrawal_orders(revision,source,order_id)",
    "CREATE TABLE IF NOT EXISTS withdrawal_impacts (withdrawal_id TEXT NOT NULL, edition_id TEXT NOT NULL, data_revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','replaced','skipped')), replacement_id TEXT, created_at TEXT NOT NULL, PRIMARY KEY(withdrawal_id,edition_id))",
    "CREATE INDEX IF NOT EXISTS withdrawal_impact_edition ON withdrawal_impacts(edition_id)",
    "CREATE TABLE IF NOT EXISTS withdrawal_replacements (edition_id TEXT PRIMARY KEY, replacement_id TEXT NOT NULL, withdrawal_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL)",
)


def initialize(connection):
    for statement in STATEMENTS:
        connection.execute(statement)
    row = connection.execute("SELECT schema_version FROM ledger_meta WHERE singleton=1").fetchone()
    if row[0] != SCHEMA_VERSION:
        raise RuntimeError("数据库模式版本不受支持")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("数据库外键检查失败")
