import json
import uuid
from collections import Counter

from ..errors import LedgerError, conflict, missing, require
from ..values import canonical, fingerprint, identifier, page_size, text, unique_ids, utc_now
from ..analysis.windows import normalize_request
from .contracts import contribution


class Withdrawals:
    """来源数据使用授权撤回。

    撤回不是取消：订单事件与退款流水原样保留用于内部对账，但撤回订单自撤回版本起
    不再参与任何新生成的统计，含其贡献的既有版本立即停止对外提供。
    """

    def __init__(self, database, intake):
        self.database = database
        self.intake = intake
        self.editions = None  # 由 Ledger 装配后注入，用于生成替代分析

    # ---------- 统计排除集 ----------

    def pairs_at(self, revision, connection):
        rows = connection.execute(
            "SELECT source, order_id FROM withdrawal_orders WHERE revision<=?", (revision,)).fetchall()
        return {(row["source"], row["order_id"]) for row in rows}

    def all_pairs(self, connection):
        rows = connection.execute("SELECT source, order_id FROM withdrawal_orders").fetchall()
        return {(row["source"], row["order_id"]) for row in rows}

    def edition_status(self, edition_id, connection):
        rows = connection.execute(
            "SELECT withdrawal_id FROM withdrawal_impacts WHERE edition_id=? ORDER BY withdrawal_id",
            (edition_id,)).fetchall()
        replacement = connection.execute(
            "SELECT replacement_id FROM withdrawal_replacements WHERE edition_id=?", (edition_id,)).fetchone()
        return {"affected": bool(rows), "withdrawals": [row["withdrawal_id"] for row in rows],
                "replacement_id": replacement["replacement_id"] if replacement else None}

    def require_clean(self, edition_id, connection, action):
        status = self.edition_status(edition_id, connection)
        require(not status["affected"], "withdrawn_contributions",
                f"该版本包含已撤回授权的数据贡献，不能{action}", 409, status)

    def require_servable(self, edition_id):
        with self.database.transaction() as connection:
            status = self.edition_status(edition_id, connection)
        require(not status["affected"], "withdrawn_data",
                "该版本包含已撤回授权的数据，已停止对外提供；撤回前已下载到外部的文件无法收回",
                410, status)

    # ---------- 查询 ----------

    def decode(self, row, include_summary=True):
        result = dict(row)
        result["scope"] = json.loads(result["scope"])
        summary = json.loads(result.pop("orders_summary"))
        if include_summary:
            result["orders_summary"] = summary
        else:
            result["order_count"] = summary["order_count"]
            result["event_count"] = summary["event_count"]
        return result

    def get(self, withdrawal_id, connection=None):
        identifier(withdrawal_id, "withdrawal_id")
        if connection is None:
            with self.database.transaction() as opened:
                return self.get(withdrawal_id, opened)
        row = connection.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if row is None:
            raise missing("撤回", withdrawal_id)
        result = self.decode(row)
        counts = {item["state"]: item["n"] for item in connection.execute(
            "SELECT state, COUNT(*) n FROM withdrawal_impacts WHERE withdrawal_id=? GROUP BY state",
            (withdrawal_id,)).fetchall()}
        result["progress"] = {"impacts": sum(counts.values()), "pending": counts.get("pending", 0),
                              "replaced": counts.get("replaced", 0), "skipped": counts.get("skipped", 0)}
        return result

    def list(self, source=None, state=None, limit=100, offset=0):
        page_size(limit, offset)
        clauses = []
        parameters = []
        if source is not None:
            identifier(source)
            clauses.append("source=?")
            parameters.append(source)
        if state is not None:
            require(state in ("processing", "completed"), "invalid_state", "撤回状态无效")
            clauses.append("state=?")
            parameters.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM withdrawals" + where + " ORDER BY created_at, id LIMIT ? OFFSET ?",
                parameters + [limit, offset]).fetchall()
        return [self.decode(row, include_summary=False) for row in rows]

    # ---------- 提交 ----------

    def commit(self, source, request_key, reason, actor="operator", order_ids=None, batch_ids=None,
               autoprocess=True):
        identifier(source, "source")
        identifier(request_key, "request_key")
        reason = text(reason, "撤回原因", 1000)
        actor = text(actor, "操作人", 100)
        order_ids = unique_ids(order_ids or [], "order_ids", allow_empty=True, maximum=1000)
        batch_ids = unique_ids(batch_ids or [], "batch_ids", allow_empty=True, maximum=200)
        require(order_ids or batch_ids, "empty_scope", "撤回范围需要至少一个订单或导入批次")
        scope = {"order_ids": order_ids, "batch_ids": batch_ids}
        scope_digest = fingerprint({"source": source, "reason": reason, **scope})
        with self.database.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM withdrawals WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing["scope_digest"] != scope_digest:
                    raise conflict("撤回请求号已用于不同的撤回内容")
                withdrawal_id = existing["id"]
                replayed = True
            else:
                replayed = False
                self.intake.catalog.get("source", source, connection=connection)
                orders = set(order_ids)
                batches = []
                for batch_id in batch_ids:
                    batch = connection.execute(
                        "SELECT * FROM batches WHERE source=? AND id=?", (source, batch_id)).fetchone()
                    if batch is None:
                        raise missing("批次", f"{source}:{batch_id}")
                    batches.append(dict(batch))
                    rows = connection.execute(
                        "SELECT DISTINCT order_id FROM events WHERE source=? AND revision=?",
                        (source, batch["revision"])).fetchall()
                    orders.update(row["order_id"] for row in rows)
                for order_id in order_ids:
                    found = connection.execute(
                        "SELECT 1 FROM events WHERE source=? AND order_id=? LIMIT 1",
                        (source, order_id)).fetchone()
                    if found is None:
                        raise missing("订单", f"{source}:{order_id}")
                prior = {row["order_id"]: row for row in connection.execute(
                    "SELECT order_id, withdrawal_id, revision FROM withdrawal_orders WHERE source=?",
                    (source,)).fetchall()}
                summaries = []
                totals = Counter()
                event_count = 0
                for order_id in sorted(orders):
                    rows = connection.execute(
                        "SELECT * FROM events WHERE source=? AND order_id=? ORDER BY sequence",
                        (source, order_id)).fetchall()
                    head = json.loads(rows[-1]["payload"])
                    head_contribution = contribution(head)
                    totals.update(head_contribution)
                    event_count += len(rows)
                    previous = prior.get(order_id)
                    summaries.append({
                        "order_id": order_id, "events": len(rows), "head_sequence": head["sequence"],
                        "head_event_id": head["event_id"], "state": head["state"],
                        "origin": head["origin"], "destination": head["destination"],
                        "travel_day": head["travel_day"], "head_digest": rows[-1]["digest"],
                        "head_contribution": head_contribution,
                        "already_withdrawn": previous["withdrawal_id"] if previous else None})
                summary = {"order_count": len(orders), "event_count": event_count,
                           "batches": [batch["id"] for batch in batches],
                           "head_contribution_totals": dict(totals), "orders": summaries}
                revision = self.database.advance(
                    connection, "withdrawal.commit", f"{source}:{request_key}", actor,
                    {"source": source, "request_key": request_key, "reason": reason, "scope": scope,
                     "orders": sorted(orders), "order_count": len(orders)})
                withdrawal_id = "wd-" + uuid.uuid4().hex
                created = utc_now()
                connection.execute(
                    "INSERT INTO withdrawals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (withdrawal_id, request_key, source, reason, actor, canonical(scope), scope_digest,
                     canonical(summary), "processing", revision, None, created, None))
                for order_id in sorted(orders):
                    connection.execute(
                        "INSERT OR IGNORE INTO withdrawal_orders VALUES (?,?,?,?)",
                        (source, order_id, withdrawal_id, revision))
                # 影响扫描与提交同事务：撤回一经提交，受影响版本立即被拦截
                targets = {(source, order_id) for order_id in orders}
                impacted = 0
                for edition in connection.execute("SELECT id, data_revision, result FROM editions").fetchall():
                    lineage = json.loads(edition["result"])["lineage"]
                    pairs = {(item["source"], item["order_id"]) for item in lineage}
                    if pairs & targets:
                        connection.execute(
                            "INSERT OR IGNORE INTO withdrawal_impacts VALUES (?,?,?,?,?,?)",
                            (withdrawal_id, edition["id"], edition["data_revision"], "pending", None, created))
                        impacted += 1
                if not impacted:
                    connection.execute(
                        "UPDATE withdrawals SET state='completed', completed_at=? WHERE id=?",
                        (created, withdrawal_id))
        result = self.get(withdrawal_id)
        if autoprocess and result["state"] == "processing":
            # 提交后或重试中断的撤回：继续完成影响处理，不重复撤回
            result = self.process(withdrawal_id)
        return {**result, "replayed": replayed}

    # ---------- 影响处理（可断点续跑） ----------

    def process(self, withdrawal_id):
        identifier(withdrawal_id, "withdrawal_id")
        try:
            self._sweeps(withdrawal_id)
        except LedgerError as exc:
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE withdrawals SET last_error=? WHERE id=?",
                                   (f"{exc.code}: {exc.message}", withdrawal_id))
        return self.get(withdrawal_id)

    def _sweeps(self, withdrawal_id, limit=100):
        for _ in range(limit):
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT state FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
                if row is None:
                    raise missing("撤回", withdrawal_id)
                state = row["state"]
                impacts = [dict(item) for item in connection.execute(
                    "SELECT * FROM withdrawal_impacts WHERE withdrawal_id=? ORDER BY edition_id",
                    (withdrawal_id,)).fetchall()]
                current = self.database.revision(connection)
                actionable = None
                for impact in impacts:
                    if impact["state"] == "pending":
                        actionable = ("resolve", impact)
                        break
                    if impact["state"] == "replaced":
                        replacement = connection.execute(
                            "SELECT state, data_revision FROM editions WHERE id=?",
                            (impact["replacement_id"],)).fetchone()
                        tainted = self.edition_status(impact["replacement_id"], connection)["affected"]
                        if (replacement is None or tainted or replacement["data_revision"] != current
                                or replacement["state"] in ("rejected", "retired")):
                            actionable = ("reset", impact)
                            break
            if actionable is None:
                if state != "completed":
                    with self.database.transaction(write=True) as connection:
                        connection.execute(
                            "UPDATE withdrawals SET state='completed', completed_at=?, last_error=NULL WHERE id=?",
                            (utc_now(), withdrawal_id))
                return
            kind, impact = actionable
            if kind == "reset":
                with self.database.transaction(write=True) as connection:
                    connection.execute(
                        "UPDATE withdrawal_impacts SET state='pending', replacement_id=NULL "
                        "WHERE withdrawal_id=? AND edition_id=?",
                        (withdrawal_id, impact["edition_id"]))
                continue
            edition = self.editions.get(impact["edition_id"], include_result=False)
            if edition["state"] not in ("approved", "published"):
                with self.database.transaction(write=True) as connection:
                    connection.execute(
                        "UPDATE withdrawal_impacts SET state='skipped' WHERE withdrawal_id=? AND edition_id=?",
                        (withdrawal_id, impact["edition_id"]))
                continue
            request = {key: value for key, value in edition["request"].items() if key != "revision"}
            digest = fingerprint(normalize_request(request))
            with self.database.transaction() as connection:
                now = self.database.revision(connection)
                found = connection.execute(
                    "SELECT id FROM editions WHERE request_digest=? AND data_revision=? "
                    "AND state IN ('draft','approved','published') ORDER BY created_at, id LIMIT 1",
                    (digest, now)).fetchone()
            if found:
                replacement_id = found["id"]
            else:
                key = f"wd-{withdrawal_id}:{impact['edition_id']}"
                replacement_id = self.editions.create(key, request)["id"]
            with self.database.transaction(write=True) as connection:
                replacement = connection.execute(
                    "SELECT data_revision FROM editions WHERE id=?", (replacement_id,)).fetchone()
                connection.execute(
                    "UPDATE withdrawal_impacts SET state='replaced', replacement_id=? "
                    "WHERE withdrawal_id=? AND edition_id=?",
                    (replacement_id, withdrawal_id, impact["edition_id"]))
                connection.execute(
                    "INSERT INTO withdrawal_replacements VALUES (?,?,?,?,?) "
                    "ON CONFLICT(edition_id) DO UPDATE SET replacement_id=excluded.replacement_id, "
                    "withdrawal_id=excluded.withdrawal_id, revision=excluded.revision, "
                    "created_at=excluded.created_at",
                    (impact["edition_id"], replacement_id, withdrawal_id,
                     replacement["data_revision"], utc_now()))
        raise LedgerError("withdrawal_processing", "撤回影响处理未能在限定步数内收敛，请重试", 409)

    def resume_pending(self):
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM withdrawals WHERE state='processing' ORDER BY created_at, id").fetchall()
        resumed = []
        for row in rows:
            try:
                self.process(row["id"])
                resumed.append(row["id"])
            except Exception as exc:  # 启动恢复不中断服务，错误留在撤回记录上
                try:
                    with self.database.transaction(write=True) as connection:
                        connection.execute("UPDATE withdrawals SET last_error=? WHERE id=?",
                                           (f"resume_failed: {exc}", row["id"]))
                except Exception:
                    pass
        return resumed

    # ---------- 影响清单 ----------

    def impact(self, withdrawal_id):
        withdrawal = self.get(withdrawal_id)
        with self.database.transaction() as connection:
            impacts = connection.execute(
                "SELECT * FROM withdrawal_impacts WHERE withdrawal_id=? ORDER BY edition_id",
                (withdrawal_id,)).fetchall()
            current = self.database.revision(connection)
            editions = []
            impacted_ids = []
            replacement_ids = set()
            for impact in impacts:
                edition = self.editions.get(impact["edition_id"], connection, include_result=False)
                impacted_ids.append(impact["edition_id"])
                live = connection.execute(
                    "SELECT replacement_id FROM withdrawal_replacements WHERE edition_id=?",
                    (impact["edition_id"],)).fetchone()
                linked = live["replacement_id"] if live else impact["replacement_id"]
                replacement = None
                if linked:
                    found = self.editions.get(linked, connection, include_result=False)
                    replacement_ids.add(linked)
                    replacement = {"id": found["id"], "state": found["state"], "stale": found["stale"],
                                   "data_revision": found["data_revision"],
                                   "publishable": found["quality"]["publishable"],
                                   "linked_by_this_withdrawal": impact["replacement_id"] == linked}
                editions.append({"edition_id": impact["edition_id"], "name": edition["name"],
                                 "state": edition["state"], "data_revision": edition["data_revision"],
                                 "impact_state": impact["state"], "replacement": replacement})
            channels = []
            pointers = connection.execute("SELECT * FROM channels ORDER BY name").fetchall()
            history = connection.execute(
                "SELECT DISTINCT channel, edition_id FROM publication_history").fetchall()
            affected = {row["name"] for row in pointers if row["edition_id"] in impacted_ids}
            affected |= {row["channel"] for row in history if row["edition_id"] in impacted_ids}
            for name in sorted(affected):
                pointer = next((dict(row) for row in pointers if row["name"] == name), None)
                suspended = pointer is not None and pointer["edition_id"] in impacted_ids
                channels.append({
                    "channel": name, "status": "suspended" if suspended else "restored",
                    "generation": pointer["generation"] if pointer else None,
                    "current_edition_id": pointer["edition_id"] if pointer else None,
                    "current_is_replacement": bool(pointer) and pointer["edition_id"] in replacement_ids})
            archives = []
            for edition_id in impacted_ids:
                row = connection.execute(
                    "SELECT id, name, state, result_digest FROM editions WHERE id=?",
                    (edition_id,)).fetchone()
                archives.append({"edition_id": edition_id, "name": row["name"], "state": row["state"],
                                 "result_digest": row["result_digest"], "blocked": True,
                                 "artifacts": ["export.json", "export.zip", "tables/*.csv",
                                               "coverage.csv", "lineage.csv"]})
        return {"withdrawal": withdrawal, "current_revision": current,
                "orders": withdrawal["orders_summary"]["orders"],
                "editions": editions, "channels": channels, "archives": archives,
                "enforcement": {
                    "public_channels": "含撤回贡献的版本不再通过 /public 与导出接口返回",
                    "internal_replay": "订单事件与历史版本保留用于内部对账与审计重放，重放不等于对外可用",
                    "channel_restore": "替代版本通过既有审核与发布流程后频道恢复"},
                "limitations": [
                    "已经下载、转发或缓存到账本之外的文件无法收回，归档摘要仅用于识别这些外部副本",
                    "撤回不修改既有订单事件与退款流水，内部对账口径保持不变",
                    "被排除订单对应的日期不能解释为零需求，替代分析在质量检查中披露该限制"]}
