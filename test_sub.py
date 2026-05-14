import sys
sys.path.append('.')
import urllib.parse
from src.subscription import apply_inbound_client_extras

class MockDB:
    def __init__(self, setting):
        self.setting = setting
    def get_setting(self, key):
        if key == "inbound_client_extras":
            return self.setting
        return None

db = MockDB('{"vless-xhttp-cdn":"extra=%7B%22foo%22%3A%22bar%22%7D"}')
lines = [
    "vless://uuid@host:443?type=xhttp#Test_Node%20-%20vless-xhttp-cdn",
    "vless://uuid@host:443?type=xhttp#SomeOtherNode"
]

out = apply_inbound_client_extras(lines, db)
for line in out:
    print(line)
