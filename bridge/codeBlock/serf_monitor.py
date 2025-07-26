import json
import base64
import subprocess
import threading
import time
import requests
import logging
from datetime import datetime
import os
from collections import deque
import hashlib

logger = logging.getLogger(__name__)

# Global shared data & locks (define elsewhere in your app as needed)
metrics_lock = threading.Lock()
app_metrics = {
    "serf_members": [],
    "serf_rpc_status": "Disconnected",
    "serf_monitor_status": "Stopped",
    "serf_monitor_last_error": None,
    "cometbft_rpc_status": "Disconnected",
    "cometbft_node_info": {},
    "serf_events_received": 0
}
recent_activity_log = []
RECENT_ACTIVITY_MAX_ITEMS = 100
processed_monitor_events = deque(maxlen=50)

LOCAL_NODE_NAME = os.uname().nodename  # Your node name, set properly
SERF_EXECUTABLE_PATH = "/usr/bin/serf"  # Change to your serf path
SERF_RPC_ADDR = "172.20.20.7:7373"  # Your serf RPC addr
default_p2p_port = 26656  # Default CometBFT P2P port


def get_transaction_hash(transaction_content_string: str) -> str:
    try:
        parsed_json = json.loads(transaction_content_string)
        canonical_json_str = json.dumps(parsed_json, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical_json_str.encode('utf-8')).hexdigest()
    except json.JSONDecodeError:
        logger.debug(f"Input not JSON, hashing raw string: {transaction_content_string[:30]}...")
        return hashlib.sha256(transaction_content_string.encode('utf-8')).hexdigest()
    except Exception as e:
        logger.error(f"Unexpected error in get_transaction_hash: {e}. Preview: {transaction_content_string[:50]}...")
        return hashlib.sha256(transaction_content_string.encode('utf-8')).hexdigest()


def dispatch_serf_report_event(original_event_name: str, original_transaction_hash: str,
                               reporting_node: str, broadcast_status: str, consensus_status: str):
    report_data = {
        "original_event_name": original_event_name,
        "original_transaction_hash": original_transaction_hash,
        "reporting_node": reporting_node,
        "broadcast_status": broadcast_status,
        "consensus_status": consensus_status,
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
            logger.debug(f"Dispatched Serf report event '{report_event_name}': {stdout.strip()}")
        else:
            logger.warning(f"Failed to dispatch Serf report event '{report_event_name}': {stderr.strip()}")
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout dispatching Serf report event '{report_event_name}'. Killing process...")
        process.kill()
        stdout, stderr = process.communicate()
    except Exception as e:
        logger.error(f"Exception dispatching Serf report event: {e}")


def process_serf_user_event(event_name: str, payload_b64: str, mempool_client):
    """
    Decode payload, check duplicates, broadcast tx, update metrics and logs,
    dispatch report events about tx status.
    """
    try:
        decoded_payload = base64.b64decode(payload_b64).decode('utf-8')
        logger.info(f"Decoded Payload: {decoded_payload}")
        parsed_payload = json.loads(decoded_payload)

        # Always recompute the hash based on what you're sending
        kv_tx_string = json.dumps(parsed_payload)
        tx_hash = get_transaction_hash(kv_tx_string)

        if tx_hash in processed_monitor_events:
            logger.debug(f"Duplicate event detected, skipping tx_hash: {tx_hash}")
            return
        processed_monitor_events.append(tx_hash)

        with metrics_lock:
            app_metrics["serf_events_received"] += 1
            activity_entry = {
                "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "type": "Serf User Event",
                "name": event_name,
                "payload_full": payload_b64,
                "payload_preview": payload_b64[:50] + ("..." if len(payload_b64) > 50 else ""),
                "cometbft_broadcast_response": "Pending...",
                "cometbft_consensus_status": "Waiting for broadcast...",
                "processed_by_node": LOCAL_NODE_NAME,
                "transaction_hash": tx_hash
            }
            recent_activity_log.insert(0, activity_entry)
            if len(recent_activity_log) > RECENT_ACTIVITY_MAX_ITEMS:
                recent_activity_log.pop()

        logger.info(f"Processing Serf user event: {event_name} with tx_hash {tx_hash}")
        kv_tx_b64 = base64.b64encode(kv_tx_string.encode('utf-8')).decode('utf-8')

        # === Define nested helpers so they can access variables ===
        def update_consensus_status(activity, success, tx_data, msg):
            with metrics_lock:
                if success:
                    tx_result = tx_data.get('tx_result', {})
                    code = int(tx_result.get('code', -1))  # Ensure code is an int
                    log_msg = tx_result.get('log', '') or ""

                    # Optional: attempt to parse the log if it's a JSON string
                    try:
                        parsed_log = json.loads(log_msg)
                        log_summary = str(parsed_log)[:50]
                    except Exception:
                        log_summary = log_msg[:50]

                    consensus_str = (
                        f"Committed! Height: {tx_data.get('height')}, Code: {code}, Log: {log_summary}"
                    )
                else:
                    consensus_str = msg

                activity["cometbft_consensus_status"] = consensus_str

                threading.Thread(
                    target=dispatch_serf_report_event,
                    args=(
                        event_name,
                        tx_hash,
                        LOCAL_NODE_NAME,
                        activity.get("cometbft_broadcast_response", "N/A"),
                        consensus_str,
                    )
                ).start()

        def broadcast_response_callback(response):
            with metrics_lock:
                if not response:
                    logger.error("Broadcast failed: No response returned.")
                    activity_entry["cometbft_broadcast_response"] = "Broadcast failed: No response"
                    activity_entry["cometbft_consensus_status"] = "Broadcast failed"
                    return

                result = response.get("result", {})
                if not result:
                    logger.error("Broadcast failed: Missing result in response.")
                    activity_entry["cometbft_broadcast_response"] = "Broadcast failed: Missing result"
                    activity_entry["cometbft_consensus_status"] = "Broadcast failed"
                    return

                code = int(result.get("code", -1))
                log = result.get("log", "") or ""
                broadcast_tx_hash = result.get("hash", "")

                broadcast_status = f"Code: {code}, Log: {log[:50]}..."
                activity_entry["cometbft_broadcast_response"] = broadcast_status

                if code == 0 and broadcast_tx_hash:
                    logger.info(f"Broadcast success for '{event_name}' Code={code} Hash={broadcast_tx_hash}")
                    activity_entry["cometbft_consensus_status"] = "Polling for commitment..."

                    mempool_client.poll_tx_status(
                        broadcast_tx_hash,
                        lambda success, tx_data, msg: update_consensus_status(activity_entry, success, tx_data, msg)
                    )
                    logger.info(f"Polling results for consensus: {activity_entry}")
                else:
                    logger.error(f"Broadcast error for '{event_name}': {broadcast_status}")
                    consensus_str = f"Broadcast Failed (Code: {code}) Log: {log[:50]}..."
                    activity_entry["cometbft_consensus_status"] = consensus_str

                    threading.Thread(
                        target=dispatch_serf_report_event,
                        args=(event_name, tx_hash, LOCAL_NODE_NAME, broadcast_status, consensus_str)
                    ).start()

        # === Broadcast Transaction ===
        try:
            logger.info(f"Preparing payload for the broadcast: {kv_tx_b64}")
            broadcast_response = mempool_client.broadcast_tx_sync(kv_tx_b64)
            broadcast_response_callback(broadcast_response)
        except Exception as e:
            logger.exception(f"Unexpected error during broadcast: {e}")
            activity_entry["cometbft_broadcast_response"] = f"Broadcast Exception: {str(e)}"
            activity_entry["cometbft_consensus_status"] = "Broadcast failed due to exception"
            threading.Thread(target=dispatch_serf_report_event,
                             args=(event_name, tx_hash, LOCAL_NODE_NAME, f"RPC call error: {e}",
                                   f"RPC call error: {e}")).start()

        # === Dial Peers if any ===
        try:
            peers_to_dial = []
            for member in app_metrics.get("serf_members", []):
                tags = member.get("tags", {})
                peer = tags.get("cometbft_node_id")
                if peer:
                    peers_to_dial.append(peer)
            if peers_to_dial:
                logger.info(f"Dialing peers from serf_members: {peers_to_dial}")
                mempool_client.dial_peers(peers_to_dial)
            else:
                logger.info("No valid cometbft_node_id found in serf_members to dial.")

        except Exception as e:
            logger.error(f"Error while collecting peers to dial: {e}")

    except Exception as e:
        logger.error(f"Error processing serf user event '{event_name}': {e}")


def serf_monitor_thread(serf_exec_path: str, rpc_addr: str, mempool_client):
    logger.info(f"Starting Serf monitor thread. Connecting to RPC {rpc_addr}")

    last_members_check_time = 0
    last_cometbft_status_check_time = 0
    MEMBER_CHECK_INTERVAL = 10
    COMETBFT_STATUS_CHECK_INTERVAL = 5

    current_event_info = {}
    parsing_event_info_block = False

    while True:
        current_time = time.time()

        # Periodically update Serf members with enriched tags
        if current_time - last_members_check_time > MEMBER_CHECK_INTERVAL:
            try:
                members_cmd = [serf_exec_path, "members", "-format=json", f"-rpc-addr={rpc_addr}"]
                members_process = subprocess.run(members_cmd, capture_output=True, text=True, timeout=5)
                if members_process.returncode == 0:
                    members_data = json.loads(members_process.stdout)
                    enriched_members = []
                    for member in members_data.get("members", []):
                        name = member.get("name")
                        raw_addr = member.get("addr", "")  # Example: "10.0.1.11:7946"
                        ip = raw_addr.split(":")[0]
                        node_id = f"{name}@{ip}:{default_p2p_port}"
                        tags = member.get("tags", {})
                        tags["cometbft_node_id"] = node_id
                        tags["p2p_port"] = default_p2p_port
                        member["tags"] = tags
                        enriched_members.append(member)
                    with metrics_lock:
                        app_metrics["serf_members"] = enriched_members
                        app_metrics["serf_rpc_status"] = "Connected"
                        app_metrics["serf_monitor_status"] = "Running"
                        app_metrics["serf_monitor_last_error"] = None
                    logger.debug(f"Updated Serf members: {len(enriched_members)} found")
                else:
                    logger.error(f"Failed to get Serf members: {members_process.stderr.strip()}")
                    with metrics_lock:
                        app_metrics["serf_rpc_status"] = "Disconnected"
                        app_metrics["serf_monitor_status"] = "Failed to get members"
                        app_metrics["serf_monitor_last_error"] = members_process.stderr.strip() or "Unknown error"
                last_members_check_time = current_time
            except Exception as e:
                logger.error(f"Error fetching Serf members: {e}")
                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Error"
                    app_metrics["serf_monitor_last_error"] = f"Members fetch error: {e}"
                last_members_check_time = current_time

        # Periodically check CometBFT RPC status
        if current_time - last_cometbft_status_check_time > COMETBFT_STATUS_CHECK_INTERVAL:
            try:
                comet_status_endpoint = f"{mempool_client.base_url}/status"
                comet_response = requests.get(comet_status_endpoint, timeout=3)
                comet_response.raise_for_status()
                comet_status_data = comet_response.json()
                with metrics_lock:
                    if not app_metrics["cometbft_rpc_status"].startswith(("Broadcasting", "Polling")):
                        app_metrics["cometbft_rpc_status"] = "Connected"
                    app_metrics["cometbft_node_info"] = comet_status_data.get("result", {}).get("node_info", {})
                logger.debug(f"CometBFT RPC status OK. Node: {app_metrics['cometbft_node_info'].get('moniker')}")
            except requests.exceptions.ConnectionError as e:
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Disconnected"
                    app_metrics["cometbft_node_info"] = {}
                logger.warning(f"CometBFT RPC connection error: {e}")
            except requests.exceptions.Timeout:
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Timeout"
                    app_metrics["cometbft_node_info"] = {}
                logger.warning("CometBFT RPC request timed out")
            except Exception as e:
                with metrics_lock:
                    app_metrics["cometbft_rpc_status"] = "Error"
                    app_metrics["cometbft_node_info"] = {}
                logger.error(f"CometBFT RPC status check failed: {e}")
            finally:
                last_cometbft_status_check_time = current_time

        # Launch serf monitor process to receive events
        try:
            cmd_args = [serf_exec_path, "monitor", f"-rpc-addr={rpc_addr}"]
            process = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            logger.info("Serf monitor command launched, listening for events...")

            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue

                logger.info(f"Serf monitor line: {line}")

                with metrics_lock:
                    app_metrics["serf_rpc_status"] = "Connected"

                # Detect multi-line Event Info block
                if line.startswith("Event Info:"):
                    parsing_event_info_block = True
                    current_event_info = {}
                    continue

                # Parse multi-line event info block
                if parsing_event_info_block:
                    if line.startswith("Name:"):
                        current_event_info["name"] = line.split("Name:", 1)[1].strip().strip('"')
                    if line.startswith("Event"):
                        serf_event = line.split("Event:", 1)[1].strip().strip('"')
                        current_event_info["event"] = f"{serf_event}-event"
                    elif line.startswith("Payload:"):
                        try:
                            # Payload line format: Payload: []byte{0xXX, 0xXX, ...}
                            hex_bytes_str = line.split("Payload: []byte{", 1)[1].strip("}").replace("0x", "").replace(
                                ", ", "")
                            payload_bytes = bytes.fromhex(hex_bytes_str)
                            current_event_info["payload_full"] = payload_bytes.decode('utf-8')
                            payload_serf = current_event_info["payload_full"]
                            logger.info(f"Received payload from serf: {payload_serf}")
                        except Exception as e:
                            logger.error(f"Failed to parse Serf payload hex bytes: {e}")
                            parsing_event_info_block = False
                            current_event_info = {}
                            continue

                    # Once name & payload are collected, process event
                    if "name" in current_event_info and current_event_info["name"].startswith(
                            "transfer") and "payload_full" in current_event_info:
                        process_serf_user_event(current_event_info["name"], current_event_info["payload_full"],
                                                mempool_client)
                        parsing_event_info_block = False
                        current_event_info = {}
                    continue
                logger.info(f"Ignoring payload for non-transfer event: {current_event_info.get('name', 'Unknown')}")

            stderr_output = process.stderr.read()
            if stderr_output:
                logger.error(f"Serf monitor stderr output: {stderr_output.strip()}")
                with metrics_lock:
                    app_metrics["serf_monitor_last_error"] = f"Serf CLI error: {stderr_output.strip()}"

            process.wait()
            if process.returncode != 0:
                logger.error(f"Serf monitor exited with code: {process.returncode}")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "Exited with error"
                    app_metrics["serf_monitor_last_error"] = f"Exit code: {process.returncode}"
            else:
                logger.info("Serf monitor exited gracefully")
                with metrics_lock:
                    app_metrics["serf_monitor_status"] = "Exited gracefully"

        except FileNotFoundError:
            logger.critical(f"Serf executable not found: {serf_exec_path}")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = "CRITICAL: Executable missing"
                app_metrics["serf_monitor_last_error"] = f"Missing executable: {serf_exec_path}"
            time.sleep(10)

        except Exception as e:
            logger.critical(f"Serf monitor thread fatal error: {e}")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = "Initialization error"
                app_metrics["serf_monitor_last_error"] = f"Startup error: {e}"
            time.sleep(5)
