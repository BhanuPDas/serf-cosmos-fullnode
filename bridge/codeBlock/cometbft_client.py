import requests
import logging
import base64
import json
import urllib.parse
import threading
import time
from datetime import datetime, timezone
import redis

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)
r = redis.Redis(host='localhost', port=6379, decode_responses=True)
stream_key = "transEventStream"


class MempoolClient:
    def __init__(self, cometbft_url="localhost"):
        self.base_url = f"{cometbft_url}"
        logger.info(f"[Init] MempoolClient initialized at {self.base_url}")

    def get_status(self):
        """Check /v1/status endpoint."""
        try:
            url = f"{self.base_url}/v1/status"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            status = response.json()
            logger.debug(f"[Status] Node status retrieved: {status}")
            return status
        except requests.RequestException as e:
            logger.error(f"[Status] Failed to retrieve status: {e}")
            return None

    def get_health(self):
        """Check /v1/health endpoint."""
        try:
            url = f"{self.base_url}/v1/health"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            logger.info("[Health] Node is healthy")
            return response.json()
        except requests.RequestException as e:
            logger.error(f"[Health] Health check failed: {e}")
            return None

    def broadcast_tx_sync(self, tx_data: str):
        """
        Broadcasts a transaction synchronously to the node using GET with tx as query param.
        `tx_data` must be a base64-encoded string.
        """
        try:
            url = f"{self.base_url}/v1/broadcast_tx_sync"
            params = {"tx": f'"{tx_data}"'}  # note the quotes around tx_data, matching example curl

            logger.info(f"[Tx] Broadcasting transaction (GET): {tx_data}")
            response = requests.get(url, params=params, timeout=5)
            response.raise_for_status()
            data = response.json()
            logger.info(f"[Tx] Broadcast response: {data}")
            return data
        except requests.RequestException as e:
            logger.error(f"[Tx] Failed to broadcast transaction: {e}")
            return None

    def dial_peers(self, peers: list[str], persistent: bool = False):
        """
        Dials a list of peers using /v1/dial_peers.
        Each peer string should be in format: <node_id>@<ip>:<port>
        """
        try:
            peers_encoded = ",".join(peers)
            params = {
                "peers": peers_encoded,
                "persistent": str(persistent).lower()
            }
            url = f"{self.base_url}/v1/dial_peers?" + urllib.parse.urlencode(params)
            logger.info(f"[P2P] Dialing peers: {peers}, persistent={persistent}")
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            logger.info(f"[P2P] Dial response: {data}")
            return data
        except requests.RequestException as e:
            logger.error(f"[P2P] Failed to dial peers: {e}")
            return None

    def poll_tx_status(self, tx_hash: str, max_attempts=10, interval=1):
        """
        Poll tx status asynchronously on a separate thread.

        :param tx_hash: Transaction hash string.
        :param max_attempts: How many times to poll before giving up.
        :param interval: Seconds between polls.
        """

        def poller():
            attempts = 0
            while attempts < max_attempts:
                try:
                    url = f"{self.base_url}/v1/tx"
                    params = {"hash": f"0x{tx_hash.lstrip('0x')}", "prove": "true"}
                    response = requests.get(url, params=params, timeout=3)
                    if response.status_code != 200:
                        logger.warning(f"[PollTxStatus] HTTP {response.status_code}: {response.text}")
                        attempts += 1
                        time.sleep(interval)
                        continue

                    result = response.json()
                    # Check if tx_result exists and code == 0 (success)
                    tx_result = result.get("result")
                    if tx_result:
                        msg = {"event": "poll-event", "result": json.dumps(result),
                               "success": str(True), "msg": "Transaction committed successfully",
                               "timestamp": datetime.now(timezone.utc).isoformat()}
                        cleaned_msg = {k: str(v) for k, v in msg.items() if v is not None}
                        msg_id = r.xadd(stream_key, cleaned_msg)
                        logger.info(f"Polling Results dispatched: {msg_id}")
                        return
                    else:
                        # Still pending or failed code
                        logger.info(f"[PollTxStatus] Transaction not yet committed or failed: {tx_result}")
                        attempts += 1
                        time.sleep(interval)
                except Exception as e:
                    logger.error(f"[PollTxStatus] Exception during polling: {e}")
                    attempts += 1
                    time.sleep(interval)

            # Timeout or failure
            msg = {"event": "poll-event", "result": None,
                   "success": str(False), "msg": f"Transaction not confirmed after {max_attempts} attempts",
                   "timestamp": datetime.now(timezone.utc).isoformat()}
            cleaned_msg1 = {k: str(v) for k, v in msg.items() if v is not None}
            msg_id = r.xadd(stream_key, cleaned_msg1)
            logger.info(f"Polling Results dispatched: {msg_id}")

        threading.Thread(target=poller, daemon=True).start()
