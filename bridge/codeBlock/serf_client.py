import json
import base64
import subprocess
import threading
import time
import requests
import logging
from datetime import datetime, timezone
import os
from collections import deque
import hashlib
import redis
from cometbft_client import MempoolClient

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
previous_dialed_peers = set()

LOCAL_NODE_NAME = os.uname().nodename  # Your node name, set properly
SERF_EXECUTABLE_PATH = "/usr/bin/serf"  # Change to your serf path
SERF_RPC_ADDR = "172.20.20.7:7373"  # Your serf RPC addr
default_p2p_port = 26656  # Default CometBFT P2P port
COMETBFT_RPC_URL = "http://localhost:26657"
cometbft = MempoolClient(COMETBFT_RPC_URL)
r = redis.Redis(host='localhost', port=6379, decode_responses=True)
stream_key = "transEventStream"
group_name = "execEvents"
consumer_name = "c1"

try:
    r.xgroup_create(stream_key, group_name, id='0', mkstream=True)
    logger.info("Group created.")
except redis.exceptions.ResponseError as e:
    if "BUSYGROUP" in str(e):
        logger.info("Group already exists.")
    else:
        raise


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

def is_valid_tx_hash(tx_hash: str) -> bool:
    hex_str = tx_hash.lower().lstrip("0x")
    return (
        len(hex_str) > 0 and
        len(hex_str) % 2 == 0 and
        all(c in "0123456789abcdef" for c in hex_str)
    )


def broadcast_response_callback(event_name: str, response, activity_entry, mempool_client):
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
            if not is_valid_tx_hash(broadcast_tx_hash):
                logger.warning(f"Invalid broadcast_tx_hash: {broadcast_tx_hash}")
                return
            mempool_client.poll_tx_status(broadcast_tx_hash)
            logger.info("Started Polling for consensus...")
        else:
            logger.error(f"Broadcast error for '{event_name}': {broadcast_status}")
            consensus_str = f"Broadcast Failed (Code: {code}) Log: {log[:50]}..."
            activity_entry["cometbft_consensus_status"] = consensus_str


def dial_peers():
    while True:
        try:
            current_peers = {
                tags.get("cometbft_node_id")
                for member in app_metrics.get("serf_members", [])
                if (tags := member.get("tags", {})).get("cometbft_node_id")
            }
            new_peers = current_peers - previous_dialed_peers
            if new_peers:
                logger.info(f"Dialing new peers: {list(new_peers)}")
                cometbft.dial_peers(list(new_peers))
                previous_dialed_peers.update(new_peers)
            else:
                logger.info("No new peers to dial.")
        except Exception as e:
            logger.error(f"Error while collecting peers to dial: {e}")
        time.sleep(10)


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
        # === Broadcast Transaction ===
        try:
            logger.info(f"Preparing payload for the broadcast: {kv_tx_b64}")
            broadcast_response = mempool_client.broadcast_tx_sync(kv_tx_b64)
            broadcast_response_callback(event_name, broadcast_response, activity_entry, mempool_client)
        except Exception as e:
            logger.exception(f"Unexpected error during broadcast: {e}")
            activity_entry["cometbft_broadcast_response"] = f"Broadcast Exception: {str(e)}"
            activity_entry["cometbft_consensus_status"] = "Broadcast failed due to exception"
    except Exception as e:
        logger.error(f"Error processing serf user event '{event_name}': {e}")


def serf_monitor_thread(serf_exec_path: str, rpc_addr: str, mempool_client):
    logger.info(f"Starting Serf monitor thread. Connecting to RPC {rpc_addr}")

    last_members_check_time = 0
    last_cometbft_status_check_time = 0
    MEMBER_CHECK_INTERVAL = 10
    COMETBFT_STATUS_CHECK_INTERVAL = 5

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
                comet_status_data = mempool_client.get_status()
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
            entries = r.xreadgroup(group_name, consumer_name, {stream_key: '>'}, block=5000, count=10)
            if entries:
                for stream, messages in entries:
                    for msg_id, data in messages:
                        logger.info(f"Consumer {consumer_name} received {msg_id}: {data}")
                        try:
                            event_name = data["event"]
                            if event_name.startswith("transfer"):
                                payload_b64 = data["payload"]
                                process_serf_user_event(event_name, payload_b64, mempool_client)
                            elif event_name.startswith("poll"):
                                res = data.get("result", "")
                                success = data.get("success", "").lower() == "true"
                                msg = data.get("msg", "")
                                logger.info(f"Received Polling result for the transaction: {event_name}")
                                if success:
                                    if res:
                                        result_json = json.loads(res)
                                        result = result_json.get("result", {})
                                        tx_result = result.get('tx_result', {})
                                        log_msg = tx_result.get('log', '') or ""
                                        consensus_str = (
                                            f"Transaction Committed! Height: {result.get('height')}, Log: {log_msg}"
                                        )
                                        logger.info(consensus_str)
                                else:
                                    logger.info(msg)
                            r.xack(stream_key, group_name, msg_id)
                            logger.info(f"{msg_id} is acknowledged.")
                        except KeyError as e:
                            logger.error(f"Missing expected field in Redis stream message: {e}")
                        except Exception as e:
                            logger.error(f"Error processing message {msg_id}: {e}")

        except Exception as e:
            logger.critical(f"Serf monitor thread fatal error: {e}")
            with metrics_lock:
                app_metrics["serf_monitor_status"] = "Initialization error"
                app_metrics["serf_monitor_last_error"] = f"Startup error: {e}"
            time.sleep(5)

#thread = threading.Thread(target=dial_peers,name="DialPeersThread")
#thread.daemon = True
#thread.start()
#logger.info("dial peer thread initiated.")
