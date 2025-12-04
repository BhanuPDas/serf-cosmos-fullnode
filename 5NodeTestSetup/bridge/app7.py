import os
import subprocess
import json
import base64
import logging
import threading
import time
import requests
import random
from flask import Flask, jsonify, render_template, request

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
SERF_EXECUTABLE_PATH = "/usr/bin/serf"  # This path is now correct for your system
SERF_RPC_ADDR = "127.0.0.1:7373"
COMETBFT_RPC_URL = "http://localhost:26657"

# --- Flask Application Setup ---
app = Flask(__name__)

# --- Global State for Monitoring ---
app_metrics = {
    "serf_monitor_status": "Starting...",
    "serf_monitor_last_error": None,
    "serf_rpc_status": "Unknown",
    "cometbft_rpc_status": "Unknown",
    "serf_events_received": 0,
    "cometbft_tx_broadcast": 0,
    "serf_members": [],
    "cometbft_node_info": {}
}
metrics_lock = threading.Lock()
recent_activity_log = []
RECENT_ACTIVITY_MAX_ITEMS = 15
threads_started = False
threads_lock = threading.Lock()

class CometBFTTxResponse:
    """A data class to hold the results from a broadcast_tx_commit call."""
    def __init__(self, check_tx_code=None, deliver_tx_code=None, log="", hash="", height=0):
        self.check_tx_code = check_tx_code
        self.deliver_tx_code = deliver_tx_code
        self.log = log
        self.hash = hash
        self.height = height

    def to_dict(self):
        return {"CheckTxCode": self.check_tx_code, "DeliverTxCode": self.deliver_tx_code, "Log": self.log, "Hash": self.hash, "Height": self.height}

    @property
    def is_successful(self):
        return self.check_tx_code == 0 and self.deliver_tx_code == 0

class CometBFTClient:
    """A client for interacting with the CometBFT RPC."""
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        logger.info(f"CometBFTClient initialized with RPC URL: {self.rpc_url}")

    def broadcast_and_commit_tx(self, tx_bytes: bytes, cb: callable):
        tx_b64 = base64.b64encode(tx_bytes).decode('utf-8')
        endpoint = self.rpc_url
        headers = {'Content-Type': 'application/json'}
        payload = {"jsonrpc": "2.0", "method": "broadcast_tx_commit", "params": {"tx": tx_b64}, "id": 1}

        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            rpc_result = response.json()

            if "result" in rpc_result:
                tx_result = rpc_result["result"]
                comet_response = CometBFTTxResponse(
                    check_tx_code=tx_result.get("check_tx", {}).get("code", -1),
                    deliver_tx_code=tx_result.get("deliver_tx", {}).get("code", -1),
                    log=tx_result.get("deliver_tx", {}).get("log") or tx_result.get("check_tx", {}).get("log", "No log message"),
                    hash=tx_result.get("hash", ""),
                    height=tx_result.get("height", 0)
                )
                with metrics_lock:
                    app_metrics["cometbft_tx_broadcast"] += 1
                cb(comet_response)
            else:
                # ... (error handling as before) ...
                cb(CometBFTTxResponse(log="Unexpected RPC response format"))
        except Exception as e:
            # ... (error handling as before) ...
            cb(CometBFTTxResponse(log=f"RPC Error: {e}"))

# --- Background Threads ---

def serf_monitor_thread():
    """Listens for Serf events. This thread BLOCKS while listening."""
    while True:
        try:
            logger.info("Attempting to launch 'serf monitor'...")
            cmd_args = [SERF_EXECUTABLE_PATH, "monitor", f"-rpc-addr={SERF_RPC_ADDR}", "-log-level=info"]
            process = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            
            with metrics_lock:
                 app_metrics["serf_monitor_status"] = "Running"
                 app_metrics["serf_rpc_status"] = "Connected"

            for line in iter(process.stdout.readline, ''):
                handle_serf_event_line(line)

            stderr_output = process.stderr.read()
            logger.error(f"Serf monitor process exited. Stderr: {stderr_output}")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = f"Exited with code {process.returncode}"
                app_metrics["serf_rpc_status"] = "Disconnected"

        except Exception as e:
            logger.critical(f"Failed to run Serf monitor: {e}", exc_info=True)
            with metrics_lock:
                app_metrics["serf_monitor_status"] = f"Error: {e}"
                app_metrics["serf_rpc_status"] = "Error"
        time.sleep(5) # Wait before retrying

def health_check_thread():
    """NEW: Periodically checks the status of Serf members and CometBFT."""
    cometbft_client = CometBFTClient(COMETBFT_RPC_URL)
    while True:
        # Check Serf Members
        try:
            members_cmd = [SERF_EXECUTABLE_PATH, "members", "-format=json", f"-rpc-addr={SERF_RPC_ADDR}"]
            result = subprocess.run(members_cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                members_data = json.loads(result.stdout)
                with metrics_lock:
                    app_metrics["serf_members"] = members_data.get("members", [])
                    app_metrics["serf_rpc_status"] = "Connected"
            else:
                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Disconnected"
        except Exception as e:
            logger.warning(f"Health check failed to get Serf members: {e}")
            with metrics_lock:
                app_metrics["serf_rpc_status"] = "Error"
        
        # Check CometBFT Status
        try:
            status_response = requests.get(f"{cometbft_client.rpc_url}/status", timeout=3)
            status_response.raise_for_status()
            status_data = status_response.json()
            with metrics_lock:
                app_metrics["cometbft_node_info"] = status_data.get("result", {}).get("node_info", {})
                app_metrics["cometbft_rpc_status"] = "Connected"
        except Exception as e:
            logger.warning(f"Health check failed to get CometBFT status: {e}")
            with metrics_lock:
                app_metrics["cometbft_rpc_status"] = "Disconnected"

        time.sleep(10) # Wait for 10 seconds before the next check


def handle_serf_event_line(line: str):
    """Processes a single line of output from the 'serf monitor' command."""
    line = line.strip()
    if not line: return

    logger.debug(f"Raw Serf Line: {line}")
    try:
        event_data = json.loads(line)
        if event_data.get("Event") == "user":
            process_serf_user_event(event_data)
    except json.JSONDecodeError:
        logger.debug(f"Ignoring non-JSON Serf output: {line}")


def process_serf_user_event(event_data: dict):
    """Handles the logic for a parsed Serf user event."""
    cometbft_client = CometBFTClient(COMETBFT_RPC_URL)
    payload_b64 = event_data.get("Payload")
    if not payload_b64: return
    
    with metrics_lock:
        app_metrics["serf_events_received"] += 1

    new_activity = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "name": event_data.get("Name", "unknown-event"),
        "payload_full": payload_b64,
        "cometbft_response": "Pending...",
        "status": "pending"
    }
    with metrics_lock:
        recent_activity_log.insert(0, new_activity)
        if len(recent_activity_log) > RECENT_ACTIVITY_MAX_ITEMS:
            recent_activity_log.pop()

    def commit_response_callback(response: CometBFTTxResponse, activity_entry=new_activity):
        with metrics_lock:
            if response.is_successful:
                activity_entry["cometbft_response"] = f"Success (H: {response.height}) Hash: {response.hash[:10]}..."
                activity_entry["status"] = "success"
            else:
                log_message = response.log.replace('"',"'").replace("\n", " ")
                activity_entry["cometbft_response"] = f"Failed (C:{response.check_tx_code},D:{response.deliver_tx_code}) Log: {log_message[:50]}..."
                activity_entry["status"] = "failure"

    try:
        decoded_tx_bytes = base64.b64decode(payload_b64)
        cometbft_client.broadcast_and_commit_tx(decoded_tx_bytes, commit_response_callback)
    except Exception as e:
        logger.error(f"Failed to decode or broadcast payload: {e}")
        with metrics_lock:
            new_activity["cometbft_response"] = f"Decode/Broadcast Error: {e}"
            new_activity["status"] = "failure"

# --- Flask Routes ---

@app.before_request
def start_background_threads():
    """MODIFIED: Starts both the monitor and health check threads."""
    global threads_started
    with threads_lock:
        if not threads_started:
            monitor = threading.Thread(target=serf_monitor_thread, name="SerfMonitorThread", daemon=True)
            health_checker = threading.Thread(target=health_check_thread, name="HealthCheckThread", daemon=True)
            
            monitor.start()
            health_checker.start()
            
            threads_started = True
            logger.info("Started Serf monitor and Health check threads.")

# ... (The rest of the Flask routes: /trigger_random_transaction, /, /status remain the same as the previous version) ...

@app.route('/trigger_random_transaction', methods=['POST'])
def trigger_random_transaction():
    tx_key = f"tx_key_{random.randint(1000, 9999)}".encode('utf-8')
    tx_value = f"value_{time.time()}".encode('utf-8')
    transaction_payload = b'='.join([tx_key, tx_value])
    transaction_payload_b64 = base64.b64encode(transaction_payload).decode('utf-8')
    event_name = f"kv-tx-{random.randint(1, 100)}"
    try:
        cmd_args = [SERF_EXECUTABLE_PATH, "event", f"-rpc-addr={SERF_RPC_ADDR}", event_name, transaction_payload_b64]
        result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return jsonify({"status": "success", "message": f"Dispatched Serf event '{event_name}'."})
        else:
            return jsonify({"status": "error", "message": f"Failed to dispatch Serf event: {result.stderr.strip()}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal server error: {e}"}), 500

@app.route('/')
def index():
    with metrics_lock:
        current_metrics = app_metrics.copy()
        current_activity_log = recent_activity_log[:]
    
    serf_status_color = "bg-gray-400"
    if current_metrics["serf_rpc_status"] == "Connected": serf_status_color = "bg-green-500"
    elif "Error" in current_metrics["serf_rpc_status"] or "Disconnected" in current_metrics["serf_rpc_status"]: serf_status_color = "bg-red-500"

    comet_status_color = "bg-gray-400"
    if current_metrics["cometbft_rpc_status"] == "Connected": comet_status_color = "bg-green-500"
    elif "Error" in current_metrics["cometbft_rpc_status"] or "Disconnected" in current_metrics["cometbft_rpc_status"]: comet_status_color = "bg-red-500"

    return render_template("index.html", metrics=current_metrics, activity_log=current_activity_log, serf_status_color=serf_status_color, comet_status_color=comet_status_color)

@app.route('/status')
def status():
    with metrics_lock:
        return jsonify({"status": "running", "metrics": app_metrics.copy(), "recent_activity_log": recent_activity_log[:]})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False)
