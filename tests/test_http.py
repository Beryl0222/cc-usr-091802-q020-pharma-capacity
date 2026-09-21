"""HTTP 契约测试：启动内存服务并走真实套接字。"""

import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from pharma.app import Application
from pharma.scenario import build_demo
from pharma.store import EventStore
from service import Handler, health_payload


def _start_server():
    store = EventStore()
    build_demo(store)
    app = Application(store)
    Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _request(method, url, body=None):
    # 对查询串中的中文做百分号编码（路径为 ASCII）
    if "://" in url:
        prefix, _, query = url.partition("?")
        if query:
            query = urllib.parse.urlencode(
                urllib.parse.parse_qsl(query, keep_blank_values=True))
            url = prefix + "?" + query
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = _start_server()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_health(self):
        status, payload = _request("GET", self.base + "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, health_payload())

    def test_summary_and_drilldown(self):
        status, payload = _request(
            "GET", self.base +
            "/summary?parks=张江,苏州,临港&year=2029&as_of=2030-03-01")
        self.assertEqual(status, 200)
        self.assertEqual(payload["totals"]["counted_target_revenue"], 120.0)
        # 数字可下钻：去重项目与产能依据
        row = next(d for d in payload["drilldown"] if d["project_id"] == "p-a")
        self.assertEqual(row["capacity_basis"][0]["counted_qty"], 5_800_000)

    def test_gate_blocks_financing_only_project(self):
        status, payload = _request(
            "GET", self.base + "/projects/p-c/gate?as_of=2030-01-15")
        self.assertEqual(status, 200)
        self.assertFalse(payload["can_advance"])
        reqs = {b["requirement"] for b in payload["blockers"]}
        self.assertIn("investment-agreement", reqs)

    def test_command_rejects_overcapacity(self):
        status, payload = _request(
            "POST", self.base + "/commands/declare_occupancy",
            {"user_id": "u-admin", "occupancy_id": "x", "project_id": "p-c",
             "line_id": "l-fill", "year": 2029, "unit": "vial/年",
             "qty": 1, "occurred_at": "2030-01-12"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "occupancy-rejected")

    def test_impact_and_disposition_flow(self):
        status, payload = _request(
            "GET", self.base + "/impact?kind=supplier&id=s-api&as_of=2030-02-16")
        self.assertEqual(status, 200)
        self.assertEqual(payload["affected"][0]["project_id"], "p-b")
        self.assertTrue(payload["affected"][0]["pending_owner_confirmation"])
        # 非负责人被拒
        status, denied = _request("POST", self.base + "/dispositions", {
            "user_id": "u-wang", "node_kind": "supplier", "node_id": "s-api",
            "project_id": "p-b", "action": "switch", "occurred_at": "2030-02-17",
            "alternative": {"kind": "supplier", "ref_id": "s-api2"}})
        self.assertEqual(status, 400)
        self.assertEqual(denied["error"], "forbidden")
        # 负责人确认
        status, ok = _request("POST", self.base + "/dispositions", {
            "user_id": "u-li", "node_kind": "supplier", "node_id": "s-api",
            "project_id": "p-b", "action": "switch", "occurred_at": "2030-02-17",
            "alternative": {"kind": "supplier", "ref_id": "s-api2"},
            "note": "切换至备选供应商乙"})
        self.assertEqual(status, 200)
        self.assertTrue(ok["ok"])

    def test_report_reproduction_endpoint(self):
        status, payload = _request(
            "GET", self.base + "/reports/r2028/reproduce")
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["report_id"], "r2028")
        self.assertEqual(payload["summary"]["totals"]["project_count"], 2)

    def test_commercial_access(self):
        url = self.base + "/projects/p-a?as_of=2030-03-01"
        _, anon = _request("GET", url)
        _, authed = _request("GET", url + "&user=u-zhang")
        self.assertEqual(anon["gate"]["evidence"]["investment-agreement"], [])
        self.assertEqual(authed["gate"]["evidence"]["investment-agreement"],
                         ["ev-ia-a"])


if __name__ == "__main__":
    unittest.main()
