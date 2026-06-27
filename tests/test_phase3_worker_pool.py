from __future__ import annotations

import queue
import threading
import time


def _start_workers(wr, count: int = 2) -> None:
    for i in range(count):
        t = threading.Thread(target=wr.worker_loop, args=(f"phase3-test-worker-{i+1}",), daemon=True)
        t.start()


def _enqueue(wr, q: queue.Queue, payload: dict, intake_received_at: str) -> None:
    q.put(wr._queue_work_item(payload, intake_received_at))


def test_same_symbol_is_processed_in_order(wr, monkeypatch):
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(wr, "PROCESS_QUEUE", q)

    events = []
    events_lock = threading.Lock()

    def fake_process(payload, intake_received_at):
        with events_lock:
            events.append(("start", payload["id"]))
        time.sleep(0.06)
        with events_lock:
            events.append(("end", payload["id"]))

    monkeypatch.setattr(wr, "process_payload_async", fake_process)
    _start_workers(wr, 2)
    time.sleep(0.02)

    _enqueue(wr, q, {"symbol": "XRPUSDT", "id": 1}, "2026-06-25T00:00:01Z")
    _enqueue(wr, q, {"symbol": "XRPUSDT", "id": 2}, "2026-06-25T00:00:02Z")
    q.join()

    idx_start_1 = events.index(("start", 1))
    idx_end_1 = events.index(("end", 1))
    idx_start_2 = events.index(("start", 2))
    idx_end_2 = events.index(("end", 2))

    assert idx_start_1 < idx_end_1
    assert idx_end_1 < idx_start_2
    assert idx_start_2 < idx_end_2


def test_different_symbols_can_run_concurrently(wr, monkeypatch):
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(wr, "PROCESS_QUEUE", q)

    counters = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    def fake_process(payload, intake_received_at):
        with lock:
            counters["active"] += 1
            counters["max_active"] = max(counters["max_active"], counters["active"])
        time.sleep(0.08)
        with lock:
            counters["active"] -= 1

    monkeypatch.setattr(wr, "process_payload_async", fake_process)
    _start_workers(wr, 2)
    time.sleep(0.02)

    _enqueue(wr, q, {"symbol": "BTCUSDT", "id": 1}, "2026-06-25T00:01:01Z")
    _enqueue(wr, q, {"symbol": "ETHUSDT", "id": 2}, "2026-06-25T00:01:02Z")
    q.join()

    assert counters["max_active"] >= 2


def test_four_symbol_burst_not_blocked_by_same_symbol_backlog(wr, monkeypatch):
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(wr, "PROCESS_QUEUE", q)

    start = time.monotonic()
    finished: dict[str, list[float]] = {}
    lock = threading.Lock()

    def fake_process(payload, intake_received_at):
        symbol = payload["symbol"]
        if symbol == "SOLUSDT":
            time.sleep(0.12)
        else:
            time.sleep(0.03)
        with lock:
            finished.setdefault(symbol, []).append(time.monotonic() - start)

    monkeypatch.setattr(wr, "process_payload_async", fake_process)
    _start_workers(wr, 4)
    time.sleep(0.02)

    for i in range(4):
        _enqueue(wr, q, {"symbol": "SOLUSDT", "id": i}, f"2026-06-25T00:02:0{i}Z")
    _enqueue(wr, q, {"symbol": "BTCUSDT", "id": 10}, "2026-06-25T00:02:10Z")
    _enqueue(wr, q, {"symbol": "ETHUSDT", "id": 11}, "2026-06-25T00:02:11Z")
    _enqueue(wr, q, {"symbol": "XRPUSDT", "id": 12}, "2026-06-25T00:02:12Z")
    q.join()

    fast_elapsed = [finished["BTCUSDT"][0], finished["ETHUSDT"][0], finished["XRPUSDT"][0]]
    slow_elapsed = finished["SOLUSDT"]
    assert max(fast_elapsed) < max(slow_elapsed)


def test_same_symbol_ordering_fuzz_under_interleaved_load(wr, monkeypatch):
    q: queue.Queue = queue.Queue()
    monkeypatch.setattr(wr, "PROCESS_QUEUE", q)

    processed: list[tuple[str, int, str]] = []
    lock = threading.Lock()

    def fake_process(payload, intake_received_at):
        time.sleep(0.004)
        with lock:
            processed.append((payload["symbol"], payload["id"], payload["action"]))

    monkeypatch.setattr(wr, "process_payload_async", fake_process)
    _start_workers(wr, 6)
    time.sleep(0.02)

    for i in range(30):
        action = "OPEN" if (i % 2) == 0 else "CLOSE"
        _enqueue(wr, q, {"symbol": "XRPUSDT", "id": i, "action": action}, f"2026-06-25T00:03:{i:02d}Z")
        _enqueue(wr, q, {"symbol": "BTCUSDT", "id": i, "action": "NOISE"}, f"2026-06-25T00:04:{i:02d}Z")
        _enqueue(wr, q, {"symbol": "ETHUSDT", "id": i, "action": "NOISE"}, f"2026-06-25T00:05:{i:02d}Z")

    q.join()

    xrp_ids = [pid for sym, pid, _ in processed if sym == "XRPUSDT"]
    assert xrp_ids == list(range(30))
