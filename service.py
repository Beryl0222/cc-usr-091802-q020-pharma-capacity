"""医药园区能力兑现项目依赖服务 —— HTTP 入口。

端点
----
GET  /health                              服务身份
POST /commands/<command>                  执行登记/闸门/处置等命令（JSON body）
GET  /projects/<id>?as_of=&user=          项目下钻（阶段、产能依据、待核事项）
GET  /projects/<id>/gate?as_of=&user=     阶段闸门核对
GET  /lines/<id>/capacity?year=&as_of=    共享产线占用与剩余能力
GET  /summary?parks=a,b&year=&as_of=&user= 区域去重合计与全部下钻行
GET  /impact?kind=&id=&as_of=&until=      供应商中断/审批延迟影响分析
POST /dispositions                        负责人确认处置（也可走 commands）
GET  /reports/<id>/reproduce?user=        按发布当时证据复现年度报告
GET  /events                              事件日志（排错用；?user= 控制商业字段）

启动：
  python3 service.py --port 8000 --demo            # 内存 + 合成场景
  python3 service.py --port 8000 --store data.jsonl
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from pharma import SERVICE_ID
from pharma.app import Application, COMMANDS, DomainError
from pharma.scenario import build_demo
from pharma.store import EventStore

SERVICE_NAME = "医药园区能力兑现"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    app: Application = None

    # ---- 工具 ---------------------------------------------------------------

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self, name, default=None, required=True):
        qs = parse_qs(urlparse(self.path).query)
        if name not in qs:
            if required:
                raise DomainError("bad-request", f"缺少查询参数：{name}")
            return default
        return qs[name][0]

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError("bad-json", f"请求体不是合法 JSON：{exc}")
        return data if isinstance(data, dict) else {}

    # ---- 路由 ---------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._send(200, health_payload())
            elif path.startswith("/projects/") and path.endswith("/gate"):
                pid = path.split("/")[2]
                self._send(200, self.app.project_gate(
                    pid, self._qs("as_of"), self._qs("user", required=False)))
            elif path.startswith("/projects/"):
                pid = path.split("/")[2]
                self._send(200, self.app.project_detail(
                    pid, self._qs("as_of"), self._qs("user", required=False)))
            elif path.startswith("/lines/") and path.endswith("/capacity"):
                lid = path.split("/")[2]
                self._send(200, self.app.line_capacity(
                    lid, int(self._qs("year")), self._qs("as_of"),
                    self._qs("user", required=False)))
            elif path == "/summary":
                parks = [p for p in self._qs("parks").split(",") if p]
                self._send(200, self.app.summary(
                    parks, int(self._qs("year")), self._qs("as_of"),
                    self._qs("user", required=False)))
            elif path == "/impact":
                self._send(200, self.app.impact(
                    self._qs("kind"), self._qs("id"), self._qs("as_of"),
                    self._qs("user", required=False),
                    self._qs("until", required=False)))
            elif path.startswith("/reports/") and path.endswith("/reproduce"):
                rid = path.split("/")[2]
                self._send(200, self.app.reproduce_report(
                    rid, self._qs("user", required=False)))
            elif path == "/events":
                user = self._qs("user", required=False)
                # 商业敏感事件仅对真正持商业授权的用户可见，未知用户一律不披露
                include = self.app._can_see_commercial(user, "9999-12-31") \
                    if user else False
                self._send(200, {"events": [
                    e.to_dict() for e in self.app.store.query(
                        include_confidential=include)]})
            else:
                self._send(404, {"error": "not-found", "path": path})
        except DomainError as exc:
            self._send(400, exc.to_dict())

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self._body()
            if path.startswith("/commands/"):
                command = path.split("/")[-1]
                fn = COMMANDS.get(command)
                if fn is None:
                    raise DomainError("unknown-command", f"未知命令：{command}")
                user_id = body.pop("user_id", None)
                if not user_id:
                    raise DomainError("user-required", "命令必须携带 user_id")
                self._send(200, {"ok": True,
                                 "result": fn(self.app, user_id, **body)})
            elif path == "/dispositions":
                user_id = body.pop("user_id", None)
                if not user_id:
                    raise DomainError("user-required", "处置必须携带 user_id")
                result = self.app.handle_disruption(user_id=user_id, **body)
                self._send(200, {"ok": True, "result": result})
            else:
                self._send(404, {"error": "not-found", "path": path})
        except TypeError as exc:
            self._send(400, {"error": "bad-arguments", "message": str(exc)})
        except DomainError as exc:
            self._send(400, exc.to_dict())

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--store", default=None, help="事件 JSONL 持久化路径")
    parser.add_argument("--demo", action="store_true", help="启动时装载合成场景")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return

    store = EventStore(args.store)
    app = Application(store)
    if args.demo:
        build_demo(store)
    Handler.app = app
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
