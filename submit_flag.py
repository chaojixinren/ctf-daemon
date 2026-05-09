#!/usr/bin/env python3
"""Quick flag submission helper. Usage: python3 submit_flag.py <challenge_id> <flag>"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gzctf_client import load_config, GZCTFClient
from solver import submit_and_record, load_state, save_state

if len(sys.argv) < 3:
    print("Usage: python3 submit_flag.py <challenge_id> <flag>")
    sys.exit(1)

challenge_id = int(sys.argv[1])
flag = sys.argv[2]

config = load_config(os.path.join(os.path.dirname(__file__), "config.env"))
client = GZCTFClient(
    config["GZCTF_BASE_URL"],
    config["GZCTF_USERNAME"],
    config["GZCTF_PASSWORD"],
)

if not client.login():
    print("ERROR: Login failed")
    sys.exit(1)

# Get game ID
game_id = int(config.get("GZCTF_GAME_ID", "0"))
if game_id == 0:
    games = client.list_games()
    if games:
        game_id = games[0]["id"]

client.get_game_detail(game_id)

state = load_state()
ok = submit_and_record(client, challenge_id, flag, state)
sys.exit(0 if ok else 1)
