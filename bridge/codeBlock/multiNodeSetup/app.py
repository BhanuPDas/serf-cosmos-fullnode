import os
import json
import base64
import logging
import threading
import redis
from flask import Flask, jsonify, request
from datetime import datetime, timezone
from cometbft_client import MempoolClient
from serf_client import serf_monitor_thread, app_metrics

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger(__name__)

SERF_EXECUTABLE_PATH = "/opt/serfapp/serf"
SERF_RPC_ADDR = "127.0.0.1:7373"
COMETBFT_RPC_URL = "http://localhost:26657"

app = Flask(__name__)

metrics_lock = threading.Lock()

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
                return jsonify({"error": "Serf executable not found or not executable"}), 500

            thread = threading.Thread(
                target=serf_monitor_thread,
                args=(SERF_EXECUTABLE_PATH, SERF_RPC_ADDR, cometbft_mempool_client),
                name="SerfMonitorThread"
            )
            thread.daemon = True
            thread.start()
            logger.info("Serf monitor thread initiated.")
            serf_monitor_thread_started = True


@app.route('/transaction', methods=['POST'])
def get_transaction():
    try:
        # Extract base64 payload from request JSON
        data = request.get_json()
        if not data or "payload_b64_for_serf_event" not in data:
            return jsonify({"error": "Missing payload_b64_for_serf_event"}), 400

        payload_b64 = data["payload_b64_for_serf_event"]

        # Decode from base64 → UTF-8 string and parse the json
        try:
            decoded_bytes = base64.b64decode(payload_b64)
            decoded_str = decoded_bytes.decode("utf-8")
            transaction_json = json.loads(decoded_str)
            from_node = transaction_json.get("from_node")
            to_node = transaction_json.get("to_node")
            logger.info(f"Generated transaction JSON: {transaction_json}")
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Invalid JSON inside payload: {str(e)}"}), 400
        except Exception as e:
            return jsonify({"error": f"Invalid base64 encoding: {str(e)}"}), 400
        if not from_node or not to_node:
            return jsonify({"error": "Missing required fields: from_node or to_node"}), 400

        event_name = f"transfer-{from_node}-to-{to_node}"
        msg = {"event": event_name, "payload": payload_b64, "timestamp": datetime.now(timezone.utc).isoformat()}
        msg_id = r.xadd(stream_key, msg)

        if msg_id:
            logger.info(f"Base64-encoded payload: {payload_b64}")
            logger.info(f"Successfully dispatched transaction event '{event_name}' to the queue: Msg ID: {msg_id}")
            return jsonify(
                {"status": "success",
                 "message": f"Transaction event '{event_name}' dispatched to the Message Queue.",
                 "msg_id": msg_id}
            ), 200
        else:
            logger.error(f"Failed to dispatch transaction event '{event_name}'.")
            return jsonify(
                {"status": "error", "message": f"Failed to dispatch event:"}), 500
    except Exception as e:
        logger.error(f"Exception while dispatching event: {e}")
        return jsonify({"status": "error", "message": f"Internal server error: {e}"}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
