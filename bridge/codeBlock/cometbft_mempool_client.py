import requests
import logging
import base64
import json
import urllib.parse
import threading
import time

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


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
            logger.info(f"[Status] Node status retrieved: {status}")
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
        Example: "f9baeaa15fedf5e1ef7448dd60f46c01f1a9e9c4@1.2.3.4:26656"
        """
        try:
            peers_encoded = json.dumps(peers)
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

    def poll_tx_status(self, tx_hash: str, callback, max_attempts=10, interval=1):
        """
        Poll tx status asynchronously on a separate thread.

        :param tx_hash: Transaction hash string.
        :param callback: Function with signature (success: bool, tx_data: dict, msg: str).
        :param max_attempts: How many times to poll before giving up.
        :param interval: Seconds between polls.
        """

        def poller():
            attempts = 0
            while attempts < max_attempts:
                try:
                    url = f"{self.base_url}/v1/tx"
                    params = {"hash": tx_hash}
                    response = requests.get(url, params=params, timeout=3)
                    if response.status_code != 200:
                        logger.warning(f"[PollTxStatus] HTTP {response.status_code}: {response.text}")
                        attempts += 1
                        time.sleep(interval)
                        continue

                    result = response.json()
                    # Check if tx_result exists and code == 0 (success)
                    tx_result = result.get("tx_result")
                    if tx_result and tx_result.get("code", 1) == 0:
                        callback(True, result, "Transaction committed successfully")
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
            callback(False, None, f"Transaction not confirmed after {max_attempts} attempts")

        threading.Thread(target=poller, daemon=True).start()