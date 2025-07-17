import os
import subprocess
import json
import base64
import logging
import threading
import time
import requests
import random
from flask import Flask, jsonify, render_template_string, request

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
SERF_EXECUTABLE_PATH = "/usr/bin/serf"
SERF_RPC_ADDR = "127.0.0.1:7373"
COMETBFT_RPC_URL = "http://localhost:26657"

# --- Flask Application Setup ---
app = Flask(__name__)

# --- Global State for Monitoring ---
app_metrics = {
    "serf_monitor_status": "Starting...",
    "serf_rpc_status": "Unknown",
    "cometbft_rpc_status": "Unknown",
    "serf_events_received": 0,
    "cometbft_tx_broadcast": 0,
    "serf_members": [], # Will be populated by the health checker
    "cometbft_node_info": {}
}
metrics_lock = threading.Lock()
recent_activity_log = []
RECENT_ACTIVITY_MAX_ITEMS = 20
threads_started = False
threads_lock = threading.Lock()

# --- HTML Template Embedded as a String ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Serf & CometBFT Bridge</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <meta http-equiv="refresh" content="7">
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #f8f9fa; }
        .card { background-color: #ffffff; border-radius: 0.5rem; border: 1px solid #dee2e6; box-shadow: 0 0.125rem 0.25rem rgba(0,0,0,.075); }
        .header-text { color: #0056b3; }
        .status-dot { height: 0.75rem; width: 0.75rem; border-radius: 9999px; }
        .bg-green-500 { background-color: #28a745; }
        .bg-red-500 { background-color: #dc3545; }
        .bg-gray-400 { background-color: #6c757d; }
        .btn { display: inline-flex; align-items: center; padding: 0.5rem 1rem; border: 1px solid transparent; font-weight: bold; border-radius: 0.375rem; color: white; transition: background-color 0.2s; }
        .btn-primary { background-color: #007bff; }
        .btn-primary:hover { background-color: #0069d9; }
        .status-success { color: #155724; background-color: #d4edda; }
        .status-failure { color: #721c24; background-color: #f8d7da; }
    </style>
</head>
<body class="p-4">
    <div class="max-w-7xl mx-auto">
        <h1 class="text-4xl font-extrabold header-text mb-6 text-center">
            Serf &harr; CometBFT Bridge Dashboard
        </h1>

        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-8">
            <div class="card p-6">
                <h2 class="text-xl font-semibold header-text mb-4">Serf Monitoring</h2>
                <div class="flex items-center mb-2"><div class="status-dot {{ serf_status_color }} mr-2"></div><span class="font-medium">RPC Status:</span><span class="ml-2 font-bold">{{ metrics.serf_rpc_status }}</span></div>
                <p class="text-sm">Events Received: <span class="font-bold text-indigo-600">{{ metrics.serf_events_received }}</span></p>
                <p class="text-sm italic">Monitor Status: <span class="font-bold">{{ metrics.serf_monitor_status }}</span></p>
            </div>

            <div class="card p-6">
                <h2 class="text-xl font-semibold header-text mb-4">CometBFT Integration</h2>
                <div class="flex items-center mb-2"><div class="status-dot {{ comet_status_color }} mr-2"></div><span class="font-medium">RPC Status:</span><span class="ml-2 font-bold">{{ metrics.cometbft_rpc_status }}</span></div>
                <p class="text-sm">Committed TXs: <span class="font-bold text-indigo-600">{{ metrics.cometbft_tx_broadcast }}</span></p>
                <p class="text-sm italic">Node Name: <span class="font-bold text-blue-600">{{ metrics.cometbft_node_info.moniker | default('N/A') }}</span></p>
            </div>

            <div class="card p-6">
                <h2 class="text-xl font-semibold header-text mb-4">Serf Cluster ({{ metrics.serf_members|length }} Nodes)</h2>
                <div class="max-h-24 overflow-y-auto">
                    <ul class="text-xs space-y-1">
                    {% for member in metrics.serf_members %}
                        <li class="flex items-center p-1 bg-gray-100 rounded">
                            <div class="status-dot {% if member.status == 'alive' %}bg-green-500{% else %}bg-red-500{% endif %} mr-2"></div>
                            <span class="font-semibold">{{ member.name }}</span>
                            <span class="text-gray-600 ml-auto">{{ member.addr }}:{{ member.port }}</span>
                        </li>
                    {% else %}
                        <p class="text-sm text-gray-500 italic">No Serf members found.</p>
                    {% endfor %}
                    </ul>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
            <div class="card p-6 flex flex-col justify-center">
                 <h2 class="text-xl font-semibold header-text mb-4">Dispatch Transaction</h2>
                 <form id="tx-form">
                    <textarea name="custom_tx" class="w-full p-2 border rounded mb-3 font-mono text-sm" rows="3" placeholder="Enter custom TX data or leave blank for random."></textarea>
                    <button type="submit" class="btn btn-primary w-full">Broadcast via Serf</button>
                </form>
                <p id="tx-status" class="text-sm italic mt-3 text-center"></p>
            </div>
            <div class="card p-6">
                <h2 class="text-xl font-semibold header-text mb-4">Recent Activity Log</h2>
                <div class="space-y-3 max-h-80 overflow-y-auto">
                    {% for entry in activity_log %}
                    <div class="border rounded-md p-3 text-sm">
                        <div class="flex justify-between items-center mb-1">
                            <p class="font-medium"><span class="text-indigo-600 font-bold">{{ entry.name }}</span></p>
                            <span class="font-semibold px-2 py-1 rounded-md text-xs
                                {% if entry.status == 'success' %}status-success
                                {% elif entry.status == 'failure' %}status-failure
                                {% else %}status-pending{% endif %}">
                                {{ entry.cometbft_response }}
                            </span>
                        </div>
                    </div>
                    {% else %}
                    <p class="text-gray-600 italic text-center py-4">No recent activity.</p>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>

<script>
document.getElementById('tx-form').addEventListener('submit', function(e) {
    e.preventDefault();
    const statusEl = document.getElementById('tx-status');
    const formData = new FormData(e.target);
    statusEl.textContent = 'Dispatching...';
    fetch('/trigger_transaction', { method: 'POST', body: formData })
        .then(response => response.json())
        .then(data => {
            statusEl.textContent = data.message || 'Done';
            if (data.status === 'success') { e.target.reset(); }
        })
        .catch(err => { statusEl.textContent = 'Request failed.'; });
});
</script>
</body>
</html>
"""

# --- Data Structures and Core Logic (largely unchanged) ---

class CometBFTTxResponse:
    def __init__(self, check_tx_code=None, deliver_tx_code=None, log="", hash="", height=0):
        self.check_tx_code, self.deliver_tx_code, self.log, self.hash, self.height = check_tx_code, deliver_tx_code, log, hash, height
    @property
    def is_successful(self): return self.check_tx_code == 0 and self.deliver_tx_code == 0

class CometBFTClient:
    def __init__(self, rpc_url: str): self.rpc_url = rpc_url
    def broadcast_and_commit_tx(self, tx_bytes: bytes, cb: callable):
        tx_b64 = base64.b64encode(tx_bytes).decode('utf-8')
        payload = {"jsonrpc": "2.0", "method": "broadcast_tx_commit", "params": {"tx": tx_b64}, "id": 1}
        try:
            r = requests.post(self.rpc_url, headers={'Content-Type': 'application/json'}, json=payload, timeout=10)
            r.raise_for_status()
            res = r.json().get("result", {})
            cb(CometBFTTxResponse(
                check_tx_code=res.get("check_tx", {}).get("code", -1),
                deliver_tx_code=res.get("deliver_tx", {}).get("code", -1),
                log=res.get("deliver_tx", {}).get("log") or res.get("check_tx", {}).get("log", "N/A"),
                hash=res.get("hash", ""), height=res.get("height", 0)
            ))
        except Exception as e: cb(CometBFTTxResponse(log=str(e)))

# --- Background Threads ---

def serf_monitor_thread():
    while True:
        try:
            logger.info("Launching 'serf monitor'...")
            cmd_args = [SERF_EXECUTABLE_PATH, "monitor", f"-rpc-addr={SERF_RPC_ADDR}", "-log-level=info"]
            process = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, text=True, bufsize=1)
            with metrics_lock: app_metrics["serf_monitor_status"] = "Running"
            for line in iter(process.stdout.readline, ''):
                if line.strip(): handle_serf_event_line(line)
        except Exception as e: logger.critical(f"Serf monitor failed: {e}")
        with metrics_lock: app_metrics["serf_monitor_status"] = "Error"
        time.sleep(5)

def health_check_thread():
    while True:
        # Check Serf Members
        try:
            cmd = [SERF_EXECUTABLE_PATH, "members", "-format=json", f"-rpc-addr={SERF_RPC_ADDR}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            with metrics_lock:
                app_metrics["serf_members"] = json.loads(result.stdout).get("members", []) if result.returncode == 0 else []
                app_metrics["serf_rpc_status"] = "Connected" if result.returncode == 0 else "Disconnected"
        except Exception:
            with metrics_lock: app_metrics["serf_rpc_status"] = "Error"
        
        # Check CometBFT Status
        try:
            r = requests.get(f"{COMETBFT_RPC_URL}/status", timeout=3)
            r.raise_for_status()
            with metrics_lock:
                app_metrics["cometbft_node_info"] = r.json().get("result", {}).get("node_info", {})
                app_metrics["cometbft_rpc_status"] = "Connected"
        except Exception:
            with metrics_lock: app_metrics["cometbft_rpc_status"] = "Disconnected"
        time.sleep(7)

def handle_serf_event_line(line: str):
    try:
        event_data = json.loads(line)
        if event_data.get("Event") == "user":
            process_serf_user_event(event_data)
    except json.JSONDecodeError: pass

def process_serf_user_event(event_data: dict):
    cometbft_client = CometBFTClient(COMETBFT_RPC_URL)
    payload_b64 = event_data.get("Payload")
    if not payload_b64: return
    
    with metrics_lock: app_metrics["serf_events_received"] += 1
    new_activity = {"name": event_data.get("Name", "unknown"), "cometbft_response": "Pending...", "status": "pending"}
    recent_activity_log.insert(0, new_activity)

    def cb(response: CometBFTTxResponse, entry=new_activity):
        with metrics_lock:
            app_metrics["cometbft_tx_broadcast"] += 1
            if response.is_successful:
                entry["cometbft_response"] = f"Success (H:{response.height})"
                entry["status"] = "success"
            else:
                entry["cometbft_response"] = f"Failed (C:{response.check_tx_code},D:{response.deliver_tx_code})"
                entry["status"] = "failure"
    try:
        cometbft_client.broadcast_and_commit_tx(base64.b64decode(payload_b64), cb)
    except Exception as e:
        new_activity["cometbft_response"], new_activity["status"] = f"Broadcast Error: {e}", "failure"

# --- Flask Routes ---

@app.before_request
def start_background_threads():
    global threads_started
    with threads_lock:
        if not threads_started:
            threading.Thread(target=serf_monitor_thread, daemon=True).start()
            threading.Thread(target=health_check_thread, daemon=True).start()
            threads_started = True
            logger.info("Started background threads.")

@app.route('/trigger_transaction', methods=['POST'])
def trigger_transaction():
    tx_data = request.form.get('custom_tx')
    payload = tx_data.encode('utf-8') if tx_data else f"random_key=value_{random.randint(1000,9999)}".encode('utf-8')
    event_name = "custom-tx" if tx_data else "random-tx"
    try:
        cmd = [SERF_EXECUTABLE_PATH, "event", f"-rpc-addr={SERF_RPC_ADDR}", event_name, base64.b64encode(payload).decode('utf-8')]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return jsonify({"status": "success", "message": "Event dispatched successfully."})
        return jsonify({"status": "error", "message": f"Serf Error: {result.stderr.strip()}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server Error: {e}"}), 500

@app.route('/')
def index():
    with metrics_lock:
        # Pass copies to the template to avoid modification during render
        metrics = app_metrics.copy()
        activity = recent_activity_log[:]

    serf_status_color = "bg-gray-400"
    if metrics["serf_rpc_status"] == "Connected": serf_status_color = "bg-green-500"
    elif metrics["serf_rpc_status"] != "Unknown": serf_status_color = "bg-red-500"

    comet_status_color = "bg-gray-400"
    if metrics["cometbft_rpc_status"] == "Connected": comet_status_color = "bg-green-500"
    elif metrics["cometbft_rpc_status"] != "Unknown": comet_status_color = "bg-red-500"
    
    return render_template_string(HTML_TEMPLATE, metrics=metrics, activity_log=activity, serf_status_color=serf_status_color, comet_status_color=comet_status_color)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False)
