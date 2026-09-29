"""Explicit one-shot outbox sender for development; no background service yet."""
import argparse
import json
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .core import LocalPDV, PDVError


def sync_once(pdv, base_url, token, transport=urlopen):
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
        raise PDVError("Sincronização remota exige HTTPS")
    if not token or len(token) < 32:
        raise PDVError("Token de terminal ausente ou inválido")
    events = pdv.pending_events(limit=50)
    if not events:
        return {"sent": 0, "remaining": 0}
    request = Request(base_url.rstrip("/") + "/api/sync/events",
                      data=json.dumps({"events": events}, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                      method="POST")
    with transport(request, timeout=15) as response:
        result = json.loads(response.read())
    accepted = result.get("accepted_event_ids")
    sent_ids = [event["event_id"] for event in events]
    if not isinstance(accepted, list) or accepted != sent_ids:
        raise PDVError("Servidor não confirmou todos os eventos na ordem enviada")
    for event_id in accepted:
        pdv.acknowledge_event(event_id)
    return {"sent": len(accepted), "remaining": pdv.status()["pending_events"]}


def main():
    parser = argparse.ArgumentParser(description="Envia eventos pendentes do caixa para a prévia SaaS")
    parser.add_argument("--db", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.environ.get("PDV_TERMINAL_TOKEN", "")
    pdv = LocalPDV(args.db)
    print(json.dumps(sync_once(pdv, args.url, token), ensure_ascii=False))


if __name__ == "__main__":
    main()
