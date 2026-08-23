#!/usr/bin/env python3
import logging
import os

from src.broker_operations import BrokerOperations
from src.broker_server import BrokerApplication, build_broker_server
from src.marzban import MarzbanClient


def main():
    logging.basicConfig(level=os.getenv("BROKER_LOG_LEVEL", "INFO"))
    host = os.getenv("MARZBAN_BROKER_LISTEN_HOST", "127.0.0.1")
    port = int(os.getenv("MARZBAN_BROKER_LISTEN_PORT", "8002"))
    if not 1 <= port <= 65535:
        raise ValueError("production broker port must be between 1 and 65535")
    shared_key = os.getenv("MARZBAN_BROKER_AUTH_KEY", "")
    client_id = os.getenv("MARZBAN_BROKER_CLIENT_ID", "mgboost-main")
    skew = int(os.getenv("MARZBAN_BROKER_ALLOWED_SKEW_SECONDS", "30"))
    workers = int(os.getenv("MARZBAN_BROKER_MAX_WORKERS", "16"))
    app = BrokerApplication(
        BrokerOperations(MarzbanClient()),
        shared_key=shared_key,
        client_id=client_id,
        allowed_skew_seconds=skew,
    )
    server = build_broker_server(host, port, app, max_workers=workers)
    logging.getLogger(__name__).info("Marzban broker listening on %s:%d", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
