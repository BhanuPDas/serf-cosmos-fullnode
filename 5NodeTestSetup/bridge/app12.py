import os
import subprocess
import json
import base64
import logging
import threading
import time
import requests
import random
from flask import Flask, jsonify, render_template_string, request, redirect, url_for
from collections import deque
import hashlib

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
SERF_EXECUTABLE_PATH = "/usr/bin/serf"
SERF_RPC_ADDR = "172.20.20.7:7373"
COMETBFT_RPC_URL = "http://localhost:26657"

# --- Flask Application Setup ---
app = Flask(__name__)

# Global Application State for Monitoring
app_metrics = {
    "serf_monitor_status": "Starting...",
    "serf_monitor_last_error": None,
    "serf_rpc_status": "Unknown",
    "cometbft_rpc_status": "Unknown",
    "serf_events_received": 0,
    "cometbft_tx_broadcast": 0,
    "last_cometbft_rpc_check": None,
    "last_serf_rpc_check": None,
    "serf_members": [],
    "cometbft_node_info": {}
}

metrics_lock = threading.Lock()

RECENT_ACTIVITY_MAX_ITEMS = 20
recent_activity_log = []

# This deque now stores the *transaction_hash* (hash of the original JSON string)
processed_monitor_events = deque(maxlen=50)

serf_monitor_thread_started = False
serf_monitor_thread_lock = threading.Lock()

LOCAL_NODE_NAME = os.uname().nodename


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

    def BroadcastTx(self, tx_b64_encoded_str: str, cb: callable) -> None:
        endpoint = f"{self.rpc_url}/broadcast_tx_sync"
        headers = {'Content-Type': 'application/json'}
        payload = {
            "jsonrpc": "2.0",
            "method": "broadcast_tx_sync",
            "params": [tx_b64_encoded_str],
            "id": 1
        }

        with metrics_lock:
            app_metrics["last_cometbft_rpc_check"] = time.time()
            app_metrics["cometbft_rpc_status"] = "Broadcasting..."

        try:
            logger.debug(f"Attempting to broadcast transaction (hash: {tx_b64_encoded_str[:10]}...) to CometBFT RPC: {endpoint}")
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
                logger.info(f"CometBFT RPC broadcast response received: {comet_response.to_dict()}")
                cb(comet_response)

                with metrics_lock:
                    app_metrics["cometbft_tx_broadcast"] += 1
                    app_metrics["cometbft_rpc_status"] = "Broadcasted (Pending Consensus)"
            elif "error" in rpc_result:
                error_details = rpc_result["error"]
                logger.error(f"CometBFT RPC error for broadcast_tx_sync: Code={error_details.get('code')}, Message={error_details.get('message')}, Data={error_details.get('data')}")
                cb(MockResponseCheckTx(code=error_details.get('code', -1), log=f"RPC Error: {error_details.get('message')}"))
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Broadcast Error"
            else:
                logger.error(f"Unexpected CometBFT RPC response format: {rpc_result}")
                cb(MockResponseCheckTx(code=-1, log="Unexpected RPC response format"))
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Unknown Response"

        except requests.exceptions.Timeout:
            logger.error(f"CometBFT RPC broadcast request timed out to {endpoint}")
            cb(MockResponseCheckTx(code=-1, log="CometBFT RPC Broadcast Timeout"))
            with metrics_lock:
                app_metrics["cometbft_rpc_status"] = "Timeout"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Could not connect to CometBFT RPC at {endpoint}: {e}")
            cb(MockResponseCheckTx(code=-1, log=f"CometBFT RPC Connection Error: {e}"))
            with metrics_lock:
                app_metrics["cometbft_rpc_status"] = "Disconnected"
        except Exception as e:
            logger.error(f"An unexpected error occurred during CometBFT RPC broadcast: {e}")
            cb(MockResponseCheckTx(code=-1, log=f"Unexpected Error: {e}"))
            with metrics_lock:
                app_metrics["cometbft_rpc_status"] = "Error"

    def PollTxStatus(self, tx_hash: str, callback: callable, max_attempts: int = 20, interval_sec: int = 1) -> None:
        """
        Polls the CometBFT RPC for a transaction's inclusion in a block by its hash.
        Runs in a separate thread. `callback` receives (success: bool, tx_data: dict, message: str).
        """
        def _poll():
            logger.debug(f"Polling for transaction hash: {tx_hash}...")
            attempts = 0
            while attempts < max_attempts:
                attempts += 1
                try:
                    endpoint = f"{self.rpc_url}/tx?hash=0x{tx_hash}&prove=true"
                    response = requests.get(endpoint, timeout=3)
                    response.raise_for_status()
                    result = response.json()

                    if "result" in result and result["result"] is not None and "tx_result" in result["result"]:
                        tx_result_data = result["result"]
                        abci_response_code = tx_result_data.get("tx_result", {}).get("code", -1)
                        abci_response_log = tx_result_data.get("tx_result", {}).get("log", "")
                        
                        logger.info(f"Tx {tx_hash[:10]}... found in block! Height: {tx_result_data.get('height')}, ABCI Code: {abci_response_code}, Log: '{abci_response_log}'")
                        callback(True, tx_result_data, f"Committed! Code: {abci_response_code}, Log: {abci_response_log[:50]}")
                        return
                    else:
                        logger.debug(f"Tx {tx_hash[:10]}... not yet found (attempt {attempts}/{max_attempts}). Retrying in {interval_sec}s.")

                except requests.exceptions.Timeout:
                    logger.warning(f"Polling for {tx_hash[:10]}... timed out (attempt {attempts}/{max_attempts}).")
                except requests.exceptions.ConnectionError as e:
                    logger.error(f"Polling connection error for {tx_hash[:10]}...: {e}")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error while polling for {tx_hash[:10]}...: {e}")

                time.sleep(interval_sec)

            logger.warning(f"Tx {tx_hash[:10]}... not found after {max_attempts} attempts.")
            callback(False, {}, "Timeout / Not Found after polling")

        polling_thread = threading.Thread(target=_poll, name=f"TxPollThread-{tx_hash[:5]}")
        polling_thread.daemon = True
        polling_thread.start()

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


def get_transaction_hash(transaction_json_string: str) -> str:
    """
    Generates a consistent SHA256 hash for a transaction's JSON string.
    This hash can be used as a unique identifier for reports.
    """
    # Ensure canonical JSON string (e.g., consistent key order) before hashing
    try:
        canonical_json_str = json.dumps(json.loads(transaction_json_string), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical_json_str.encode('utf-8')).hexdigest()
    except Exception as e:
        logger.error(f"Error generating transaction hash from JSON string: {e}. String: {transaction_json_string[:50]}...")
        # Fallback to hashing the raw string if JSON parsing fails
        return hashlib.sha256(transaction_json_string.encode('utf-8')).hexdigest()

def dispatch_serf_report_event(original_event_name: str, original_transaction_hash: str, reporting_node: str, broadcast_status: str, consensus_status: str):
    """
    Dispatches a Serf user event to report the status of a transaction processing.
    This event will be picked up by other monitors (including the local one).
    The payload is now concise to avoid exceeding Serf's size limit.
    """
    report_data = {
        "original_event_name": original_event_name,
        "original_transaction_hash": original_transaction_hash, # Sending hash instead of full payload
        "reporting_node": reporting_node,
        "broadcast_status": broadcast_status,
        "consensus_status": consensus_status,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
    }
    report_payload_json = json.dumps(report_data)
    report_payload_b64 = base64.b64encode(report_payload_json.encode('utf-8')).decode('utf-8')

    report_event_name = f"report-tx-status-{reporting_node}"

    try:
        cmd_args = [
            SERF_EXECUTABLE_PATH,
            "event",
            f"-rpc-addr={SERF_RPC_ADDR}",
            report_event_name,
            report_payload_b64
        ]
        process = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=2)
        
        if process.returncode == 0:
            logger.debug(f"Successfully dispatched Serf report event '{report_event_name}'. Output: {stdout.strip()}")
        else:
            logger.warning(f"Failed to dispatch Serf report event '{report_event_name}'. Error: {stderr.strip()}")
    except subprocess.TimeoutExpired:
        logger.warning(f"Serf report event dispatch timed out for '{report_event_name}'.")
        process.kill()
        stdout, stderr = process.communicate()
    except Exception as e:
        logger.error(f"Exception while dispatching Serf report event: {e}")


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
    MEMBER_CHECK_INTERVAL = 10
    COMETBFT_STATUS_CHECK_INTERVAL = 5

    current_event_info = {}
    parsing_event_info_block = False

    while True:
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
                        app_metrics["serf_rpc_status"] = "Connected"
                        app_metrics["serf_monitor_status"] = "Running"
                        app_metrics["serf_monitor_last_error"] = None
                    logger.debug(f"Updated Serf members: {len(app_metrics['serf_members'])} members found.")
                else:
                    logger.error(f"Failed to get Serf members: {members_process.stderr.strip()}")
                    with metrics_lock:
                        app_metrics["serf_rpc_status"] = "Disconnected"
                        app_metrics["serf_monitor_status"] = "Failed to get Serf members"
                        app_metrics["serf_monitor_last_error"] = members_process.stderr.strip() or "Error fetching members"
                last_members_check_time = current_time
            except Exception as e:
                logger.error(f"Error fetching Serf members: {e}")
                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Error"
                    app_metrics["serf_monitor_last_error"] = f"Members Fetch Error: {e}"
                last_members_check_time = current_time

        # --- Periodic CometBFT RPC Status Check ---
        if current_time - last_cometbft_status_check_time > COMETBFT_STATUS_CHECK_INTERVAL:
            try:
                comet_status_endpoint = f"{mempool_client.rpc_url}/status"
                comet_response = requests.get(comet_status_endpoint, timeout=3)
                comet_response.raise_for_status()
                comet_status_data = comet_response.json()
                with metrics_lock:
                    if not app_metrics["cometbft_rpc_status"].startswith(("Broadcasting", "Polling")):
                        app_metrics["cometbft_rpc_status"] = "Connected"
                    app_metrics["cometbft_node_info"] = comet_status_data.get("result", {}).get("node_info", {})
                logger.debug(f"CometBFT RPC status check successful. Node: {app_metrics['cometbft_node_info'].get('moniker')}")
            except requests.exceptions.ConnectionError as e:
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Disconnected"
                    app_metrics["cometbft_node_info"] = {}
                logger.warning(f"CometBFT RPC: Connection error - {e}.")
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
            cmd_args = [serf_exec_path, "monitor", f"-rpc-addr={rpc_addr}"]
            process = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

            logger.info("Serf monitor command launched. Listening for ALL events (plain text format)...")
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue

                logger.debug(f"Received raw Serf monitor line: {line}")

                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Connected"

                if not line.startswith(("Name:", "Payload:", "Event Info:", "Coalesce:", "Event:", "LTime:", "Received event: user-event:")):
                    parsing_event_info_block = False
                    current_event_info = {}
                    if "[INFO] agent:" in line or "[INFO] serf:" in line:
                        logger.debug(f"Serf Agent/Internal Log: {line}")
                    else:
                        logger.debug(f"Other Serf monitor line (ignored for processing): {line}")
                    continue

                if line.startswith("Name:"):
                    parsing_event_info_block = True
                    current_event_info = {"type": "Serf User Event", "name": line.split("Name:", 1)[1].strip().strip('"')}
                elif line.startswith("Payload:") and parsing_event_info_block:
                    if "name" in current_event_info:
                        try:
                            hex_bytes_str = line.split("Payload: []byte{", 1)[1].strip("}").replace("0x", "").replace(", ", "")
                            payload_bytes = bytes.fromhex(hex_bytes_str)
                            payload_b64 = base64.b64encode(payload_bytes).decode('utf-8')
                            current_event_info["payload_full"] = payload_b64
                        except Exception as e:
                            logger.error(f"Error converting hex payload to base64: {e}. Line: {line}")
                            current_event_info["payload_full"] = "Parsing Error"
                            parsing_event_info_block = False
                            current_event_info = {}
                            continue

                        if current_event_info["payload_full"] != "Parsing Error":
                            event_name_to_process = current_event_info["name"]
                            payload_b64_to_process = current_event_info["payload_full"]
                            
                            # Calculate the consistent hash of the original payload for reports
                            # This needs to be done carefully to avoid parsing errors
                            original_transaction_json_str = ""
                            try:
                                original_transaction_json_str = base64.b64decode(payload_b64_to_process).decode('utf-8')
                            except Exception as e:
                                logger.error(f"Could not decode payload_b64 to JSON string for hashing: {e}")
                                # If Base64 decode fails, we can't get the original JSON string.
                                # Fallback to hashing the raw Base64 string itself for uniqueness, but warn.
                                original_transaction_json_str = payload_b64_to_process
                            
                            transaction_hash_for_report = get_transaction_hash(original_transaction_json_str)


                            # --- Handle Report Events ---
                            if event_name_to_process.startswith("report-tx-status-"):
                                try:
                                    report_data = json.loads(base64.b64decode(payload_b64_to_process).decode('utf-8'))
                                    with metrics_lock:
                                        found_original_event = False
                                        original_transaction_hash_from_report = report_data.get("original_transaction_hash")
                                        for entry in recent_activity_log:
                                            # Match by original_event_name and original_transaction_hash
                                            if entry.get("type") in ["Serf User Event", "Serf User Event (Single Line)"] and \
                                               entry.get("name") == report_data["original_event_name"] and \
                                               entry.get("transaction_hash") == original_transaction_hash_from_report: # Match using the stored hash
                                                
                                                entry["cometbft_broadcast_response"] = report_data["broadcast_status"]
                                                entry["cometbft_consensus_status"] = report_data["consensus_status"]
                                                entry["reported_by_node"] = report_data["reporting_node"]
                                                entry["report_timestamp"] = report_data["timestamp"]
                                                entry["type"] = "Serf User Event (Reported)"
                                                found_original_event = True
                                                break
                                        if not found_original_event:
                                            new_report_entry = {
                                                "timestamp": report_data["timestamp"],
                                                "type": "Serf Report",
                                                "name": f"Report from {report_data['reporting_node']} for {report_data['original_event_name']}",
                                                "payload_full": "Original payload not available (hash: " + original_transaction_hash_from_report[:10] + "...) ",
                                                "payload_preview": "Original payload not available (hash: " + original_transaction_hash_from_report[:10] + "...) ",
                                                "cometbft_broadcast_response": report_data["broadcast_status"],
                                                "cometbft_consensus_status": report_data["consensus_status"],
                                                "reported_by_node": report_data["reporting_node"]
                                            }
                                            recent_activity_log.insert(0, new_report_entry)
                                            if len(recent_activity_log) > RECENT_ACTIVITY_MAX_ITEMS:
                                                recent_activity_log.pop()
                                    logger.info(f"Processed Serf report from {report_data['reporting_node']} for event '{report_data['original_event_name']}'.")
                                except Exception as e:
                                    logger.error(f"Error parsing Serf report event payload: {e}. Payload: {payload_b64_to_process}")
                                parsing_event_info_block = False
                                current_event_info = {}
                                continue

                            # --- Handle Regular Transaction Events (only if not a report event) ---
                            if transaction_hash_for_report in processed_monitor_events: # Use the actual hash for duplicate check
                                logger.debug(f"Skipping duplicate event (already processed by monitor): {event_name_to_process}")
                                parsing_event_info_block = False
                                current_event_info = {}
                                continue
                            processed_monitor_events.append(transaction_hash_for_report) # Store the hash

                            logger.info(f"Parsed Serf user event (multi-line): Name='{event_name_to_process}', Payload(base64)='{payload_b64_to_process[:30]}...'")

                            with metrics_lock:
                                app_metrics["serf_events_received"] += 1
                                activity_entry = {
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                                    "type": "Serf User Event",
                                    "name": event_name_to_process,
                                    "payload_full": payload_b64_to_process,
                                    "payload_preview": payload_b64_to_process[:50] + ("..." if len(payload_b64_to_process) > 50 else ""),
                                    "cometbft_broadcast_response": "Pending...",
                                    "cometbft_consensus_status": "Waiting for broadcast...",
                                    "processed_by_node": LOCAL_NODE_NAME,
                                    "transaction_hash": transaction_hash_for_report # Store the transaction hash
                                }
                                recent_activity_log.insert(0, activity_entry)
                                if len(recent_activity_log) > RECENT_ACTIVITY_MAX_ITEMS:
                                    recent_activity_log.pop()
                            
                            def broadcast_response_callback(response: MockResponseCheckTx, activity_entry=activity_entry, event_name_for_log=event_name_to_process, original_transaction_hash_for_report=transaction_hash_for_report):
                                with metrics_lock:
                                    broadcast_status_str = f"Code: {response.code}, Log: {response.log[:50]}..."
                                    activity_entry["cometbft_broadcast_response"] = broadcast_status_str
                                    if response.code == 0 and response.hash:
                                        logger.info(
                                            f"CometBFT RPC Broadcast Success for event '{event_name_for_log}': "
                                            f"Code={response.code}, Log='{response.log}', Hash={response.hash}"
                                        )
                                        activity_entry["cometbft_consensus_status"] = "Polling for commitment..."
                                        mempool_client.PollTxStatus(response.hash,
                                            lambda success, tx_data, msg: update_consensus_status(activity_entry, success, tx_data, msg, event_name_for_log, original_transaction_hash_for_report, broadcast_status_str))
                                    else:
                                        logger.error(
                                            f"CometBFT RPC Broadcast Failed for event '{event_name_for_log}': "
                                            f"Code={response.code}, Log='{response.log}'"
                                        )
                                        consensus_status_str = f"Broadcast Failed (Code: {response.code}) Log: {response.log[:50]}..."
                                        activity_entry["cometbft_consensus_status"] = consensus_status_str
                                        threading.Thread(target=dispatch_serf_report_event, args=(event_name_for_log, original_transaction_hash_for_report, LOCAL_NODE_NAME, broadcast_status_str, consensus_status_str)).start()

                            def update_consensus_status(activity_entry, success, tx_data, msg, event_name_for_log, original_transaction_hash_for_report, broadcast_status_str):
                                with metrics_lock:
                                    consensus_status_str = ""
                                    if success:
                                        abci_code = tx_data.get('tx_result', {}).get('code', -1)
                                        abci_log = tx_data.get('tx_result', {}).get('log', '')
                                        consensus_status_str = f"Committed! Height: {tx_data.get('height')}, Code: {abci_code}, Log: {abci_log[:50]}"
                                    else:
                                        consensus_status_str = msg
                                    activity_entry["cometbft_consensus_status"] = consensus_status_str
                                    threading.Thread(target=dispatch_serf_report_event, args=(event_name_for_log, original_transaction_hash_for_report, LOCAL_NODE_NAME, broadcast_status_str, consensus_status_str)).start()

                            try:
                                mempool_client.BroadcastTx(payload_b64_to_process, broadcast_response_callback)
                            except Exception as e:
                                logger.error(f"Error calling CometBFTMempoolClient.BroadcastTx for '{event_name_to_process}': {e}")
                                with metrics_lock:
                                    activity_entry["cometbft_broadcast_response"] = f"CometBFT RPC Call Error: {e}"
                                    activity_entry["cometbft_consensus_status"] = f"CometBFT RPC Call Error: {e}"
                                threading.Thread(target=dispatch_serf_report_event, args=(event_name_to_process, transaction_hash_for_report, LOCAL_NODE_NAME, f"RPC Call Error: {e}", f"RPC Call Error: {e}")).start()

                        parsing_event_info_block = False
                        current_event_info = {}
                    else:
                        logger.debug(f"Incomplete multi-line event info block (missing Name after Payload or vice-versa): {line}")
                        if not line.startswith("Event Info:") and not line.startswith("Coalesce:") and not line.startswith("Event:") and not line.startswith("LTime:"):
                            parsing_event_info_block = False
                            current_event_info = {}

                elif "Received event: user-event:" in line and not parsing_event_info_block:
                    try:
                        event_part = line.split("Received event: user-event:", 1)[1].strip()
                        parts = event_part.split(" ", 1)
                        if len(parts) == 2:
                            event_name_to_process, payload_b64_to_process = parts[0], parts[1]
                            
                            # Calculate the consistent hash of the original payload for reports
                            original_transaction_json_str = ""
                            try:
                                original_transaction_json_str = base64.b64decode(payload_b64_to_process).decode('utf-8')
                            except Exception as e:
                                logger.error(f"Could not decode payload_b64 to JSON string for hashing: {e}")
                                original_transaction_json_str = payload_b64_to_process # Fallback
                            
                            transaction_hash_for_report = get_transaction_hash(original_transaction_json_str)

                            # --- Handle Report Events (for single-line format) ---
                            if event_name_to_process.startswith("report-tx-status-"):
                                try:
                                    report_data = json.loads(base64.b64decode(payload_b64_to_process).decode('utf-8'))
                                    with metrics_lock:
                                        found_original_event = False
                                        original_transaction_hash_from_report = report_data.get("original_transaction_hash")
                                        for entry in recent_activity_log:
                                            if entry.get("type") in ["Serf User Event", "Serf User Event (Single Line)"] and \
                                               entry.get("name") == report_data["original_event_name"] and \
                                               entry.get("transaction_hash") == original_transaction_hash_from_report:
                                                entry["cometbft_broadcast_response"] = report_data["broadcast_status"]
                                                entry["cometbft_consensus_status"] = report_data["consensus_status"]
                                                entry["reported_by_node"] = report_data["reporting_node"]
                                                entry["report_timestamp"] = report_data["timestamp"]
                                                entry["type"] = "Serf User Event (Reported)"
                                                found_original_event = True
                                                break
                                        if not found_original_event:
                                            new_report_entry = {
                                                "timestamp": report_data["timestamp"],
                                                "type": "Serf Report",
                                                "name": f"Report from {report_data['reporting_node']} for {report_data['original_event_name']}",
                                                "payload_full": "Original payload not available (hash: " + original_transaction_hash_from_report[:10] + "...) ",
                                                "payload_preview": "Original payload not available (hash: " + original_transaction_hash_from_report[:10] + "...) ",
                                                "cometbft_broadcast_response": report_data["broadcast_status"],
                                                "cometbft_consensus_status": report_data["consensus_status"],
                                                "reported_by_node": report_data["reporting_node"]
                                            }
                                            recent_activity_log.insert(0, new_report_entry)
                                            if len(recent_activity_log) > RECENT_ACTIVITY_MAX_ITEMS:
                                                recent_activity_log.pop()
                                    logger.info(f"Processed Serf report from {report_data['reporting_node']} for event '{report_data['original_event_name']}'.")
                                except Exception as e:
                                    logger.error(f"Error parsing single-line Serf report event payload: {e}. Payload: {payload_b64_to_process}")
                                parsing_event_info_block = False
                                current_event_info = {}
                                continue

                            # --- Handle Regular Transaction Events (single-line, not a report) ---
                            if transaction_hash_for_report in processed_monitor_events: # Check for duplicates
                                logger.debug(f"Skipping duplicate event (already processed by monitor): {event_name_to_process}")
                                continue
                            processed_monitor_events.append(transaction_hash_for_report) # Store the hash

                            logger.info(f"Parsed Serf user event (single line): Name='{event_name_to_process}', Payload(base64)='{payload_b64_to_process[:30]}...'")
                            
                            with metrics_lock:
                                app_metrics["serf_events_received"] += 1
                                activity_entry = {
                                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                                    "type": "Serf User Event (Single Line)",
                                    "name": event_name_to_process,
                                    "payload_full": payload_b64_to_process,
                                    "payload_preview": payload_b64_to_process[:50] + ("..." if len(payload_b64_to_process) > 50 else ""),
                                    "cometbft_broadcast_response": "Pending...",
                                    "cometbft_consensus_status": "Waiting for broadcast...",
                                    "processed_by_node": LOCAL_NODE_NAME,
                                    "transaction_hash": transaction_hash_for_report
                                }
                                recent_activity_log.insert(0, activity_entry)
                                if len(recent_activity_log) > RECENT_ACTIVITY_MAX_ITEMS:
                                    recent_activity_log.pop()
                            
                            def broadcast_response_callback(response: MockResponseCheckTx, activity_entry=activity_entry, event_name_for_log=event_name_to_process, original_transaction_hash_for_report=transaction_hash_for_report):
                                with metrics_lock:
                                    broadcast_status_str = f"Code: {response.code}, Log: {response.log[:50]}..."
                                    activity_entry["cometbft_broadcast_response"] = broadcast_status_str
                                    if response.code == 0 and response.hash:
                                        logger.info(
                                            f"CometBFT RPC Broadcast Success for event '{event_name_for_log}': "
                                            f"Code={response.code}, Log='{response.log}', Hash={response.hash}"
                                        )
                                        activity_entry["cometbft_consensus_status"] = "Polling for commitment..."
                                        mempool_client.PollTxStatus(response.hash,
                                            lambda success, tx_data, msg: update_consensus_status(activity_entry, success, tx_data, msg, event_name_for_log, original_transaction_hash_for_report, broadcast_status_str))
                                    else:
                                        logger.error(
                                            f"CometBFT RPC Broadcast Failed for event '{event_name_for_log}': "
                                            f"Code={response.code}, Log='{response.log}'"
                                        )
                                        consensus_status_str = f"Broadcast Failed (Code: {response.code}) Log: {response.log[:50]}..."
                                        activity_entry["cometbft_consensus_status"] = consensus_status_str
                                        threading.Thread(target=dispatch_serf_report_event, args=(event_name_for_log, original_transaction_hash_for_report, LOCAL_NODE_NAME, broadcast_status_str, consensus_status_str)).start()

                            def update_consensus_status(activity_entry, success, tx_data, msg, event_name_for_log, original_transaction_hash_for_report, broadcast_status_str):
                                with metrics_lock:
                                    consensus_status_str = ""
                                    if success:
                                        abci_code = tx_data.get('tx_result', {}).get('code', -1)
                                        abci_log = tx_data.get('tx_result', {}).get('log', '')
                                        consensus_status_str = f"Committed! Height: {tx_data.get('height')}, Code: {abci_code}, Log: {abci_log[:50]}"
                                    else:
                                        consensus_status_str = msg
                                    activity_entry["cometbft_consensus_status"] = consensus_status_str
                                    threading.Thread(target=dispatch_serf_report_event, args=(event_name_for_log, original_transaction_hash_for_report, LOCAL_NODE_NAME, broadcast_status_str, consensus_status_str)).start()

                            try:
                                mempool_client.BroadcastTx(payload_b64_to_process, broadcast_response_callback)
                            except Exception as e:
                                logger.error(f"Error calling CometBFTMempoolClient.BroadcastTx for '{event_name_to_process}': {e}")
                                with metrics_lock:
                                    activity_entry["cometbft_broadcast_response"] = f"CometBFT RPC Call Error: {e}"
                                    activity_entry["cometbft_consensus_status"] = f"CometBFT RPC Call Error: {e}"
                                threading.Thread(target=dispatch_serf_report_event, args=(event_name_to_process, transaction_hash_for_report, LOCAL_NODE_NAME, f"RPC Call Error: {e}", f"RPC Call Error: {e}")).start()

                            parsing_event_info_block = False
                            current_event_info = {}
                        else:
                            logger.debug(f"Could not parse single-line user event name/payload format: {line}")
                    except Exception as e:
                        logger.error(f"Error parsing single-line user event: {e}. Line: {line}")
                        parsing_event_info_block = False
                        current_event_info = {}

            stderr_output = process.stderr.read()
            if stderr_output:
                logger.error(f"Serf monitor stderr: {stderr_output}")
                with metrics_lock:
                    app_metrics["serf_monitor_last_error"] = f"Serf CLI Error: {stderr_output.strip()}"

            process.wait()
            if process.returncode != 0:
                logger.error(f"Serf monitor command exited with error: {process.returncode}")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "Exited with Error"
                    app_metrics["serf_monitor_last_error"] = f"CLI Exit Code: {process.returncode}"
            else:
                logger.info("Serf monitor command exited gracefully.")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "Exited Gracefully"

        except FileNotFoundError:
            logger.critical(f"Serf executable not found: {serf_exec_path}")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = "CRITICAL: Executable Missing"
                app_metrics["serf_monitor_last_error"] = f"Missing: {serf_exec_path}"
            time.sleep(10)

        except Exception as e:
            logger.critical(f"Failed to start/monitor Serf: {e}")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = "Initialization Error"
                app_metrics["serf_monitor_last_error"] = f"Startup Error: {e}"
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
                logger.critical(
                    f"Serf executable not found or not executable at '{SERF_EXECUTABLE_PATH}'. Please check configuration.")
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


@app.route('/trigger_random_transaction', methods=['POST'])
def trigger_random_transaction():
    """
    Triggers a conceptual 'transaction' between two random Serf nodes by
    dispatching a Serf user event from the Flask application itself.
    """
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
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
    }
    transaction_payload_json = json.dumps(transaction_data)
    transaction_payload_b64 = base64.b64encode(transaction_payload_json.encode('utf-8')).decode('utf-8')

    event_name = f"transfer-{sender_node['name']}-to-{receiver_node['name']}"

    try:
        cmd_args = [
            SERF_EXECUTABLE_PATH,
            "event",
            f"-rpc-addr={SERF_RPC_ADDR}",
            event_name,
            transaction_payload_b64
        ]
        result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=5)

        if result.returncode == 0:
            logger.debug(f"Generated transaction JSON: {transaction_payload_json}")
            logger.debug(f"Base64-encoded payload: {transaction_payload_b64}")
            logger.info(f"Successfully dispatched Serf event '{event_name}' via RPC. Output: {result.stdout.strip()}")
            return jsonify({"status": "success", "message": f"Transaction event '{event_name}' dispatched.",
                            "payload": transaction_payload_json}), 200
        else:
            logger.error(f"Failed to dispatch Serf event '{event_name}'. Error: {result.stderr.strip()}")
            return jsonify(
                {"status": "error", "message": f"Failed to dispatch Serf event: {result.stderr.strip()}"}), 500
    except Exception as e:
        logger.error(f"Exception while dispatching Serf event: {e}")
        return jsonify({"status": "error", "message": f"Internal server error: {e}"}), 500


@app.route('/')
def index():
    """Renders the main dashboard page with live monitoring data."""
    with metrics_lock:
        current_metrics = app_metrics.copy()
        current_activity_log = recent_activity_log[:]

    serf_status_color = "bg-gray-700"
    if current_metrics["serf_rpc_status"] == "Connected":
        serf_status_color = "bg-green-500"
    elif "Error" in current_metrics["serf_rpc_status"] or "Disconnected" in current_metrics["serf_rpc_status"] or "CRITICAL" in current_metrics["serf_monitor_status"]:
        serf_status_color = "bg-red-500"

    comet_status_color = "bg-gray-700"
    if current_metrics["cometbft_rpc_status"] == "Connected":
        comet_status_color = "bg-green-500"
    elif "Error" in current_metrics["cometbft_rpc_status"] or "Disconnected" in current_metrics["cometbft_rpc_status"] or "Timeout" in current_metrics["cometbft_rpc_status"]:
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
                    background-color: #f8f9fa; /* Office light background */
                    color: #212529; /* Dark text for contrast */
                }
                .card {
                    background-color: #ffffff; /* White card background */
                    border: 1px solid #dee2e6; /* Light grey border */
                    box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075); /* Subtle shadow */
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
                .bg-yellow-500 { background-color: #ffc107; } /* Warning yellow for pending */
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
                .bg-green-100 { background-color: #d4edda; }
                .text-green-700 { color: #155724; }
                .bg-green-200 { background-color: #c3e6cb; }
                .text-green-800 { color: #0f5132; }

                .text-red-600 { color: #c82333; } /* Darker red */
                .bg-red-100 { background-color: #f8d7da; }
                .text-red-700 { color: #721c24; }

                .bg-yellow-200 { background-color: #ffeeba; }
                .text-yellow-800 { color: #664d03; }

                .bg-blue-100 { background-color: #cfe2ff; }
                .text-blue-700 { color: #052c65; }

                .text-gray-600 { color: #6c757d; } /* Standard gray */

                /* Scrollbars for activity log */
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
                }

                .payload-text {
                    white-space: pre-wrap;
                    word-break: break-all;
                }

                /* Button Styling */
                .bg-teal-600 { background-color: #17a2b8; } /* Teal for main button */
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
                    <!-- Serf Monitoring Card -->
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

                    <!-- CometBFT Status Card -->
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

                    <!-- Serf Cluster Members Card -->
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

                <!-- Recent Activity Log Section -->
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
        "mempool_integration": "real_rpc_with_consensus_check",
        "metrics": current_metrics,
        "recent_activity_log": current_activity_log
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
