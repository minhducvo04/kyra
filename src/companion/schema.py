from sqlalchemy import Boolean, CheckConstraint, Column, Float, Integer, MetaData, String, Table, Text, UniqueConstraint
metadata = MetaData()
outbound_audit = Table(
    "outbound_audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(64), nullable=False, index=True),
    Column("destination", String(64), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("accepted", Boolean, nullable=False),
    Column("would_refuse", Boolean, nullable=False),
    Column("tier", Integer, nullable=False),
    Column("classes", Text, nullable=False),
    Column("refused", Text, nullable=False),
    Column("flags", Text, nullable=False),
    Column("bytes", Integer, nullable=False),
    Column("request_id", String(64), nullable=False),
    Column("policy_version", Integer, nullable=False),
)
checkpoint_revisions = Table(
    "checkpoint_revisions", metadata,
    Column("id", String(36), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("task", Text, nullable=False),
    Column("last_result", Text, nullable=False),
    Column("next_action", Text, nullable=False),
    Column("references", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
    CheckConstraint("revision > 0", name="positive_checkpoint_revision"),
)
tool_runs = Table(
    "tool_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tool", String(128), nullable=False),
    Column("args", Text, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("summary", String(300), nullable=False),
    Column("error", Text),
    Column("started_at", String(64), nullable=False),
    Column("duration_ms", Float, nullable=False),
)
