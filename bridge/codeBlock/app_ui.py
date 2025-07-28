import os
import json
import base64
import logging
import threading
import random
import redis
from flask import Flask, jsonify, render_template_string
import hashlib
from datetime import datetime, timezone
from cometbft_client import MempoolClient
from serf_client import serf_monitor_thread, app_metrics

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger(__name__)

SERF_EXECUTABLE_PATH = "/usr/bin/serf"
SERF_RPC_ADDR = "172.20.20.7:7373"
COMETBFT_RPC_URL = "http://localhost:26657"

app = Flask(__name__)

metrics_lock = threading.Lock()
recent_activity_log = []

serf_monitor_thread_started = False
serf_monitor_thread_lock = threading.Lock()
cometbft_mempool_client = MempoolClient(COMETBFT_RPC_URL)
r = redis.Redis(host='localhost', port=6379, decode_responses=True)
stream_key = "transEventStream"


@app.before_request
def before_request_hook():
    global serf_monitor_thread_started
    with serf_monitor_thread_lock:
        if not serf_monitor_thread_started:
            if not os.path.exists(SERF_EXECUTABLE_PATH) or not os.access(SERF_EXECUTABLE_PATH, os.X_OK):
                logger.critical(
                    f"Serf executable not found or not executable at '{SERF_EXECUTABLE_PATH}'. Please check configuration.")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "CRITICAL: Executable Missing"
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


@app.route('/trigger_random_transaction', methods=['POST'])
def trigger_random_transaction():
    with metrics_lock:
        members = [m for m in app_metrics["serf_members"] if m.get("status") == "alive"]

    if len(members) < 2:
        return jsonify(
            {"status": "error", "message": "Need at least 2 ALIVE Serf members to perform a random transaction."}), 400

    sender_node, receiver_node = random.sample(members, 2)

    transaction_data = {
        "type": "transfer",
        "from_node": sender_node["name"],
        "to_node": receiver_node["name"],
        "amount": f"{random.randint(1, 100)} tokens",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    full_transaction_json = json.dumps(transaction_data)
    transaction_hash = hashlib.sha256(full_transaction_json.encode('utf-8')).hexdigest()
    payload_b64_for_serf_event = base64.b64encode(full_transaction_json.encode('utf-8')).decode('utf-8')
    event_name = f"transfer-{sender_node['name']}-to-{receiver_node['name']}"

    try:
        msg = {"event": event_name, "payload": payload_b64_for_serf_event, "timestamp": datetime.now(timezone.utc).isoformat()}
        msg_id = r.xadd(stream_key, msg)

        if msg_id:
            logger.debug(f"Generated transaction JSON: {full_transaction_json}")
            logger.info(f"Base64-encoded payload: {payload_b64_for_serf_event}")
            logger.info(f"Successfully dispatched transaction event '{event_name}' to the queue: Msg ID: {msg_id}")
            return jsonify(
                {"status": "success",
                 "message": f"Transaction event '{event_name}' dispatched to the Message Queue.",
                 "payload_hash": transaction_hash,
                 "msg_id": msg_id}
            ), 200
        else:
            logger.error(f"Failed to dispatch transaction event '{event_name}'.")
            return jsonify(
                {"status": "error", "message": f"Failed to dispatch event:"}), 500
    except Exception as e:
        logger.error(f"Exception while dispatching Serf event: {e}")
        return jsonify({"status": "error", "message": f"Internal server error: {e}"}), 500


@app.route('/status')
def status():
    with metrics_lock:
        current_metrics = app_metrics.copy()
        current_activity_log = recent_activity_log[:]
    return jsonify({
        "status": "running",
        "serf_rpc_address": SERF_RPC_ADDR,
        "cometbft_rpc_url": COMETBFT_RPC_URL,
        "mempool_integration": "real_rpc_with_consensus_check",
        "metrics": current_metrics,
        "recent_activity_log": current_activity_log
    })


@app.route('/')
def index():
    with metrics_lock:
        current_metrics = app_metrics.copy()

    serf_status_color = "bg-gray-700"
    if current_metrics["serf_rpc_status"] == "Connected":
        serf_status_color = "bg-green-500"
    elif "Error" in current_metrics["serf_rpc_status"] or "Disconnected" in current_metrics[
        "serf_rpc_status"] or "CRITICAL" in current_metrics["serf_monitor_status"]:
        serf_status_color = "bg-red-500"

    comet_status_color = "bg-gray-700"
    if current_metrics["cometbft_rpc_status"] == "Connected":
        comet_status_color = "bg-green-500"
    elif "Error" in current_metrics["cometbft_rpc_status"] or "Disconnected" in current_metrics[
        "cometbft_rpc_status"] or "Timeout" in current_metrics["cometbft_rpc_status"]:
        comet_status_color = "bg-red-500"
    elif current_metrics["cometbft_rpc_status"].startswith("Broadcasting"):
        comet_status_color = "bg-yellow-500"

    for member in current_metrics["serf_members"]:
        if member["status"] == "alive":
            member["display_status_color"] = "bg-green-500"
        elif member["status"] == "failed":
            member["display_status_color"] = "bg-red-500"
        else:
            member["display_status_color"] = "bg-gray-500"

    return render_template_string("""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Flask Serf CometBFT Bridge Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <style>
                body {
                    font-family: 'Inter', sans-serif;
                    background-color: #f8f9fa;
                    color: #212529;
                }
                .card {
                    background-color: #ffffff;
                    border: 1px solid #dee2e6;
                    box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075);
                }
                .header-text {
                    color: #0056b3;
                }
                .text-indigo-600 { color: #6610f2; }
                .text-blue-600 { color: #007bff; }
                .text-purple-800 { color: #6f42c1; }
                .text-green-800 { color: #28a745; }
                .text-gray-900 { color: #212529; }
                .text-gray-700 { color: #495057; }
                .text-gray-600 { color: #6c757d; }
                .text-gray-500 { color: #adb5bd; }
                .text-gray-50 { color: #212529; }
                .text-gray-100 { color: #343a40; }
                .text-gray-200 { color: #495057; }
                .text-gray-300 { color: #6c757d; }
                .text-gray-400 { color: #adb5bd; }

                .bg-green-500 { background-color: #28a745; }
                .bg-red-500 { background-color: #dc3545; }
                .bg-yellow-500 { background-color: #ffc107; }
                .bg-gray-400 { background-color: #6c757d; }
                .bg-gray-700 { background-color: #adb5bd; }

                .bg-blue-50 { background-color: #e6f7ff; border-color: #b3e0ff; }
                .bg-purple-50 { background-color: #f3e6ff; border-color: #d6b3ff; }
                .bg-green-50 { background-color: #e6fff7; border-color: #b3ffcc; }
                .bg-gray-50 { background-color: #f8f9fa; border-color: #e9ecef; }

                .bg-gray-800 { background-color: #e9ecef; color: #343a40; }
                .border-gray-700 { border-color: #ced4da; }

                .text-green-600 { color: #218838; }
                .bg-green-100 { background-color: #d4edda; }
                .text-green-700 { color: #155724; }
                .bg-green-200 { background-color: #c3e6cb; }
                .text-green-800 { color: #0f5132; }

                .text-red-600 { color: #c82333; }
                .bg-red-100 { background-color: #f8d7da; }
                .text-red-700 { color: #721c24; }

                .bg-yellow-200 { background-color: #ffeeba; }
                .text-yellow-800 { color: #664d03; }

                .bg-blue-100 { color: #cfe2ff; }
                .text-blue-700 { color: #052c65; }

                .text-gray-600 { color: #6c757d; }

                .overflow-y-auto::-webkit-scrollbar {
                    width: 8px;
                }
                .overflow-y-auto::-webkit-scrollbar-track {
                    background: #e9ecef;
                    border-radius: 10px;
                }
                .overflow-y-auto::-webkit-webkit-scrollbar-thumb {
                    background: #ced4da;
                    border-radius: 10px;
                }
                .overflow-y-auto::-webkit-scrollbar-thumb:hover {
                    background: #adb5bd;
                    border-radius: 10px;
                }

                .payload-text {
                    white-space: pre-wrap;
                    word-break: break-all;
                }

                .bg-teal-600 { background-color: #17a2b8; }
                .hover:bg-teal-700:hover { background-color: #138496; }

            </style>
             <meta http-equiv="refresh" content="5">
        </head>
        <body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
            <div class="card p-8 rounded-lg w-full max-w-4xl">
                <h1 class="text-4xl font-extrabold header-text mb-6 text-center">
                    ✨ Serf <span class="text-gray-900">↔</span> CometBFT Bridge Dashboard ✨
                </h1>

                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-8">
                    <div class="card bg-blue-50 p-6 flex flex-col">
                        <h2 class="text-xl font-semibold header-text mb-4">Serf Monitoring</h2>
                        <div class="flex items-center mb-2">
                            <span class="inline-block h-4 w-4 rounded-full {{ serf_status_color }} mr-2 animate-pulse"></span>
                            <span class="text-gray-700 font-medium">RPC Connection:</span>
                            <span class="ml-2 font-bold text-gray-900">{{ metrics.serf_rpc_status }}</span>
                        </div>
                        <p class="text-gray-700 mb-2">
                            Events Received: <span class="font-bold text-indigo-600">{{ metrics.serf_events_received }}</span>
                        </p>
                        <p class="text-gray-600 text-sm italic">
                            Monitor Thread: <span class="font-bold">{{ metrics.serf_monitor_status }}</span>
                        </p>
                        {% if metrics.serf_monitor_last_error %}
                        <p class="text-red-500 text-xs mt-1">Error: {{ metrics.serf_monitor_last_error }}</p>
                        {% endif %}
                    </div>

                    <div class="card bg-purple-50 p-6 flex flex-col">
                        <h2 class="text-xl font-semibold header-text mb-4">CometBFT Integration</h2>
                        <div class="flex items-center mb-2">
                            <span class="inline-block h-4 w-4 rounded-full {{ comet_status_color }} mr-2 animate-pulse"></span>
                            <span class="text-gray-700 font-medium">RPC Connection:</span>
                            <span class="ml-2 font-bold text-gray-900">{{ metrics.cometbft_rpc_status }}</span>
                        </div>
                        <p class="text-gray-700 mb-2">
                            Transactions Broadcast: <span class="font-bold text-indigo-600">{{ metrics.cometbft_tx_broadcast }}</span>
                        </p>
                        <p class="text-gray-600 text-sm italic">
                            RPC URL: <span class="font-bold text-blue-600">{{ cometbft_rpc_url }}</span>
                        </p>
                            {% if metrics.cometbft_node_info %}
                                <p class="text-gray-600 text-sm italic mt-2">
                                    Node Moniker: <span class="font-bold">{{ metrics.cometbft_node_info.moniker }}</span>
                                </p>
                                <p class="text-gray-600 text-xs italic">
                                    Version: {{ metrics.cometbft_node_info.version }} (App: {{ metrics.cometbft_node_info.app_version }})
                                </p>
                            {% endif %}
                    </div>

                    <div class="card bg-green-50 p-6 flex flex-col">
                        <h2 class="text-xl font-semibold header-text mb-4">Serf Cluster Members ({{ metrics.serf_members|length }} Nodes)</h2>
                        {% if metrics.serf_members %}
                            <ul class="space-y-2 text-sm max-h-48 overflow-y-auto">
                                {% for member in metrics.serf_members %}
                                    <li class="flex items-center bg-gray-50 p-2 rounded-md border border-gray-200">
                                        <span class="inline-block h-3 w-3 rounded-full {{ member.display_status_color }} mr-2"></span>
                                        <span class="font-medium text-gray-900">{{ member.name }}</span>
                                        <span class="text-gray-700 ml-2">({{ member.addr }}:{{ member.port }})</span>
                                        <span class="text-xs font-semibold ml-auto px-2 py-0.5 rounded-full {% if member.status == 'alive' %}bg-green-200 text-green-800{% elif member.status == 'failed' %}bg-red-200 text-red-800{% else %}bg-gray-200 text-gray-800{% endif %}">
                                            {{ member.status }}
                                        </span>
                                    </li>
                                {% endfor %}
                            </ul>
                        {% else %}
                            <p class="text-gray-600 italic text-center">No Serf members discovered yet. Ensure your topology is deployed and agents are joined!</p>
                        {% endif %}
                    </div>
                </div>

                <div class="card bg-gray-50 p-6 mb-8">
                    <h2 class="text-xl font-semibold header-text mb-4">Recent Activity Log</h2>
                    {% if activity_log %}
                        <div class="space-y-4 max-h-80 overflow-y-auto">
                            {% for entry in activity_log %}
                            <div class="bg-gray-50 border border-gray-200 rounded-md p-3 shadow-sm flex flex-col text-sm">
                                <div class="flex justify-between items-center mb-1">
                                    <p class="font-medium text-gray-900">
                                        {{ entry.timestamp }} -
                                        <span class="text-indigo-600">{{ entry.type }}</span>:
                                        <span class="font-bold">{{ entry.name }}</span>
                                        {% if entry.processed_by_node %}
                                            <span class="text-gray-600 text-xs italic ml-2">(Processed by: {{ entry.processed_by_node }})</span>
                                        {% elif entry.reported_by_node %}
                                            <span class="text-gray-600 text-xs italic ml-2">(Reported by: {{ entry.reported_by_node }})</span>
                                        {% endif %}
                                    </p>
                                    <div class="flex flex-col items-end">
                                        <span class="font-semibold text-xs px-2 py-0.5 rounded-full mb-1
                                            {% if 'Code: 0' in entry.cometbft_broadcast_response %}bg-green-100 text-green-700{% elif 'Failed' in entry.cometbft_broadcast_response or 'Error' in entry.cometbft_broadcast_response %}bg-red-100 text-red-700{% else %}bg-gray-100 text-gray-700{% endif %}">
                                            Broadcast: {{ entry.cometbft_broadcast_response }}
                                        </span>
                                        <span class="font-semibold text-xs px-2 py-0.5 rounded-full
                                            {% if 'Committed!' in entry.cometbft_consensus_status %}bg-green-200 text-green-800{% elif 'Timeout' in entry.cometbft_consensus_status or 'Not Found' in entry.cometbft_consensus_status or 'Failed' in entry.cometbft_consensus_status %}bg-red-200 text-red-800{% else %}bg-blue-100 text-blue-700{% endif %}">
                                            Consensus: {{ entry.cometbft_consensus_status }}
                                        </span>
                                    </div>
                                </div>
                                <p class="text-gray-700 text-xs payload-text">Payload: <span class="font-mono text-gray-900">{{ entry.payload_full }}</span></p>
                            </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <p class="text-gray-600 italic text-center">No recent activity yet. Send a Serf user event!</p>
                    {% endif %}
                </div>

                <div class="mt-8 text-center">
                    <form action="{{ url_for('trigger_random_transaction') }}" method="post" onsubmit="alert('Attempting to dispatch transaction. Check console & dashboard log!');">
                        <button type="submit" class="inline-flex items-center px-8 py-4 border border-transparent text-base font-bold rounded-md shadow-lg text-white bg-teal-600 hover:bg-teal-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-teal-500 transition duration-150 ease-in-out">
                            <svg class="w-6 h-6 mr-2 -ml-1" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4"></path></svg>
                            Dispatch Random Serf Transaction to CometBFT
                        </button>
                    </form>
                    <p class="text-gray-600 text-sm italic mt-4">
                        (This simulates a transaction from one Serf node to another,
                        then sends it to CometBFT via RPC for validation & consensus.)
                    </p>
                </div>

                <div class="card bg-gray-50 p-4 rounded-md mt-8">
                    <p class="text-sm font-medium header-text mb-1">Application Configuration:</p>
                    <p class="text-xs break-all text-gray-700">Serf Executable Path: <span class="font-mono text-gray-900">{{ serf_exec_path }}</span></p>
                    <p class="text-xs break-all text-gray-700">Serf RPC Address: <span class="font-mono text-gray-900">{{ serf_rpc_addr }}</span></p>
                    <p class="text-xs break-all font-bold mt-2 text-gray-700">CometBFT RPC URL: <span class="font-mono text-blue-600">{{ cometbft_rpc_url }}</span></p>
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
                                  metrics=app_metrics,
                                  activity_log=recent_activity_log,
                                  serf_status_color=serf_status_color,
                                  comet_status_color=comet_status_color
                                  )


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
