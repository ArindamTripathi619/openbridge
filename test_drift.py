"""Deterministic Drift Test."""

import os
import time
import logging
from pathlib import Path
import subprocess

LOG_FILE = "drift_test.log"

def write_log(line):
    print(f"📝 {line}")
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def run_test():
    # 1. Start fresh
    if os.path.exists(LOG_FILE): os.remove(LOG_FILE)
    Path(LOG_FILE).touch()
    if os.path.exists("/home/DevCrewX/.telewatch/state.json"): os.remove("/home/DevCrewX/.telewatch/state.json")

    config_path = Path("/home/DevCrewX/.telewatch/drift_config.yaml")
    config_content = """
telegram:
  bot_token: "DUMMY"
  chat_id: "DUMMY"
llm:
  provider: "groq"
  model: "llama-3.3-70b-versatile"
  api_key: "DUMMY"
  optimization:
    profiler_limit: 10
monitors:
  - type: "file"
    name: "Drift Monitor"
    path: "drift_test.log"
    keywords: ["ERROR"]
"""
    config_path.write_text(config_content)
    
    print("🚀 Starting Bot...")
    bot_proc = subprocess.Popen(
        ["./venv/bin/python3", "-m", "telewatch.main", "start", "--config", str(config_path)],
        env={**os.environ, "PYTHONPATH": "src"}
    )
    
    time.sleep(3)
    
    # 2. Phase 1: Structured Logs (15 lines)
    print("--- PHASE 1: Structured (Syslog) ---")
    for i in range(15):
        write_log(f"Feb 14 15:20:{i:02d} node-01 proc[123]: INFO: Item {i} processed")
        time.sleep(0.2)
        
    print("Wait for profiling to complete...")
    time.sleep(2)
    
    # 3. Phase 2: Error in Phase 1 format
    print("--- PHASE 2: Error ---")
    write_log("Feb 14 15:20:16 node-01 proc[123]: ERROR: Disk nearly full")
    time.sleep(2)
    
    # 4. Phase 3: Sudden Format Change (Drift)
    print("--- PHASE 3: Drift (JSON) ---")
    for i in range(15):
        write_log('{"ts": 1234567, "level": "INFO", "msg": "Json line %d"}' % i)
        time.sleep(0.2)
        
    print("Wait for drift detection...")
    time.sleep(5)
    
    bot_proc.terminate()
    print("✅ Test Finished.")

if __name__ == "__main__":
    run_test()
