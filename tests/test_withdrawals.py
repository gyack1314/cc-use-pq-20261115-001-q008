from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from autumn_ledger.application import Ledger
from autumn_ledger.api.server import create_app
from autumn_ledger.demo import request_data, synthetic_event
from autumn_ledger.errors import LedgerError
from tests.support import LedgerCase


class WithdrawalTests(LedgerCase):
    def withdraw(self, key="wd-1", order_ids=None, batch_ids=None, autoprocess=True, reason="来源撤回数据使用授权"):
        return self.ledger.withdrawals.commit("alpha", key, reason, "compliance",
                                              order_ids, batch_ids, autoprocess)

    def test_commit_is_idempotent_and_validates_scope(self):
        first = self.withdraw(order_ids=["a2"])
        self.assertFalse(first["replayed"])
        self.assertEqual(first["state"], "completed")
        self.assertEqual(first["progress"]["impacts"], 0)
        replay = self.withdraw(order_ids=["a2"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(len(self.ledger.withdrawals.list()), 1)
        self.assertError("conflict", self.withdraw, order_ids=["a3"])
        self.assertEqual(len(self.ledger.withdrawals.list()), 1)
        self.assertError("empty_scope", self.withdraw, key="wd-2")
        self.assertError("not_found", self.withdraw, key="wd-3", order_ids=["missing"])
        self.assertError("not_found", self.withdraw, key="wd-4", batch_ids=["missing-batch"])
        self.assertError("invalid_state", self.ledger.withdrawals.list, state="unknown")
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_withdrawal_keeps_events_for_internal_reconciliation(self):
        before_history = self.ledger.intake.history("alpha", "a2")
        before_revision = self.ledger.database.revision()
        record = self.withdraw(order_ids=["a2"])
        self.assertGreater(self.ledger.database.revision(), before_revision)
        # 不修改既有订单事件，不伪造退款
        self.assertEqual(self.ledger.intake.history("alpha", "a2"), before_history)
        self.assertEqual(len(self.ledger.intake.history("alpha", "a2")), 1)
        receipts = self.ledger.receipts.inspect("alpha")
        self.assertTrue(receipts["ok"])
        self.assertEqual(receipts["events"], 5)
        self.assertEqual(receipts["withdrawn_orders"], 1)
        item = next(row for row in receipts["orders"] if row["order_id"] == "a2")
        self.assertEqual(item["withdrawal"], record["id"])
        audit = self.ledger.database.audit(limit=500)
        self.assertEqual(audit[-1]["action"], "withdrawal.commit")
        self.assertEqual(audit[-1]["actor"], "compliance")
        self.assertEqual(audit[-1]["payload"]["reason"], "来源撤回数据使用授权")
        summary = record["orders_summary"]
        self.assertEqual(summary["order_count"], 1)
        self.assertEqual(summary["orders"][0]["head_contribution"]["net_cents"], 50000)
        self.assertEqual(summary["head_contribution_totals"]["net_cents"], 50000)
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_new_analysis_excludes_withdrawn_but_old_snapshot_replays(self):
        edition = self.edition()
        self.withdraw(order_ids=["a2"])
        fresh = self.ledger.analysis.run(request_data())
        current = fresh["periods"]["current"]
        self.assertEqual(current["cube"]["totals"]["orders"], 2)
        self.assertEqual(current["cube"]["totals"]["net_cents"], 70000)
        self.assertEqual(current["withdrawn"]["excluded_orders"], 1)
        codes = {row["code"] for row in fresh["quality"]["issues"]}
        self.assertIn("withdrawn_excluded", codes)
        self.assertTrue(fresh["quality"]["publishable"])
        flagged = [row for row in current["coverage"] if row.get("withdrawn_orders")]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["day"], "2026-11-17")
        # 旧版本的内部重放保持不变，与对外可用性区分
        self.assertTrue(self.ledger.editions.verify(edition["id"])["ok"])
        preview = self.ledger.analysis.preview(request_data(revision=edition["data_revision"]))
        self.assertEqual(preview["current"]["orders"], 3)
        # 但不能用固定旧版本生成包含撤回贡献的新统计版本
        self.assertError("withdrawn_contributions", self.ledger.editions.create,
                         "pinned", request_data(revision=edition["data_revision"]))

    def test_public_serving_and_exports_stop_immediately(self):
        edition = self.edition()
        self.publish(edition)
        self.assertEqual(self.ledger.export.public_channel("weekly")
                         ["result"]["periods"]["current"]["cube"]["totals"]["orders"], 3)
        record = self.withdraw(order_ids=["a2"])
        error = self.assertError("withdrawn_data", self.ledger.export.public_channel, "weekly")
        self.assertEqual(error.status, 410)
        self.assertIn("无法收回", error.message)
        self.assertError("withdrawn_data", self.ledger.export.document, edition["id"])
        self.assertError("withdrawn_data", self.ledger.export.bundle, edition["id"])
        self.assertError("withdrawn_data", self.ledger.export.table, edition["id"], "origins")
        self.assertError("withdrawn_data", self.ledger.export.coverage, edition["id"])
        self.assertError("withdrawn_data", self.ledger.export.lineage, edition["id"])
        stored = self.ledger.editions.get(edition["id"])
        self.assertTrue(stored["withdrawal"]["affected"])
        self.assertEqual(stored["withdrawal"]["withdrawals"], [record["id"]])
        self.assertTrue(self.ledger.review.channel("weekly")["suspended"])
        self.assertTrue(self.ledger.review.channels()[0]["suspended"])
        readiness = self.ledger.publication_readiness.inspect(edition["id"])
        self.assertIn("withdrawn_data", readiness["blockers"])
        self.assertFalse(self.ledger.publication_readiness.published_consistency()["ok"])
        # 内部读取与调查仍然可用
        self.assertEqual(self.ledger.investigation.order_trace(edition["id"], "alpha", "a2")
                         ["included_in_periods"][0]["period"], "current")

    def test_tainted_edition_cannot_be_approved_or_published(self):
        draft = self.edition("one")
        approved_edition = self.edition("two", name="另一分析")
        approved = self.approve(approved_edition)
        self.withdraw(order_ids=["a2"])
        self.assertError("withdrawn_contributions", self.approve, draft)
        self.assertError("withdrawn_contributions", self.ledger.review.publish,
                         approved_edition["id"], "weekly", approved["version"], 0, "publisher")
        self.assertEqual(self.ledger.review.channels(), [])

    def test_replacement_recomputes_and_channel_restores_after_review(self):
        edition = self.edition()
        self.publish(edition)
        record = self.withdraw(order_ids=["a2"])
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["progress"],
                         {"impacts": 1, "pending": 0, "replaced": 1, "skipped": 0})
        impact = self.ledger.withdrawals.impact(record["id"])
        self.assertEqual(impact["channels"][0]["channel"], "weekly")
        self.assertEqual(impact["channels"][0]["status"], "suspended")
        self.assertEqual(impact["archives"][0]["edition_id"], edition["id"])
        self.assertTrue(impact["archives"][0]["blocked"])
        self.assertTrue(any("无法收回" in note for note in impact["limitations"]))
        replacement = impact["editions"][0]["replacement"]
        self.assertIsNotNone(replacement)
        self.assertTrue(replacement["linked_by_this_withdrawal"])
        replaced = self.ledger.editions.get(replacement["id"])
        totals = replaced["result"]["periods"]["current"]["cube"]["totals"]
        self.assertEqual(totals["orders"], 2)
        self.assertEqual(totals["net_cents"], 70000)
        # 总量、分母、比较与质量全部重算，不是删行保留原合计
        self.assertEqual(replaced["result"]["periods"]["current"]["withdrawn"]["excluded_orders"], 1)
        lineage = {(row["source"], row["order_id"]) for row in replaced["result"]["lineage"]}
        self.assertNotIn(("alpha", "a2"), lineage)
        self.assertNotEqual(replaced["result_digest"], edition["result_digest"])
        self.assertTrue(replaced["result"]["quality"]["publishable"])
        comparison = replaced["result"]["comparison"]["totals"]["orders"]
        self.assertEqual(comparison["current"], 2)
        # 替代版通过既有审核发布流程后频道恢复
        approved = self.ledger.review.decide(replacement["id"], "approve", 1, "analyst", "替代版本核对")
        published = self.ledger.review.publish(replacement["id"], "weekly", approved["version"], 1, "publisher")
        self.assertEqual(published["generation"], 2)
        current = self.ledger.export.public_channel("weekly")
        self.assertEqual(current["result"]["periods"]["current"]["cube"]["totals"]["orders"], 2)
        impact = self.ledger.withdrawals.impact(record["id"])
        self.assertEqual(impact["channels"][0]["status"], "restored")
        self.assertTrue(impact["channels"][0]["current_is_replacement"])
        self.assertTrue(self.ledger.publication_readiness.published_consistency()["ok"])

    def test_batch_scope_covers_later_revisions_of_its_orders(self):
        self.edition()
        self.ledger.intake.ingest("alpha", "correction", [synthetic_event("a1", sequence=2, refund=10000)])
        record = self.withdraw(batch_ids=["synthetic-initial"])
        summary = record["orders_summary"]
        self.assertEqual(summary["order_count"], 5)
        self.assertEqual(summary["event_count"], 6)
        fresh = self.ledger.analysis.run(request_data())
        self.assertEqual(fresh["periods"]["current"]["cube"]["totals"]["orders"], 0)
        self.assertEqual(fresh["periods"]["current"]["withdrawn"]["excluded_orders"], 3)
        # 撤回后到达的后续修订同样不参与统计，但事件保留
        self.ledger.intake.ingest("alpha", "later", [synthetic_event("a1", sequence=3, refund=20000)])
        fresh = self.ledger.analysis.run(request_data())
        self.assertEqual(fresh["periods"]["current"]["cube"]["totals"]["orders"], 0)
        self.assertEqual(len(self.ledger.intake.history("alpha", "a1")), 3)
        self.assertTrue(self.ledger.receipts.inspect("alpha")["ok"])

    def test_replacement_quality_gate_keeps_channel_suspended(self):
        edition = self.edition()
        self.publish(edition)
        record = self.withdraw(batch_ids=["synthetic-initial"])
        impact = self.ledger.withdrawals.impact(record["id"])
        replacement = impact["editions"][0]["replacement"]
        self.assertIsNotNone(replacement)
        self.assertFalse(replacement["publishable"])
        self.assertError("quality_blocked", self.ledger.review.decide,
                         replacement["id"], "approve", 1, "analyst", "尝试通过")
        self.assertError("withdrawn_data", self.ledger.export.public_channel, "weekly")
        self.assertEqual(self.ledger.withdrawals.impact(record["id"])["channels"][0]["status"], "suspended")

    def test_restart_resumes_processing_without_duplicates(self):
        first = self.edition("one")
        second = self.edition("two", name="另一分析")
        self.publish(first)
        self.publish(second, channel="daily")
        record = self.withdraw(order_ids=["a2"], autoprocess=False)
        self.assertEqual(record["state"], "processing")
        self.assertEqual(record["progress"]["pending"], 2)
        restarted = Ledger(self.path)
        resumed = restarted.withdrawals.get(record["id"])
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(resumed["progress"]["replaced"], 2)
        again = restarted.withdrawals.process(record["id"])
        self.assertEqual(again["progress"]["replaced"], 2)
        impact = restarted.withdrawals.impact(record["id"])
        replacement_ids = [row["replacement"]["id"] for row in impact["editions"]]
        self.assertEqual(len(set(replacement_ids)), 2)
        self.assertEqual(len(restarted.editions.list()), 4)
        self.assertTrue(restarted.database.integrity()["ok"])

    def test_replay_of_interrupted_withdrawal_continues_processing(self):
        self.publish(self.edition())
        record = self.withdraw(order_ids=["a2"], autoprocess=False)
        self.assertEqual(record["state"], "processing")
        replay = self.withdraw(order_ids=["a2"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["state"], "completed")
        self.assertEqual(replay["progress"]["replaced"], 1)
        self.assertEqual(len(self.ledger.withdrawals.list()), 1)
        self.assertEqual(len(self.ledger.editions.list()), 2)

    def test_crash_between_replacement_create_and_link_is_healed(self):
        edition = self.edition()
        self.publish(edition)
        record = self.withdraw(order_ids=["a2"], autoprocess=False)
        # 模拟崩溃：替代版已按同一请求创建，但影响记录尚未关联
        request = request_data()
        manual = self.ledger.editions.create("wd-manual", request)
        processed = self.ledger.withdrawals.process(record["id"])
        self.assertEqual(processed["state"], "completed")
        impact = self.ledger.withdrawals.impact(record["id"])
        self.assertEqual(impact["editions"][0]["replacement"]["id"], manual["id"])
        self.assertEqual(len(self.ledger.editions.list()), 2)

    def test_later_withdrawal_supersedes_earlier_replacement(self):
        edition = self.edition()
        self.publish(edition)
        first = self.withdraw(key="wd-1", order_ids=["a2"])
        impact = self.ledger.withdrawals.impact(first["id"])
        first_replacement = impact["editions"][0]["replacement"]["id"]
        # 第二次撤回命中第一次的替代版，处理生成新的替代并更新指针
        second = self.withdraw(key="wd-2", order_ids=["a3"])
        self.assertEqual(second["state"], "completed")
        stored = self.ledger.editions.get(first_replacement)
        self.assertTrue(stored["withdrawal"]["affected"])
        self.assertError("withdrawn_contributions", self.ledger.review.decide,
                         first_replacement, "approve", 1, "analyst", "过期替代")
        impact = self.ledger.withdrawals.impact(second["id"])
        current = next(row for row in impact["editions"] if row["edition_id"] == edition["id"])
        latest = current["replacement"]["id"]
        self.assertNotEqual(latest, first_replacement)
        totals = self.ledger.editions.get(latest)["result"]["periods"]["current"]["cube"]["totals"]
        self.assertEqual(totals["orders"], 1)
        self.assertEqual(totals["net_cents"], 50000)
        # 第一次撤回的影响清单也反映最新替代，不漏掉后来识别出的引用
        first_impact = self.ledger.withdrawals.impact(first["id"])
        row = next(row for row in first_impact["editions"] if row["edition_id"] == edition["id"])
        self.assertEqual(row["replacement"]["id"], latest)
        self.assertFalse(row["replacement"]["linked_by_this_withdrawal"])

    def test_overlapping_withdrawals_do_not_duplicate_orders(self):
        first = self.withdraw(key="wd-1", order_ids=["a2"])
        second = self.withdraw(key="wd-2", order_ids=["a2", "a3"])
        orders = {row["order_id"]: row for row in second["orders_summary"]["orders"]}
        self.assertEqual(orders["a2"]["already_withdrawn"], first["id"])
        self.assertIsNone(orders["a3"]["already_withdrawn"])
        with self.ledger.database.transaction() as connection:
            early = self.ledger.withdrawals.pairs_at(first["revision"], connection)
            late = self.ledger.withdrawals.pairs_at(second["revision"], connection)
        self.assertIn(("alpha", "a2"), early)
        self.assertNotIn(("alpha", "a3"), early)
        self.assertIn(("alpha", "a3"), late)
        self.assertEqual(len(self.ledger.withdrawals.list()), 2)

    def test_withdrawal_races_with_publish_leave_no_serving_old_content(self):
        edition = self.edition()
        approved = self.approve(edition)

        def publish(_):
            try:
                return Ledger(self.path).review.publish(edition["id"], "weekly", approved["version"], 0, "publisher")
            except LedgerError as exc:
                return exc.code

        def withdraw(_):
            return Ledger(self.path).withdrawals.commit("alpha", "wd-race", "授权撤回", "compliance", ["a2"], [])

        with ThreadPoolExecutor(max_workers=2) as pool:
            published, record = list(pool.map(lambda i: publish(i) if i == 0 else withdraw(i), range(2)))
        self.assertEqual(record["state"], "completed")
        if isinstance(published, str):
            self.assertIn(published, ("withdrawn_contributions", "stale_edition", "conflict"))
        else:
            self.assertEqual(published["generation"], 1)
        # 无论谁先提交，公开频道都不能继续返回含撤回贡献的旧内容
        try:
            self.ledger.export.public_channel("weekly")
            self.fail("撤回提交后公开频道不应返回含撤回贡献的内容")
        except LedgerError as exc:
            self.assertIn(exc.code, ("not_found", "withdrawn_data"))
        impact = self.ledger.withdrawals.impact(record["id"])
        self.assertEqual(impact["editions"][0]["edition_id"], edition["id"])

    def test_process_endpoint_and_http_surface(self):
        app = create_app(self.path)
        with TestClient(app) as client:
            edition = self.edition()
            self.publish(edition)
            response = client.post("/sources/alpha/withdrawals", json={
                "request_key": "wd-http", "reason": "来源撤回授权", "order_ids": ["a2"],
                "actor": "compliance"})
            self.assertEqual(response.status_code, 200, response.text)
            record = response.json()
            self.assertEqual(record["state"], "completed")
            replay = client.post("/sources/alpha/withdrawals", json={
                "request_key": "wd-http", "reason": "来源撤回授权", "order_ids": ["a2"]})
            self.assertTrue(replay.json()["replayed"])
            conflict = client.post("/sources/alpha/withdrawals", json={
                "request_key": "wd-http", "reason": "不同内容", "order_ids": ["a3"]})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(client.get("/public/weekly").status_code, 410)
            detail = client.get(f"/withdrawals/{record['id']}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["progress"]["replaced"], 1)
            impact = client.get(f"/withdrawals/{record['id']}/impact")
            self.assertEqual(impact.status_code, 200)
            self.assertEqual(impact.json()["channels"][0]["status"], "suspended")
            again = client.post(f"/withdrawals/{record['id']}/process")
            self.assertEqual(again.status_code, 200)
            self.assertEqual(again.json()["state"], "completed")
            listing = client.get("/withdrawals", params={"source": "alpha"})
            self.assertEqual(len(listing.json()), 1)
            self.assertEqual(listing.json()[0]["order_count"], 1)
            self.assertEqual(client.get("/withdrawals/wd-none").status_code, 404)
            invalid = client.post("/sources/alpha/withdrawals", json={"request_key": "wd-bad", "reason": "x"})
            self.assertEqual(invalid.status_code, 422)
