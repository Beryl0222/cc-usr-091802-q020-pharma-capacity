"""HTTP 端点契约测试（使用真实样例数据在本地临时端口启动）。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from service import make_handler, build_registry

FIXTURE = "fixtures/sample.json"


class ServiceHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = make_handler(build_registry(FIXTURE))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=headers or {})
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path, payload, expected_error=False):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "pharma-capacity")

    def test_projects_list(self):
        _, body = self._get("/projects")
        self.assertEqual(len(body["projects"]), 5)

    def test_advance_gate_returns_409(self):
        status, body = self._post("/projects/proj-huixin-fill/advance", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "GateError")
        self.assertIn("regulatory_license", body["missing"])

    def test_advance_success(self):
        # 独立装载的数据（每个 make_handler 独立注册表），通过补证据后推进
        # 这里直接用一个全新服务实例
        from registry import Registry
        reg = Registry.load(FIXTURE)
        handler = make_handler(reg)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            reg.project("proj-boda-pack")["evidence"].append({
                "id": "cap", "kind": "actual_capacity", "status": "verified",
                "recorded_on": "2026-09-20", "amount": 90, "window_days": 180})
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/projects/proj-boda-pack/advance",
                data=json.dumps({"decided_on": "2026-09-21"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read())
            self.assertEqual(body["stage"], "production")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_conflicts_endpoint(self):
        _, body = self._get("/reservations/conflicts")
        self.assertTrue(body["conflicts"])
        self.assertEqual(body["conflicts"][0]["facility_id"], "fac-line-a")

    def test_totals_drilldown(self):
        _, body = self._get("/totals?scope=region&id=csj")
        self.assertGreaterEqual(body["project_count"], 2)
        row = body["projects"][0]
        for key in ("capacity_basis", "decisions", "pending_items", "stage"):
            self.assertIn(key, row)

    def test_report_reproduction(self):
        _, body = self._get("/reports/rpt-2025/reproduce")
        self.assertTrue(body["reproduced"])
        self.assertIn("totals", body)

    def test_authorization_header(self):
        _, hidden = self._get("/projects/proj-boda-pack")
        self.assertGreater(hidden["redacted_commercial_evidence"], 0)
        _, visible = self._get("/projects/proj-boda-pack",
                               {"X-Principal-Id": "u-bai"})
        self.assertEqual(visible["redacted_commercial_evidence"], 0)

    def test_impacts_and_disposition_flow(self):
        _, body = self._get("/impacts?supplier=sup-nordic")
        ids = {p["project_id"] for p in body["affected_projects"]}
        self.assertIn("proj-boda-pack", ids)
        self.assertIn("proj-yuanze-cgt", ids)

        status, bad = self._post("/dispositions", {
            "project_id": "proj-boda-pack", "issue_id": "ISS-001",
            "owner": "", "action": "switch_supplier"})
        self.assertEqual(status, 400)

        status, ok = self._post("/dispositions", {
            "project_id": "proj-boda-pack", "issue_id": "ISS-001",
            "owner": "周敏", "action": "switch_supplier",
            "alternative_id": "sup-aosen"})
        self.assertEqual(status, 201)
        self.assertEqual(ok["disposition"]["status"], "confirmed")

    def test_event_creates_version(self):
        status, body = self._post("/events", {
            "project_id": "proj-anrong-fill", "event": "extension",
            "effective_on": "2026-10-01",
            "changes": {"note": "外部里程碑延期登记"},
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["version"]["event"], "extension")

    def test_as_of_temporal_query(self):
        _, current = self._get("/projects/proj-yuanze-cgt")
        # 样例业务日 2026-09-21：延期已生效，并购 2027 才生效故不可见
        self.assertEqual(current["version"]["event"], "extension")
        self.assertEqual(current["version"]["effective_on"], "2026-08-01")
        _, historical = self._get(
            "/projects/proj-yuanze-cgt?effective=2026-07-01&recorded=2026-07-01")
        self.assertEqual(historical["version"]["version"], 1)
        _, post_merger = self._get(
            "/projects/proj-yuanze-cgt?effective=2027-02-01&recorded=2027-02-01")
        self.assertEqual(post_merger["version"]["event"], "merger")
        self.assertEqual(post_merger["version"]["enterprise_id"], "ent-beimu")


if __name__ == "__main__":
    unittest.main()
