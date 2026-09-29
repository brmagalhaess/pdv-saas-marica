import io
import json
import tempfile
import unittest
from pathlib import Path

from local_pdv import LocalPDV, PDVError
from local_pdv.sync import sync_once


class FakeResponse:
    def __init__(self, accepted):
        self.accepted = accepted
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps({"accepted_event_ids": self.accepted}).encode()


class SyncTests(unittest.TestCase):
    def test_ack_only_after_matching_server_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            pdv = LocalPDV(Path(directory) / "caixa.sqlite3", "t", "s", "x")
            pdv.open_cash(0)
            event_id = pdv.pending_events()[0]["event_id"]
            def success(request, timeout):
                self.assertEqual(request.get_header("Authorization"), "Bearer " + "a" * 40)
                return FakeResponse([event_id])
            self.assertEqual(sync_once(pdv, "http://127.0.0.1:8000", "a" * 40, success)["sent"], 1)
            self.assertEqual(pdv.status()["pending_events"], 0)

    def test_server_failure_keeps_event_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            pdv = LocalPDV(Path(directory) / "caixa.sqlite3", "t", "s", "x")
            pdv.open_cash(0)
            with self.assertRaises(PDVError):
                sync_once(pdv, "http://127.0.0.1:8000", "a" * 40,
                          lambda request, timeout: FakeResponse([]))
            self.assertEqual(pdv.status()["pending_events"], 1)
            with self.assertRaises(PDVError):
                sync_once(pdv, "http://example.com", "a" * 40)


if __name__ == "__main__":
    unittest.main()
