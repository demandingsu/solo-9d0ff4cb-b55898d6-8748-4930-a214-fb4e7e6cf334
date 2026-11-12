"""报价变更采集 API 的离线测试。

- 直接以 file:// 抓取 quote-site-v1（基线）与 quote-site-v2（变更版本）；
- 以临时 SQLite 数据库验证基线、差异、幂等、不完整保留原快照与并发一致。
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.scraper import IncompleteSnapshot, scrape_complete_snapshot
from app.snapshots import SnapshotStore

ROOT = Path(__file__).resolve().parent.parent
V1 = (ROOT / "quote-site-v1" / "index.html").as_uri()
V2 = (ROOT / "quote-site-v2" / "index.html").as_uri()


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SnapshotStore(self.tmp / "snapshots.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plans(self, url: str):
        details, visited = scrape_complete_snapshot(url)
        from app.scraper import plan_payload

        return [dict(plan_payload(detail, url)) for detail in details], visited

    def test_first_complete_collection_is_baseline(self) -> None:
        plans, visited = self._plans(V1)
        outcome = self.store.commit_snapshot("src", V1, plans, visited)
        self.assertEqual(outcome.status, "baseline_created")
        self.assertIsNone(outcome.previous_snapshot_id)
        self.assertEqual(outcome.changes, [])
        self.assertEqual(outcome.plan_count, 5)

    def test_v2_reports_price_add_remove_and_new_detail_url(self) -> None:
        plans_v1, visited_v1 = self._plans(V1)
        self.store.commit_snapshot("src", V1, plans_v1, visited_v1)
        plans_v2, visited_v2 = self._plans(V2)
        outcome = self.store.commit_snapshot("src", V2, plans_v2, visited_v2)
        self.assertEqual(outcome.status, "changed")

        changes = {(change.plan_id, change.change_type, change.field): change for change in outcome.changes}
        self.assertEqual(changes[("P006", "added", None)].new_detail_url.rsplit("/", 1)[-1], "p006.html")
        self.assertEqual(changes[("P002", "removed", None)].old_detail_url.rsplit("/", 1)[-1], "p002.html")
        # 改价：月基本费与谷段费率均变化
        self.assertEqual(changes[("P001", "changed", "base_fee_cny")].old_value, 60.0)
        self.assertEqual(changes[("P001", "changed", "base_fee_cny")].new_value, 65.0)
        self.assertIn(("P001", "changed", "valley_tiers"), changes)
        # 详情地址变化：P003 的详情页改址
        self.assertTrue(changes[("P003", "changed", "detail_url")].new_detail_url.endswith("p003-v2.html"))
        # 供应商变化
        self.assertEqual(changes[("P005", "changed", "supplier")].new_value, "安能新能源")
        # 未变化的套餐不应产生记录
        self.assertNotIn(("P004", "changed", "base_fee_cny"), changes)

    def test_same_content_twice_is_unchanged_and_keeps_snapshot(self) -> None:
        plans, visited = self._plans(V1)
        first = self.store.commit_snapshot("src", V1, plans, visited)
        second = self.store.commit_snapshot("src", V1, plans, visited)
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.snapshot_id, first.snapshot_id)
        self.assertEqual(second.changes, [])

    def test_incomplete_collection_keeps_previous_snapshot(self) -> None:
        plans, visited = self._plans(V1)
        first = self.store.commit_snapshot("src", V1, plans, visited)
        issues = [{"url": V2, "stage": "detail", "reason": "模拟详情失败", "status": 500}]
        self.store.record_failed_attempt("src", V2, issues)
        current = self.store.current_snapshot("src")
        self.assertEqual(current.snapshot_id, first.snapshot_id)
        self.assertEqual(len(current.plans), 5)


class StrictScrapeTest(unittest.TestCase):
    def test_both_versions_are_complete(self) -> None:
        for url in (V1, V2):
            with self.subTest(url=url):
                details, visited = scrape_complete_snapshot(url)
                self.assertEqual(len(visited), 2)
                self.assertTrue(details)

    def test_broken_list_item_is_reported_not_dropped(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(ROOT / "quote-site-v1", tmp / "site")
            index = tmp / "site" / "index.html"
            text = index.read_text(encoding="utf-8")
            index.write_text(
                text.replace('data-plan-id="P001"', 'data-plan-id=""', 1),
                encoding="utf-8",
            )
            with self.assertRaises(IncompleteSnapshot) as ctx:
                scrape_complete_snapshot((tmp / "site" / "index.html").as_uri())
            self.assertTrue(any("列表项无法解析" in issue.reason for issue in ctx.exception.issues))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_next_page_is_reported(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(ROOT / "quote-site-v1", tmp / "site")
            index = tmp / "site" / "index.html"
            text = index.read_text(encoding="utf-8")
            index.write_text(
                text.replace('<a rel="next" href="index-2.html">下一页 &raquo;</a>', ""),
                encoding="utf-8",
            )
            with self.assertRaises(IncompleteSnapshot) as ctx:
                scrape_complete_snapshot((tmp / "site" / "index.html").as_uri())
            self.assertTrue(any("分页未到终页" in issue.reason for issue in ctx.exception.issues))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unparseable_card_mixed_with_valid_cards_is_reported(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(ROOT / "quote-site-v1", tmp / "site")
            index = tmp / "site" / "index.html"
            text = index.read_text(encoding="utf-8")
            # 在有效卡片之间插入一张缺少套餐编号的卡片
            marker = '  <article class="plan-card" data-plan-id="P002">'
            broken = (
                '  <article class="plan-card">\n'
                '    <h3><a class="plan-link" href="plans/p001.html">缺少编号的套餐</a></h3>\n'
                '    <div class="plan-meta"><span class="tag tag-region" data-region="上海">上海</span>\n'
                '    <span class="tag" data-supplier="未知">供应商：未知</span></div>\n'
                "  </article>\n"
            )
            index.write_text(text.replace(marker, broken + marker, 1), encoding="utf-8")
            with self.assertRaises(IncompleteSnapshot) as ctx:
                scrape_complete_snapshot((tmp / "site" / "index.html").as_uri())
            self.assertTrue(any("列表项无法解析" in issue.reason for issue in ctx.exception.issues))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_key_field_missing_is_reported(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(ROOT / "quote-site-v1", tmp / "site")
            detail = tmp / "site" / "plans" / "p001.html"
            text = detail.read_text(encoding="utf-8")
            detail.write_text(
                text.replace('<span data-supplier>安能电力</span>', ""),
                encoding="utf-8",
            )
            with self.assertRaises(IncompleteSnapshot) as ctx:
                scrape_complete_snapshot((tmp / "site" / "index.html").as_uri())
            issues = ctx.exception.issues
            self.assertTrue(any("关键字段缺失" in issue.reason and "supplier" in issue.reason for issue in issues))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_detail_is_reported(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(ROOT / "quote-site-v1", tmp / "site")
            (tmp / "site" / "plans" / "p001.html").unlink()
            with self.assertRaises(IncompleteSnapshot) as ctx:
                scrape_complete_snapshot((tmp / "site" / "index.html").as_uri())
            self.assertTrue(
                any(issue.stage == "detail" and issue.status == 404 for issue in ctx.exception.issues)
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        main._snapshot_store = None
        self._old_path = main.os.environ.get("SNAPSHOT_DB_PATH")
        main.os.environ["SNAPSHOT_DB_PATH"] = str(self.tmp / "api.sqlite3")
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main._snapshot_store = None
        if self._old_path is None:
            main.os.environ.pop("SNAPSHOT_DB_PATH", None)
        else:
            main.os.environ["SNAPSHOT_DB_PATH"] = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, url: str):
        response = self.client.post(
            "/api/v1/quote-changes",
            json={"source_key": "src", "list_url": url},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_api_baseline_then_changes_then_idempotent(self) -> None:
        baseline = self._post(V1)
        self.assertEqual(baseline["status"], "baseline_created")
        self.assertEqual(baseline["changes"], [])
        self.assertEqual(baseline["plan_count"], 5)
        self.assertEqual(baseline["issues"], [])

        changed = self._post(V2)
        self.assertEqual(changed["status"], "changed")
        self.assertEqual(changed["previous_snapshot_id"], baseline["snapshot_id"])
        kinds = {(c["plan_id"], c["change_type"]) for c in changed["changes"]}
        self.assertIn(("P006", "added"), kinds)
        self.assertIn(("P002", "removed"), kinds)
        self.assertIn(("P001", "changed"), kinds)

        again = self._post(V2)
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(again["changes"], [])
        self.assertEqual(again["snapshot_id"], changed["snapshot_id"])

    def test_api_incomplete_returns_issues_and_keeps_snapshot(self) -> None:
        self._post(V1)
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(ROOT / "quote-site-v1", tmp / "site")
            (tmp / "site" / "plans" / "p001.html").unlink()
            broken_url = (tmp / "site" / "index.html").as_uri()
            response = self._post(broken_url)
            self.assertEqual(response["status"], "incomplete")
            self.assertEqual(len(response["issues"]), 1)
            self.assertTrue(response["issues"][0]["url"].endswith("p001.html"))
            self.assertEqual(response["issues"][0]["status"], 404)
            # 原快照保留，套餐数不变
            self.assertEqual(response["plan_count"], 5)
            self.assertFalse(any(c["change_type"] == "removed" for c in response["changes"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_api_concurrent_same_source_is_consistent(self) -> None:
        results: list[dict] = []
        lock = threading.Lock()

        def worker() -> None:
            response = self.client.post(
                "/api/v1/quote-changes", json={"source_key": "concurrent", "list_url": V1}
            )
            with lock:
                results.append(response.json())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = sorted(item["status"] for item in results)
        # 并发采集同一内容：恰好一次建立基线，其余全部 unchanged
        self.assertEqual(statuses.count("baseline_created"), 1, statuses)
        self.assertEqual(statuses.count("unchanged"), 4, statuses)
        baseline = next(item for item in results if item["status"] == "baseline_created")
        self.assertTrue(all(item["snapshot_id"] == baseline["snapshot_id"] for item in results))
        self.assertTrue(all(item["changes"] == [] for item in results))


if __name__ == "__main__":
    unittest.main()
