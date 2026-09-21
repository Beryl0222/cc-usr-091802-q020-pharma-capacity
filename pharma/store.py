"""事件存储：只追加日志，支持 JSON 持久化与双时态过滤。"""

import json
import os
import tempfile
from typing import Iterable, List, Optional

from .events import Event


class EventStore:
    """内存事件日志，可镜像到磁盘上的 JSONL 文件。

    事件一经写入不可变；并购、迁移、延期、口径修订全部通过追加新事件
    形成新版本，绝不修改历史事件——这是年报复现的前提。
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._events: List[Event] = []
        if path and os.path.exists(path):
            self._load()

    def append(self, event: Event) -> Event:
        self._events.append(event)
        if self.path:
            self._flush()
        return event

    def all(self) -> List[Event]:
        return list(self._events)

    def query(
        self,
        type_in: Optional[Iterable[str]] = None,
        occurred_on_or_before: Optional[str] = None,
        recorded_on_or_before: Optional[str] = None,
        include_confidential: bool = True,
    ) -> List[Event]:
        """双时态过滤。

        occurred_on_or_before: 业务生效日 <= 该日（年报口径的 as_of）。
        recorded_on_or_before: 系统记录时间 <= 该时刻（复现已发布报告：报告
                               发布之后补录的证据不计入）。
        """
        types = set(type_in) if type_in else None
        out = []
        for e in self._events:
            if types is not None and e.type not in types:
                continue
            if occurred_on_or_before and e.occurred_at > occurred_on_or_before:
                continue
            if recorded_on_or_before and e.recorded_at > recorded_on_or_before:
                continue
            if e.confidential and not include_confidential:
                continue
            out.append(e)
        return out

    # ---- 持久化 -------------------------------------------------------------

    def _load(self):
        from .events import seed_seq
        max_id = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    event = Event.from_dict(json.loads(line))
                    self._events.append(event)
                    max_id = max(max_id, event.id)
        seed_seq(max_id)

    def _flush(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".events-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for e in self._events:
                    fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
