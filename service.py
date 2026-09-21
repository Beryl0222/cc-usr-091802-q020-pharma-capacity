"""医药园区能力兑现——项目依赖服务入口。

在原有健康检查之上提供只读查询与少量受控写入端点：

* ``GET  /health``
* ``GET  /projects`` / ``GET /projects/{id}``（商业材料按 ``X-Principal-Id`` 授权）
* ``POST /projects/{id}/advance``             阶段推进（门禁不满足返回 409）
* ``POST /events``                            并购/迁移/延期/口径修订，生效起形成新版本
* ``GET  /reservations/conflicts``            共享产线时间×能力单位冲突
* ``GET  /facilities/{id}/occupancy``         指定日占用与余量
* ``GET  /totals``                            园区/区域合计，去重后逐项可下钻
* ``GET  /reports`` / ``GET /reports/{id}/reproduce``
* ``GET  /impacts``                           供应商中断/审批延迟的影响与可选资源
* ``POST /dispositions``                      负责人确认处置
* ``GET  /decisions``                         阶段决定留痕

默认数据文件为 ``fixtures/sample.json``（仅含虚构样例）。写入只保存在内存中。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from domain import AsOf, GateError, STAGE_LABELS
from registry import Registry

SERVICE_ID = "pharma-capacity"
SERVICE_NAME = "医药园区能力兑现"

DEFAULT_DATA = "fixtures/sample.json"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _as_of(params) -> AsOf | None:
    effective = params.get("effective", [None])[0]
    recorded = params.get("recorded", [None])[0]
    if effective and recorded:
        return AsOf.parse(effective, recorded)
    return None


def make_handler(registry: Registry):
    class Handler(BaseHTTPRequestHandler):
        """项目依赖服务的 HTTP 路由。"""

        # ------------------------------------------------------------------
        # 工具
        # ------------------------------------------------------------------

        def _send(self, code: int, payload: dict | list) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code: int, message: str, **extra) -> None:
            self._send(code, {"error": "error", "message": message, **extra})

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _principal(self) -> str | None:
            return self.headers.get("X-Principal-Id") or parse_qs(
                urlparse(self.path).query
            ).get("as", [None])[0]

        # ------------------------------------------------------------------
        # 路由
        # ------------------------------------------------------------------

        def do_GET(self):
            parsed = urlparse(self.path)
            path, params = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)
            try:
                if path == "/health":
                    self._send(200, health_payload())
                elif path == "/projects":
                    self._list_projects(params)
                elif path.startswith("/projects/"):
                    self._project_detail(path.split("/")[2], params)
                elif path == "/reservations/conflicts":
                    self._send(200, {"conflicts": self.registry.conflicts(_as_of(params))})
                elif path.startswith("/facilities/") and path.endswith("/occupancy"):
                    fid = path.split("/")[2]
                    day = params.get("date", [None])[0]
                    self._send(200, self.registry.facility_occupancy(fid, day))
                elif path == "/totals":
                    self._totals(params)
                elif path == "/reports":
                    self._send(200, {"reports": self.registry.reports()})
                elif path.startswith("/reports/") and path.endswith("/reproduce"):
                    self._send(200, self.registry.reproduce_report(path.split("/")[2]))
                elif path == "/impacts":
                    self._impacts(params)
                elif path == "/decisions":
                    pid = params.get("project", [None])[0]
                    self._send(200, {"decisions": self.registry.decisions(pid)})
                else:
                    self._error(404, f"未知端点：{path}")
            except KeyError as exc:
                self._error(404, str(exc).strip("'"))
            except LookupError as exc:
                self._error(404, str(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(400, str(exc))

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                body = self._json_body()
                if path.startswith("/projects/") and path.endswith("/advance"):
                    pid = path.split("/")[2]
                    result = self.registry.advance(pid, body.get("decided_on"))
                    decision = result["decision"]
                    self._send(200, {"decision": decision,
                                     "stage": result["state"].stage,
                                     "stage_label": STAGE_LABELS[result["state"].stage]})
                elif path == "/events":
                    new = self.registry.new_version(
                        body["project_id"], body["event"], body["effective_on"],
                        body.get("changes", {}),
                        recorded_on=body.get("recorded_on"),
                        note=body.get("note", ""),
                    )
                    self._send(201, {"version": new})
                elif path == "/dispositions":
                    record = self.registry.confirm_disposition(
                        body["project_id"], body["issue_id"], body["owner"],
                        body["action"], alternative_id=body.get("alternative_id"),
                        note=body.get("note", ""), decided_on=body.get("decided_on"),
                    )
                    self._send(201, {"disposition": record})
                else:
                    self._error(404, f"未知端点：{path}")
            except GateError as exc:
                self._send(409, {"error": "GateError", "message": str(exc),
                                 "project_id": exc.project_id,
                                 "from_stage": exc.from_stage,
                                 "from_stage_label": STAGE_LABELS[exc.from_stage],
                                 "missing": exc.missing})
            except KeyError as exc:
                self._error(404, str(exc).strip("'"))
            except LookupError as exc:
                self._error(404, str(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(400, str(exc))

        # ------------------------------------------------------------------
        # 处理函数
        # ------------------------------------------------------------------

        def _list_projects(self, params) -> None:
            as_of = _as_of(params)
            rows = []
            for proj in self.registry.projects:
                try:
                    state = self.registry.state(proj["id"], as_of)
                except LookupError:
                    continue
                rows.append({
                    "project_id": proj["id"],
                    "name": state.version.get("name", proj.get("name", proj["id"])),
                    "stage": state.stage,
                    "stage_label": STAGE_LABELS[state.stage],
                    "park_ids": state.version.get("park_ids", []),
                    "joint": bool(state.version.get("joint")),
                    "missing_gates": state.missing_exit_gates(),
                    "open_issue_count": len(state.open_issues),
                })
            self._send(200, {"projects": rows,
                             "as_of": self._as_of_label(as_of)})

        def _project_detail(self, pid: str, params) -> None:
            self._send(200, self.registry.project_detail(
                pid, _as_of(params), principal_id=self._principal()))

        def _totals(self, params) -> None:
            scope = params.get("scope", ["region"])[0]
            scope_id = params.get("id", ["ALL"])[0]
            year = params.get("year", [None])[0]
            caliber = params.get("caliber", [None])[0]
            total = self.registry.totals(
                scope=scope, scope_id=scope_id,
                year=int(year) if year else None,
                as_of=_as_of(params), caliber_id=caliber,
            )
            self._send(200, self.registry.redact_total(total, self._principal()))

        def _impacts(self, params) -> None:
            result = self.registry.analyze_impact(
                supplier_id=params.get("supplier", [None])[0],
                project_id=params.get("project", [None])[0],
                issue_id=params.get("issue", [None])[0],
                window_days=int(params.get("window", ["90"])[0]),
            )
            self._send(200, result)

        @staticmethod
        def _as_of_label(as_of) -> dict:
            if as_of is None:
                return {"mode": "current"}
            return {"mode": "as_of", "effective": as_of.effective.isoformat(),
                    "recorded": as_of.recorded.isoformat()}

        def log_message(self, *_args):
            return

    Handler.registry = registry
    return Handler


def build_registry(path: str) -> Registry:
    return Registry.load(path)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=DEFAULT_DATA, help="样例数据 JSON 路径")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        registry = build_registry(args.data)
        assert registry.projects, "样例数据缺少项目"
        print(f"基础检查通过：{len(registry.projects)} 个项目已装载")
        return
    registry = build_registry(args.data)
    print(f"{SERVICE_NAME} 已装载 {args.data}，监听端口 {args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(registry)).serve_forever()


if __name__ == "__main__":
    main()
