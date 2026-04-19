"""Verify Phase 15 - Chaos Engineering."""

import os
import time
import subprocess
from pathlib import Path

def run_test():
    # 1. Prepare config for chaos.log
    config_path = Path("/home/DevCrewX/.telewatch/chaos_config.yaml")
    config_content = """
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  chat_id: "${TELEGRAM_CHAT_ID}"
llm:
  provider: "groq"
  model: "llama-3.3-70b-versatile"
  api_key: "${LLM_API_KEY}"
  optimization:
    enable_cache: true
    use_local_patterns: true
    profiler_limit: 20  # Fast profiling for test
notification:
  rate_limit_per_hour: 50
monitors:
  - type: "file"
    name: "Chaos Monitor"
    path: "chaos.log"
    keywords: ["CRITICAL", "FATAL", "PANIC", "ERROR"]
"""
    # Expand env vars
    for var in ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "LLM_API_KEY"]:
        config_content = config_content.replace(f"${{{var}}}", os.environ.get(var, ""))
    
    config_path.write_text(config_content)
    
    # 2. Touch log file
    Path("chaos.log").write_text("")
    
    print("🚀 Launching TeleWatch on chaos.log...")
    bot_proc = subprocess.Popen(
        ["./venv/bin/python3", "-m", "telewatch.main", "start", "--config", str(config_path)],
        env={**os.environ, "PYTHONPATH": "src"}
    )
    
    time.sleep(2)
    
    print("🐒 Unleashing Chaos Monkey...")
    try:
        # Run chaos monkey for a bit
        subprocess.run(["./venv/bin/python3", "chaos_sim.py"], timeout=120)
    except subprocess.TimeoutExpired:
        print("✅ Chaos Gauntlet session complete.")
    finally:
        bot_proc.terminate()
        print("🛑 Bot stopped.")

if __name__ == "__main__":
    run_test()
