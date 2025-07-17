import os
import subprocess
import json
import base64
import logging
import threading
import time
import requests
from flask import Flask, jsonify, render_template_string, request

# --- Configuration ---
SERF_EXECUTABLE_PATH = "/usr/bin/serf" # CONFIRM THIS PATH
SERF_RPC_ADDR = "127.0.0.1:7373" # CONFIRM YOUR SERF AGENT'S RPC ADDRESS
COMETBFT_RPC_URL = "http://localhost:26657" # CONFIGURE YOUR REAL COMETBFT RPC URL

# --- Flask Application Setup ---
app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Global Application State for Monitoring ---
app_metrics = {
    "serf_monitor_status": "Starting...",
    "serf_monitor_last_error": None,
    "serf_rpc_status": "Unknown", # Connection status to Serf agent's RPC
    "cometbft_rpc_status": "Unknown", # Connection status to CometBFT node's RPC
    "serf_events_received": 0,
    "cometbft_tx_broadcast": 0,
    "last_cometbft_rpc_check": None,
    "last_serf_rpc_check": None,
    "serf_members": [], # List of detected Serf cluster members
    "cometbft_node_info": {} # Basic info from CometBFT /status
}

metrics_lock = threading.Lock()

RECENT_ACTIVITY_MAX_ITEMS = 10
recent_activity_log = []

serf_monitor_thread_started = False
serf_monitor_thread_lock = threading.Lock()


class MockResponseCheckTx:
    def __init__(self, code=0, log="", hash="", height=0, index=0):
        self.code = code
        self.log = log
        self.hash = hash
        self.height = height
        self.index = index

    def to_dict(self):
        return {
            "Code": self.code,
            "Log": self.log,
            "Hash": self.hash,
            "Height": self.height,
            "Index": self.index
        }


class CometBFTMempoolClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        logger.info(f"CometBFTMempoolClient initialized with RPC URL: {self.rpc_url}")

    def CheckTx(self, tx_bytes: bytes, cb: callable, tx_info: dict) -> None:
        tx_b64 = base64.b64encode(tx_bytes).decode('utf-8')
        endpoint = f"{self.rpc_url}/broadcast_tx_sync"
        headers = {'Content-Type': 'application/json'}
        payload = {
            "jsonrpc": "2.0",
            "method": "broadcast_tx_sync",
            "params": [tx_b64],
            "id": 1
        }

        try:
            logger.info(f"Attempting to broadcast transaction (hash: {tx_b64[:10]}...) to CometBFT RPC: {endpoint}")
            response = requests.post(endpoint, headers=headers, json=payload, timeout=5)
            response.raise_for_status()
            rpc_result = response.json()

            if "result" in rpc_result:
                tx_result = rpc_result["result"]
                comet_response = MockResponseCheckTx(
                    code=tx_result.get("code", -1),
                    log=tx_result.get("log", "No log message"),
                    hash=tx_result.get("hash", ""),
                    height=tx_result.get("height", 0),
                    index=tx_result.get("index", 0)
                )
                logger.info(f"CometBFT RPC response received: {comet_response.to_dict()}")
                cb(comet_response)

                with metrics_lock:
                    app_metrics["cometbft_tx_broadcast"] += 1
                    app_metrics["cometbft_rpc_status"] = "Connected"
            elif "error" in rpc_result:
                error_details = rpc_result["error"]
                logger.error(f"CometBFT RPC error for broadcast_tx_sync: Code={error_details.get('code')}, Message={error_details.get('message')}, Data={error_details.get('data')}")
                cb(MockResponseCheckTx(code=error_details.get('code', -1), log=f"RPC Error: {error_details.get('message')}"))
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Error"
            else:
                logger.error(f"Unexpected CometBFT RPC response format: {rpc_result}")
                cb(MockResponseCheckTx(code=-1, log="Unexpected RPC response format"))
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Error"

        except requests.exceptions.Timeout:
            logger.error(f"CometBFT RPC request timed out to {endpoint}")
            cb(MockResponseCheckTx(code=-1, log="CometBFT RPC Timeout"))
            with metrics_lock:
                app_metrics["cometbft_rpc_status"] = "Timeout"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Could not connect to CometBFT RPC at {endpoint}: {e}")
            cb(MockResponseCheckTx(code=-1, log=f"CometBFT RPC Connection Error: {e}"))
            with metrics_lock:
                app_metrics["cometbft_rpc_status"] = "Disconnected"
        except Exception as e:
            logger.error(f"An unexpected error occurred during CometBFT RPC call: {e}")
            cb(MockResponseCheckTx(code=-1, log=f"Unexpected Error: {e}"))
            with metrics_lock:
                app_metrics["cometbft_rpc_status"] = "Error"


    def ReapMaxBytesMaxGas(self, max_bytes: int, max_gas: int) -> list:
        logger.debug("CometBFTMempoolClient: ReapMaxBytesMaxGas called (stub)")
        return []

    def ReapMaxTxs(self, max_txs: int) -> list:
        logger.debug("CometBFTMempoolClient: ReapMaxTxs called (stub)")
        return []

    def Update(self, height: int, txs: list, tx_results: list, pre_check: callable, post_check: callable) -> None:
        logger.debug(f"CometBFTMempoolClient: Update called (stub) for height {height} with {len(txs)} txs")
        return None

    def Flush(self) -> None:
        logger.debug("CometBFTMempoolClient: Flush called (stub)")
        return None

    def FlushAppConn(self) -> None:
        logger.debug("CometBFTMempoolClient: FlushAppConn called (stub)")
        return None

    def TxsAvailable(self) -> threading.Event:
        logger.debug("CometBFTMempoolClient: TxsAvailable called (stub)")
        event = threading.Event()
        event.set()
        return event

    def EnableTxsAvailable(self) -> None:
        logger.debug("CometBFTMempoolClient: EnableTxsAvailable called (stub)")
        return None

    def Size(self) -> int:
        logger.debug("CometBFTMempoolClient: Size called (stub)")
        return 0

    def SizeBytes(self) -> int:
        logger.debug("CometBFTMempoolClient: SizeBytes called (stub)")
        return 0

    def Lock(self) -> None:
        logger.debug("CometBFTMempoolClient: Lock called (stub)")
        return None

    def Unlock(self) -> None:
        logger.debug("CometBFTMempoolClient: Unlock called (stub)")
        return None

    def RemoveTxByKey(self, tx_key: bytes) -> None:
        logger.debug(f"CometBFTMempoolClient: RemoveTxByKey called (stub) for key: {tx_key}")
        return None


cometbft_mempool_client = CometBFTMempoolClient(COMETBFT_RPC_URL)


def serf_monitor_thread(serf_exec_path: str, rpc_addr: str, mempool_client: CometBFTMempoolClient):
    """
    Runs the 'serf monitor' command in a subprocess and processes its output.
    This function runs in a separate thread to avoid blocking the Flask main thread.
    It updates Serf connection status and parses events from monitor's plain text output.
    Also, it periodically checks Serf members and CometBFT RPC status.
    """
    logger.info(f"Serf monitor thread starting. Connecting to Serf RPC: {rpc_addr}")

    last_members_check_time = 0
    last_cometbft_status_check_time = 0
    MEMBER_CHECK_INTERVAL = 30 # seconds
    COMETBFT_STATUS_CHECK_INTERVAL = 10 # seconds

    while True: # Keep the monitoring alive, restart if command exits
        current_time = time.time()

        # --- Periodic Serf Members Check ---
        if current_time - last_members_check_time > MEMBER_CHECK_INTERVAL:
            try:
                members_cmd = [serf_exec_path, "members", "-format=json", f"-rpc-addr={rpc_addr}"]
                members_process = subprocess.run(members_cmd, capture_output=True, text=True, timeout=5)
                if members_process.returncode == 0:
                    members_data = json.loads(members_process.stdout)
                    with metrics_lock:
                        app_metrics["serf_members"] = members_data.get("members", [])
                    logger.debug(f"Updated Serf members: {len(app_metrics['serf_members'])} members found.")
                else:
                    logger.error(f"Failed to get Serf members: {members_process.stderr.strip()}")
                last_members_check_time = current_time
            except Exception as e:
                logger.error(f"Error fetching Serf members: {e}")
                last_members_check_time = current_time # Reset to avoid continuous errors

        # --- Periodic CometBFT RPC Status Check ---
        if current_time - last_cometbft_status_check_time > COMETBFT_STATUS_CHECK_INTERVAL:
            try:
                comet_status_endpoint = f"{mempool_client.rpc_url}/status"
                comet_response = requests.get(comet_status_endpoint, timeout=3)
                comet_response.raise_for_status()
                comet_status_data = comet_response.json()
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Connected"
                    app_metrics["cometbft_node_info"] = comet_status_data.get("result", {}).get("node_info", {})
                logger.debug(f"CometBFT RPC status check successful. Node: {app_metrics['cometbft_node_info'].get('moniker')}")
            except requests.exceptions.ConnectionError:
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Disconnected"
                    app_metrics["cometbft_node_info"] = {}
                logger.warning("CometBFT RPC: Connection error.")
            except requests.exceptions.Timeout:
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Timeout"
                    app_metrics["cometbft_node_info"] = {}
                logger.warning("CometBFT RPC: Request timed out.")
            except Exception as e:
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Error"
                    app_metrics["cometbft_node_info"] = {}
                logger.error(f"CometBFT RPC status check failed: {e}")
            finally:
                last_cometbft_status_check_time = current_time


        try:
            with metrics_lock:
                app_metrics["serf_rpc_status"] = "Connecting..."

            # Test Serf RPC connectivity before starting monitor
            test_cmd = [serf_exec_path, "members", f"-rpc-addr={rpc_addr}"]
            test_process = subprocess.run(test_cmd, capture_output=True, text=True, timeout=5)
            if test_process.returncode == 0:
                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Connected"
                    app_metrics["serf_monitor_status"] = "Running"
                    app_metrics["serf_monitor_last_error"] = None
                logger.info(f"Successfully connected to Serf RPC at {rpc_addr}")
            else:
                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Disconnected"
                    app_metrics["serf_monitor_status"] = "Failed to connect to Serf RPC"
                    app_metrics["serf_monitor_last_error"] = test_process.stderr.strip() or "Connection error"
                logger.error(f"Failed to connect to Serf RPC at {rpc_addr}: {test_process.stderr.strip()}")
                time.sleep(5)
                continue # Retry Serf connection loop

            # Launch "serf monitor" command
            cmd_args = [serf_exec_path, "monitor", f"-rpc-addr={rpc_addr}"]
            process = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

            logger.info("Serf monitor command launched. Listening for ALL events (plain text format)...")

            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue

                logger.debug(f"Received raw Serf monitor line: {line}")

                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Connected" # Keep status as connected if receiving lines

                # Robust parsing for "serf monitor" output
                if "Received event: user-event:" in line:
                    try:
                        event_part = line.split("Received event: user-event:", 1)[1].strip()
                        parts = event_part.split(" ", 1) # Split by first space for name and payload
                        if len(parts) == 2:
                            event_name = parts[0].strip()
                            event_payload_b64 = parts[1].strip()

                            logger.info(f"Parsed Serf user event: Name='{event_name}', Payload(base64)='{event_payload_b64[:30]}...'")

                            with metrics_lock:
                                app_metrics["serf_events_received"] += 1
                                new_activity = {
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                                    "type": "Serf User Event",
                                    "name": event_name,
                                    "payload_full": event_payload_b64, # Store full payload
                                    "payload_preview": event_payload_b64[:50] + ("..." if len(event_payload_b64) > 50 else ""),
                                    "cometbft_response": "Pending..."
                                }
                                recent_activity_log.insert(0, new_activity)
                                if len(recent_activity_log) > RECENT_ACTIVITY_MAX_ITEMS:
                                    recent_activity_log.pop()

                            try:
                                decoded_tx_bytes = base64.b64decode(event_payload_b64)
                            except Exception as e:
                                logger.error(f"Failed to base64 decode payload for event '{event_name}': {e}")
                                with metrics_lock:
                                    new_activity["cometbft_response"] = f"Decode Error: {e}"
                                continue

                            def check_tx_response_callback(response: MockResponseCheckTx, activity_entry=new_activity):
                                with metrics_lock:
                                    if response.code == 0:
                                        logger.info(
                                            f"CometBFT RPC Broadcast Success for event '{event_name}': "
                                            f"Code={response.code}, Log='{response.log}', Hash={response.hash}"
                                        )
                                        activity_entry["cometbft_response"] = f"Success (Code: {response.code}) Hash: {response.hash[:10]}..."
                                    else:
                                        logger.error(
                                            f"CometBFT RPC Broadcast Failed for event '{event_name}': "
                                            f"Code={response.code}, Log='{response.log}'"
                                        )
                                        activity_entry["cometbft_response"] = f"Failed (Code: {response.code}) Log: {response.log[:50]}..."


                            tx_info = {}

                            mempool_client.CheckTx(decoded_tx_bytes, check_tx_response_callback, tx_info)
                        else:
                            logger.debug(f"Could not parse user event format: {line}")
                    except Exception as e:
                        logger.error(f"Error parsing user event line '{line}': {e}")
                        with metrics_lock:
                            app_metrics["serf_monitor_last_error"] = f"Parsing Error: {e}"
                elif "agent: Accepted client:" in line or "[INFO] agent:" in line or "[INFO] serf:" in line:
                    logger.debug(f"Serf Agent/Client Log: {line}")
                else:
                    logger.debug(f"Other Serf monitor line: {line}")


            stderr_output = process.stderr.read()
            if stderr_output:
                logger.error(f"Serf monitor stderr: {stderr_output}")
                with metrics_lock:
                    app_metrics["serf_monitor_last_error"] = f"Serf CLI Error: {stderr_output.strip()}"

            process.wait()
            if process.returncode != 0:
                logger.error(f"Serf monitor command exited with non-zero status: {process.returncode}")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "Exited with Error"
                    app_metrics["serf_monitor_last_error"] = f"CLI Exit Code: {process.returncode}"
            else:
                logger.info("Serf monitor command exited gracefully.")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "Exited Gracefully"


        except FileNotFoundError:
            logger.critical(f"Serf executable not found at '{serf_exec_path}'. Please ensure Serf is installed and the path is correct.")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = "CRITICAL: Serf Executable Missing"
                app_metrics["serf_monitor_last_error"] = f"Executable not found: {serf_exec_path}"
            time.sleep(10)
        except Exception as e:
            logger.critical(f"Failed to start or monitor Serf: {e}")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = "Initialization Error"
                app_metrics["serf_monitor_last_error"] = f"Start Error: {e}"
            time.sleep(5)


@app.before_request
def before_request_hook():
    """
    Flask hook that runs before each request. Used to ensure the Serf monitor
    thread is started exactly once.
    """
    global serf_monitor_thread_started
    with serf_monitor_thread_lock:
        if not serf_monitor_thread_started:
            if not os.path.exists(SERF_EXECUTABLE_PATH) or not os.access(SERF_EXECUTABLE_PATH, os.X_OK):
                logger.critical(f"Serf executable not found or not executable at '{SERF_EXECUTABLE_PATH}'. Please check configuration.")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "CRITICAL: Serf Executable Missing"
                    app_metrics["serf_monitor_last_error"] = f"Path: {SERF_EXECUTABLE_PATH}"
                return

            thread = threading.Thread(
                target=serf_monitor_thread,
                args=(SERF_EXECUTABLE_PATH, SERF_RPC_ADDR, cometbft_mempool_client),
                name="SerfMonitorThread"
            )
            thread.daemon = True
            thread.start()
            logger.info("Serf monitor thread initiated.")
            serf_monitor_thread_started = True


@app.route('/')
def index():
    """Renders the main dashboard page with live monitoring data."""
    with metrics_lock:
        current_metrics = app_metrics.copy()
        current_activity_log = recent_activity_log[:]

    serf_status_color = "bg-gray-400"
    if current_metrics["serf_rpc_status"] == "Connected":
        serf_status_color = "bg-green-500"
    elif "Error" in current_metrics["serf_rpc_status"] or "Disconnected" in current_metrics["serf_rpc_status"] or "CRITICAL" in current_metrics["serf_monitor_status"]:
        serf_status_color = "bg-red-500"

    comet_status_color = "bg-gray-400"
    if current_metrics["cometbft_rpc_status"] == "Connected":
        comet_status_color = "bg-green-500"
    elif "Error" in current_metrics["cometbft_rpc_status"] or "Disconnected" in current_metrics["cometbft_rpc_status"] or "Timeout" in current_metrics["cometbft_rpc_status"]:
        comet_status_color = "bg-red-500"

    return render_template_string("""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Flask Serf CometBFT Bridge Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <style>
                body { font-family: 'Inter', sans-serif; }
            </style>
             <meta http-equiv="refresh" content="5">
        </head>
        <body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
            <div class="bg-white p-8 rounded-lg shadow-xl border border-gray-200 w-full max-w-4xl">
                <h1 class="text-4xl font-extrabold text-indigo-700 mb-6 text-center">
                    ✨ Serf <span class="text-gray-800">↔</span> CometBFT Bridge Dashboard ✨
                </h1>

                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-8">
                    <!-- Serf Status Card -->
                    <div class="bg-blue-50 border border-blue-200 rounded-lg p-6 shadow-md flex flex-col">
                        <h2 class="text-xl font-semibold text-blue-800 mb-4">Serf Monitoring</h2>
                        <div class="flex items-center mb-2">
                            <span class="inline-block h-4 w-4 rounded-full {{ serf_status_color }} mr-2"></span>
                            <span class="text-gray-700 font-medium">RPC Connection:</span>
                            <span class="ml-2 font-bold text-gray-900">{{ metrics.serf_rpc_status }}</span>
                        </div>
                        <p class="text-gray-700 mb-2">
                            Events Received: <span class="font-bold text-indigo-600">{{ metrics.serf_events_received }}</span>
                        </p>
                        <p class="text-gray-700 text-sm italic">
                            Monitor Thread: <span class="font-bold">{{ metrics.serf_monitor_status }}</span>
                        </p>
                        {% if metrics.serf_monitor_last_error %}
                        <p class="text-red-600 text-xs mt-1">Error: {{ metrics.serf_monitor_last_error }}</p>
                        {% endif %}
                    </div>

                    <!-- CometBFT Status Card -->
                    <div class="bg-purple-50 border border-purple-200 rounded-lg p-6 shadow-md flex flex-col">
                        <h2 class="text-xl font-semibold text-purple-800 mb-4">CometBFT Integration</h2>
                        <div class="flex items-center mb-2">
                            <span class="inline-block h-4 w-4 rounded-full {{ comet_status_color }} mr-2"></span>
                            <span class="text-gray-700 font-medium">RPC Connection:</span>
                            <span class="ml-2 font-bold text-gray-900">{{ metrics.cometbft_rpc_status }}</span>
                        </div>
                        <p class="text-gray-700 mb-2">
                            Transactions Broadcast: <span class="font-bold text-indigo-600">{{ metrics.cometbft_tx_broadcast }}</span>
                        </p>
                        <p class="text-gray-700 text-sm italic">
                            RPC URL: <span class="font-bold">{{ cometbft_rpc_url }}</span>
                        </p>
                         {% if metrics.cometbft_node_info %}
                            <p class="text-gray-700 text-sm italic mt-2">
                                Node Moniker: <span class="font-bold">{{ metrics.cometbft_node_info.moniker }}</span>
                            </p>
                             <p class="text-gray-700 text-xs italic">
                                Version: {{ metrics.cometbft_node_info.version }} (App: {{ metrics.cometbft_node_info.app_version }})
                            </p>
                        {% endif %}
                    </div>

                    <!-- Serf Cluster Members Card -->
                    <div class="bg-green-50 border border-green-200 rounded-lg p-6 shadow-md flex flex-col">
                        <h2 class="text-xl font-semibold text-green-800 mb-4">Serf Cluster Members</h2>
                        {% if metrics.serf_members %}
                            <ul class="space-y-2 text-sm max-h-48 overflow-y-auto">
                                {% for member in metrics.serf_members %}
                                    <li class="flex items-center">
                                        <span class="inline-block h-2 w-2 rounded-full {% if member.Status == 'alive' %}bg-green-500{% elif member.Status == 'failed' %}bg-red-500{% else %}bg-gray-500{% endif %} mr-2"></span>
                                        <span class="font-medium text-gray-900">{{ member.Name }}</span>
                                        <span class="text-gray-600 ml-2">({{ member.Addr }}:{{ member.Port }})</span>
                                        <span class="text-xs font-semibold ml-auto px-2 py-0.5 rounded-full {% if member.Status == 'alive' %}bg-green-200 text-green-800{% elif member.Status == 'failed' %}bg-red-200 text-red-800{% else %}bg-gray-200 text-gray-800{% endif %}">
                                            {{ member.Status }}
                                        </span>
                                    </li>
                                {% endfor %}
                            </ul>
                        {% else %}
                            <p class="text-gray-500 italic text-center">No Serf members discovered yet.</p>
                        {% endif %}
                    </div>
                </div>

                <!-- Recent Activity Log Section -->
                <div class="bg-gray-50 border border-gray-200 rounded-lg p-6 shadow-md">
                    <h2 class="text-xl font-semibold text-gray-800 mb-4">Recent Activity Log</h2>
                    {% if activity_log %}
                        <div class="space-y-4 max-h-96 overflow-y-auto">
                            {% for entry in activity_log %}
                            <div class="bg-white border border-gray-200 rounded-md p-3 shadow-sm flex flex-col sm:flex-row sm:items-center sm:justify-between text-sm">
                                <div class="flex-grow">
                                    <p class="font-medium text-gray-900 mb-1">{{ entry.timestamp }} - <span class="text-indigo-600">{{ entry.type }}</span>: <span class="font-bold">{{ entry.name }}</span></p>
                                    <p class="text-gray-700 break-all text-xs">Payload (Base64): <span class="font-mono text-gray-800">{{ entry.payload_full }}</span></p>
                                </div>
                                <div class="mt-2 sm:mt-0 sm:ml-4 text-right flex-shrink-0">
                                    <span class="font-semibold {% if 'Success' in entry.cometbft_response %}text-green-600{% elif 'Failed' in entry.cometbft_response or 'Error' in entry.cometbft_response %}text-red-600{% else %}text-gray-500{% endif %}">
                                        {{ entry.cometbft_response }}
                                    </span>
                                </div>
                            </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <p class="text-gray-500 italic text-center">No recent activity yet. Send a Serf user event!</p>
                    {% endif %}
                </div>

                <!-- Configuration Details -->
                <div class="mt-8 text-left bg-gray-50 p-4 rounded-md border border-gray-200 text-gray-700">
                    <p class="text-sm font-medium mb-1">Application Configuration:</p>
                    <p class="text-xs break-all">Serf Executable Path: <span class="font-mono text-gray-900">{{ serf_exec_path }}</span></p>
                    <p class="text-xs break-all">Serf RPC Address: <span class="font-mono text-gray-900">{{ serf_rpc_addr }}</span></p>
                    <p class="text-xs break-all font-bold mt-2">CometBFT RPC URL: <span class="font-mono text-blue-700">{{ cometbft_rpc_url }}</span></p>
                </div>

                <div class="mt-8 text-center">
                    <a href="/status" class="inline-flex items-center px-6 py-3 border border-transparent text-base font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition duration-150 ease-in-out">
                        View Raw Status API
                        <svg class="ml-2 -mr-1 h-5 w-5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                            <path fill-rule="evenodd" d="M10.293 15.707a1 1 0 010-1.414L14.586 10l-4.293-4.293a1 1 0 111.414-1.414l5 5a1 1 0 010 1.414l-5 5a1 1 0 01-1.414 0z" clip-rule="evenodd" />
                            <path fill-rule="evenodd" d="M4.293 15.707a1 1 0 010-1.414L8.586 10 4.293 5.707a1 1 0 011.414-1.414l5 5a1 1 0 010 1.414l-5 5a1 1 0 01-1.414 0z" clip-rule="evenodd" />
                        </svg>
                    </a>
                </div>
            </div>
        </body>
        </html>
    """,
    serf_exec_path=SERF_EXECUTABLE_PATH,
    serf_rpc_addr=SERF_RPC_ADDR,
    cometbft_rpc_url=COMETBFT_RPC_URL,
    metrics=current_metrics,
    activity_log=current_activity_log,
    serf_status_color=serf_status_color,
    comet_status_color=comet_status_color
    )


@app.route('/status')
def status():
    """An API endpoint to check the application status and metrics."""
    with metrics_lock:
        current_metrics = app_metrics.copy()
        current_activity_log = recent_activity_log[:]
    return jsonify({
        "status": "running",
        "serf_rpc_address": SERF_RPC_ADDR,
        "cometbft_rpc_url": COMETBFT_RPC_URL,
        "mempool_integration": "real_rpc",
        "metrics": current_metrics,
        "recent_activity_log": current_activity_log
    })


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)

