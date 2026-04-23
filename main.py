#!/usr/bin/env python3
from src.config import LISTEN_HOST, LISTEN_PORT
from src.database import Database
from src.server import Server

if __name__ == "__main__":
    db = Database()
    db.migrate_from_json()
    server = Server(db)
    server.run(LISTEN_HOST, LISTEN_PORT)
