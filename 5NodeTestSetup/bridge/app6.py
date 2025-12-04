import os
import requests
import logging
import threading
import time
import json
import base64
import random

from flask import Flask, jsonify, render_template_string, request, redirect, url_for

# --- LOGGING SETUP (MOVED TO TOP FOR GLOBAL EFFECT) ---
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration for Flask Frontend ---
# This is the address where your Go Backend Bridge is listening
GO_BACKEND_URL = "http://localhost:8080" # Default for local Go backend

# --- Flask Application Setup ---
app = Flask(__name__)

# Global State for UI Refresh (will be populated by fetch_metrics_thread)
app_metrics = {}
recent_activity_log = []

# Thread for periodically fetching metrics from Go backend
def fetch_metrics_thread():
    global app_metrics, recent_activity_log
    while True:
        try:
            # Fetch general metrics from Go backend
            metrics_response = requests.get(f"{GO_BACKEND_URL}/metrics", timeout=3)
            metrics_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            app_metrics = metrics_response.json() # Update global metrics

            # Fetch activity log from Go backend
            activity_response = requests.get(f"{GO_BACKEND_URL}/activity-log", timeout=3)
            activity_response.raise_for_status()
            recent_activity_log = activity_response.json() # Update global activity log

            logger.debug(f"Fetched metrics and activity log from Go backend. Serf members: {len(app_metrics.get('serf_members', []))}, TXs: {app_metrics.get('cometbft_tx_broadcast', 0)}")

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Could not connect to Go Backend Bridge at {GO_BACKEND_URL}: {e}")
            # Reset metrics to indicate backend is down
            app_metrics = {
                "serf_monitor_status": "Backend Down",
                "serf_rpc_status": "Disconnected",
                "cometbft_rpc_status": "Disconnected",
                "serf_events_received": 0,
                "cometbft_tx_broadcast": 0,
                "serf_members": [],
                "cometbft_node_info": {},
                "serf_monitor_last_error": f"Backend Connection Error: {e}"
            }
            recent_activity_log = [] # Clear log if backend is down
        except requests.exceptions.Timeout:
            logger.warning(f"Go Backend Bridge request timed out to {GO_BACKEND_URL}")
            app_metrics["serf_monitor_status"] = "Backend Timeout"
            app_metrics["serf_monitor_last_error"] = "Backend Timeout"
        except Exception as e:
            logger.error(f"Error fetching data from Go Backend: {e}")
            app_metrics["serf_monitor_status"] = "Backend Error"
            app_metrics["serf_monitor_last_error"] = f"Backend Fetch Error: {e}"
        
        time.sleep(2) # Fetch every 2 seconds for responsiveness


# Start the metrics fetching thread when Flask app starts (only once)
@app.before_request
def start_metrics_fetch_thread_hook():
    global _metrics_fetch_thread_started
    if not hasattr(start_metrics_fetch_thread_hook, '_metrics_fetch_thread_started'):
        thread = threading.Thread(target=fetch_metrics_thread, name="MetricsFetchThread")
        thread.daemon = True
        thread.start()
        logger.info("Metrics fetch thread initiated.")
        start_metrics_fetch_thread_hook._metrics_fetch_thread_started = True


@app.route('/trigger_random_transaction', methods=['POST'])
def trigger_random_transaction():
    """
    Triggers a conceptual 'transaction' between two random Serf nodes by
    making an API call to the Go Backend Bridge.
    """
    # Fetch live members from the global app_metrics which is updated by fetch_metrics_thread
    members = app_metrics.get("serf_members", [])
    alive_members = [m for m in members if m.get("status") == "alive"]

    if len(alive_members) < 2:
        return jsonify({"status": "error", "message": "Need at least 2 ALIVE Serf members to perform a random transaction."}), 400

    sender_node, receiver_node = random.sample(alive_members, 2)

    # Construct a simple string payload for kvstore
    transaction_payload_str = f"transfer:{sender_node['name']}_to_{receiver_node['name']}_amount_{random.randint(1, 100)}tokens_at_{time.time()}"
    transaction_payload_b64 = base64.b64encode(transaction_payload_str.encode('utf-8')).decode('utf-8')

    event_name = f"transfer-{sender_node['name']}-to-{receiver_node['name']}"

    # Make API call to Go Backend Bridge to dispatch the event
    try:
        backend_endpoint = f"{GO_BACKEND_URL}/trigger-transaction"
        payload = {"event_name": event_name, "payload_b64": transaction_payload_b64}
        
        logger.info(f"Flask dispatching to Go Backend: {backend_endpoint}, Event: {event_name}")
        response = requests.post(backend_endpoint, json=payload, timeout=10)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        
        result = response.json()
        logger.info(f"Go Backend response for transaction trigger: {result}")
        
        if result.get("status") == "success":
            return jsonify({"status": "success", "message": f"Transaction event '{event_name}' dispatched via Go backend.", "payload": transaction_payload_str}), 200
        else:
            return jsonify({"status": "error", "message": f"Go backend failed to dispatch event: {result.get('message', 'Unknown error')}"}), 500
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Could not connect to Go Backend Bridge at {GO_BACKEND_URL} to trigger transaction: {e}")
        return jsonify({"status": "error", "message": f"Could not connect to Go Backend Bridge: {e}"}), 500
    except requests.exceptions.Timeout:
        logger.error(f"Go Backend Bridge request timed out when triggering transaction to {GO_BACKEND_URL}")
        return jsonify({"status": "error", "message": "Go Backend Bridge request timed out."}), 500
    except Exception as e:
        logger.error(f"An unexpected error occurred when triggering transaction via Go Backend: {e}")
        return jsonify({"status": "error", "message": f"Internal server error triggering transaction: {e}"}), 500


@app.route('/')
def index():
    """Renders the main dashboard page with live monitoring data fetched from the Go backend."""
    current_metrics = app_metrics.copy()
    current_activity_log = recent_activity_log[:]

    # Determine status colors for UI based on metrics from Go backend
    serf_status_color = "bg-gray-700"
    if current_metrics.get("serf_rpc_status") == "Connected":
        serf_status_color = "bg-green-500"
    elif "Error" in current_metrics.get("serf_rpc_status", "") or "Disconnected" in current_metrics.get("serf_rpc_status", "") or "CRITICAL" in current_metrics.get("serf_monitor_status", "") or "Backend" in current_metrics.get("serf_monitor_status", ""):
        serf_status_color = "bg-red-500"

    comet_status_color = "bg-gray-700"
    if current_metrics.get("cometbft_rpc_status") == "Connected":
        comet_status_color = "bg-green-500"
    elif "Error" in current_metrics.get("cometbft_rpc_status", "") or "Disconnected" in current_metrics.get("cometbft_rpc_status", "") or "Timeout" in current_metrics.get("cometbft_rpc_status", ""):
        comet_status_color = "bg-red-500"

    # Set display colors for individual Serf members
    for member in current_metrics.get("serf_members", []):
        if member.get("status") == "alive":
            member["display_status_color"] = "bg-green-500"
        elif member.get("status") == "failed":
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
                    background-color: #f8f9fa; /* Office light background */
                    color: #212529; /* Dark text for contrast */
                }
                .card {
                    background-color: #ffffff; /* White card background */
                    border: 1px solid #dee2e6; /* Light grey border */
                    box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075); /* Subtle shadow */
                    border-radius: 0.5rem; /* Rounded corners */
                }
                .header-text {
                    color: #0056b3; /* Professional blue for headings */
                }
                .text-indigo-600 { color: #6610f2; } /* Purple accent for stats */
                .text-blue-600 { color: #007bff; } /* Primary blue for links/accents */
                .text-purple-800 { color: #6f42c1; } /* Darker purple for headings */
                .text-green-800 { color: #28a745; } /* Standard green for headings */
                .text-gray-900 { color: #212529; } /* Very dark text */
                .text-gray-700 { color: #495057; } /* Darker gray text */
                .text-gray-600 { color: #6c757d; } /* Medium gray text */
                .text-gray-500 { color: #adb5bd; } /* Lighter gray text/placeholders */
                .text-gray-50 { color: #212529; } /* Ensure dark text on light backgrounds */
                .text-gray-100 { color: #343a40; }
                .text-gray-200 { color: #495057; }
                .text-gray-300 { color: #6c757d; }
                .text-gray-400 { color: #adb5bd; }


                /* Status dots */
                .bg-green-500 { background-color: #28a745; } /* Success green */
                .bg-red-500 { background-color: #dc3545; } /* Error red */
                .bg-gray-400 { background-color: #6c757d; } /* Neutral gray */
                .bg-gray-700 { background-color: #adb5bd; } /* Light gray for unknown status */

                /* Card backgrounds for different sections (light theme compatible) */
                .bg-blue-50 { background-color: #e6f7ff; border-color: #b3e0ff; } /* Light blue for serf */
                .bg-purple-50 { background-color: #f3e6ff; border-color: #d6b3ff; } /* Light purple for cometbft */
                .bg-green-50 { background-color: #e6fff7; border-color: #b3ffcc; } /* Light green for members */
                .bg-gray-50 { background-color: #f8f9fa; border-color: #e9ecef; } /* Light gray for log/config */

                /* Specific text adjustments for dark elements on light background */
                .bg-gray-800 { background-color: #e9ecef; color: #343a40; } /* Light gray for inner card elements */
                .border-gray-700 { border-color: #ced4da; } /* Lighter border for inner card elements */

                /* Activity log status text colors on light background */
                .text-green-600 { color: #218838; } /* Darker green */
                .text-red-600 { color: #c82333; } /* Darker red */
                .text-indigo-600 { color: #5a2e8c; } /* Darker indigo */
                .text-blue-600 { color: #0056b3; } /* Darker blue */


                /* Scrollbars for activity log */
                .overflow-y-auto::-webkit-scrollbar {
                    width: 8px;
                }
                .overflow-y-auto::-webkit-scrollbar-track {
                    background: #e9ecef;
                    border-radius: 10px;
                }
                .overflow-y-auto::-webkit-scrollbar-thumb {
                    background: #ced4da;
                    border-radius: 10px;
                }
                .overflow-y-auto::-webkit-scrollbar-thumb:hover {
                    background: #adb5bd;
                }

                .payload-text {
                    white-space: pre-wrap;
                    word-break: break-all;
                }

                /* Button Styling */
                .bg-teal-600 { background-color: #17a2b8; } /* Teal for main button */
                .hover\:bg-teal-700:hover { background-color: #138496; } /* Tailwind needs this specific escape */

            </style>
             <meta http-equiv="refresh" content="5">
        </head>
        <body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
            <div class="card p-8 w-full max-w-4xl">
                <h1 class="text-4xl font-extrabold header-text mb-6 text-center">
                    ✨ Serf <span class="text-gray-900">↔</span> CometBFT Bridge Dashboard ✨
                </h1>

                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-8">
                    <!-- Serf Monitoring Card -->
                    <div class="card bg-blue-50 p-6 flex flex-col">
                        <h2 class="text-xl font-semibold header-text mb-4">Serf Monitoring</h2>
                        <div class="flex items-center mb-2">
                            <span class="inline-block h-4 w-4 rounded-full {{ serf_status_color }} mr-2 animate-pulse"></span>
                            <span class="text-gray-700 font-medium">RPC Connection:</span>
                            <span class="ml-2 font-bold text-gray-900">{{ metrics.get('serf_rpc_status', 'Unknown') }}</span>
                        </div>
                        <p class="text-gray-700 mb-2">
                            Events Received: <span class="font-bold text-indigo-600">{{ metrics.get('serf_events_received', 0) }}</span>
                        </p>
                        <p class="text-gray-600 text-sm italic">
                            Monitor Thread: <span class="font-bold">{{ metrics.get('serf_monitor_status', 'N/A') }}</span>
                        </p>
                        {% if metrics.get('serf_monitor_last_error') %}
                        <p class="text-red-500 text-xs mt-1">Error: {{ metrics.serf_monitor_last_error }}</p>
                        {% endif %}
                    </div>

                    <!-- CometBFT Status Card -->
                    <div class="card bg-purple-50 p-6 flex flex-col">
                        <h2 class="text-xl font-semibold header-text mb-4">CometBFT Integration</h2>
                        <div class="flex items-center mb-2">
                            <span class="inline-block h-4 w-4 rounded-full {{ comet_status_color }} mr-2 animate-pulse"></span>
                            <span class="text-gray-700 font-medium">RPC Connection:</span>
                            <span class="ml-2 font-bold text-gray-900">{{ metrics.get('cometbft_rpc_status', 'Unknown') }}</span>
                        </div>
                        <p class="text-gray-700 mb-2">
                            Transactions Broadcast: <span class="font-bold text-indigo-600">{{ metrics.get('cometbft_tx_broadcast', 0) }}</span>
                        </p>
                        <p class="text-gray-600 text-sm italic">
                            RPC URL: <span class="font-bold text-blue-600">{{ cometbft_rpc_url }}</span>
                        </p>
                         {% if metrics.get('cometbft_node_info') %}
                            <p class="text-gray-600 text-sm italic mt-2">
                                Node Moniker: <span class="font-bold">{{ metrics.cometbft_node_info.moniker }}</span>
                            </p>
                             <p class="text-gray-600 text-xs italic">
                                Version: {{ metrics.cometbft_node_info.version }} (App: {{ metrics.cometbft_node_info.app_version }})
                            </p>
                        {% endif %}
                    </div>

                    <!-- Serf Cluster Members Card -->
                    <div class="card bg-green-50 p-6 flex flex-col">
                        <h2 class="text-xl font-semibold header-text mb-4">Serf Cluster Members ({{ metrics.get('serf_members', [])|length }} Nodes)</h2>
                        {% if metrics.get('serf_members') %}
                            <ul class="space-y-2 text-sm max-h-48 overflow-y-auto">
                                {% for member in metrics.serf_members %}
                                    <li class="flex items-center bg-gray-50 p-2 rounded-md border border-gray-200">
                                        <span class="inline-block h-3 w-3 rounded-full {{ member.display_status_color }} mr-2"></span>
                                        <span class="font-medium text-gray-900">{{ member.name }}</span>
                                        <span class="text-gray-700 ml-2">({{ member.addr }}:{{ member.port }})</span>
                                        <span class="text-xs font-semibold ml-auto px-2 py-0.5 rounded-full {% if member.status == 'alive' %}bg-green-600 text-white{% elif member.status == 'failed' %}bg-red-600 text-white{% else %}bg-gray-600 text-white{% endif %}">
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

                <!-- Recent Activity Log Section -->
                <div class="card bg-gray-50 p-6 mb-8">
                    <h2 class="text-xl font-semibold header-text mb-4">Recent Activity Log</h2>
                    {% if activity_log %}
                        <div class="space-y-4 max-h-80 overflow-y-auto">
                            {% for entry in activity_log %}
                            <div class="bg-gray-50 border border-gray-200 rounded-md p-3 shadow-sm flex flex-col text-sm">
                                <div class="flex justify-between items-center mb-1">
                                    <p class="font-medium text-gray-900">{{ entry.timestamp }} - <span class="text-indigo-600">{{ entry.type }}</span>: <span class="font-bold">{{ entry.name }}</span></p>
                                    <span class="font-semibold {% if 'Success' in entry.cometbft_response %}text-green-600{% elif 'Failed' in entry.cometbft_response or 'Error' in entry.cometbft_response %}text-red-600{% else %}text-gray-600{% endif %} text-xs px-2 py-0.5 rounded-full">
                                        {{ entry.cometbft_response }}
                                    </span>
                                </div>
                                <p class="text-gray-700 text-xs payload-text">Payload: <span class="font-mono text-gray-900">{{ entry.payload_full }}</span></p>
                            </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <p class="text-gray-600 italic text-center">No recent activity yet. Send a Serf user event!</p>
                    {% endif %}
                </div>

                <!-- Transaction Trigger Button -->
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


                <!-- Configuration Details -->
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
    app.run(debug=True, host='0.0.0.0', port=5000)
