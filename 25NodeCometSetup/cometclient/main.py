import requests
import json
import time
import base64
import datetime
import urllib.parse
import logging
import sys
import redis

# --- Configuration ---
# URL for your colleague's Hilbert service (running in container 5)
HILBERT_URL = "http://127.0.0.1:4041/hilbert-output"

# URL for your CometBFT node's RPC (running on the host)
COMETBFT_RPC_URL = "http://127.0.0.1:26657"
SERF_URL = "http://127.0.0.1:5555"
BUYER_NODE_JSON = "/opt/serfapp/node.json"

rd = redis.Redis(host='localhost', port=6379, decode_responses=True)
channel = "liqo:initiate"

# How often to poll for new data
POLL_INTERVAL_SECONDS = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


# --- End Configuration ---


def find_best_seller(api_data):
    """
    Parses the Hilbert API data and finds the seller with the lowest price_per_ram.
    """
    try:
        results = api_data.get("results", [])
        best_seller = None
        lowest_price = float('inf')
        seller_ip = None
        cpu = 0
        ram = 0.0
        storage = 0
        gpu = 0

        logger.info("--- Scanning for Sellers ---")
        for node in results:
            node_name = node.get("name")
            price = node.get("price_per_ram")

            if node_name and price is not None:
                logger.info(f"  - Considering node '{node_name}' (Price: {price})")
                if price < lowest_price:
                    lowest_price = price
                    best_seller = node_name
                    seller_ip = node.get("ip", None)
                    cpu = node.get("cpu", 0)
                    ram = node.get("ram", 0.0)
                    storage = node.get("storage", 0)
                    gpu = node.get("gpu", 0)

        if best_seller:
            logger.info(f"--- Found best seller: '{best_seller}' at price {lowest_price} ---")
            # Convert float price (e.g., 1.79) to integer tokens (e.g., 179)
            amount_in_tokens = int(lowest_price * 100)
            return best_seller, amount_in_tokens, seller_ip, cpu, ram, storage, gpu
        else:
            logger.info("--- No valid sellers found. ---")
            return None, 0, None, 0, 0.0, 0, 0



    except Exception as e:
        logger.error(f"Error parsing Hilbert data: {e}")
        return None, 0


def create_transaction(buyer, seller_name, amount):
    """
    Creates the JSON payload for our transaction.
    """
    tx = {
        "type": "transfer",
        "from_node": buyer,
        "to_node": seller_name,
        "amount": f"{amount} tokens",
        "timestamp": datetime.datetime.now().isoformat()
    }
    logger.info(f"Prepared transaction: {json.dumps(tx)}")
    return tx


def publish_redis(buyer, buyer_ip, seller, seller_ip, cpu, ram, storage, gpu, amount):
    logger.info("Preparing records to publish to redis..")
    tx = {
        "type": "transfer",
        "from_node": buyer,
        "buyer_ip": buyer_ip,
        "to_node": seller,
        "seller_ip": seller_ip,
        "cpu": cpu,
        "ram": ram,
        "storage": storage,
        "gpu": gpu,
        "amount": f"{amount} tokens",
        "timestamp": datetime.datetime.now().isoformat()
    }
    try:
        msg = json.dumps(tx)
        rd.publish(channel, msg)
        logger.info(f"Message has been published to Redis: {msg}")
    except Exception as e:
        logger.error(f"Received error while publishing to redis: {e}")


def get_node_name(json_path):
    try:
        with open(json_path, 'r') as file:
            data = json.load(file)
            node_name = data.get("node_name")
            if node_name is None:
                raise KeyError("Key 'node_name' not found in JSON file.")
            return node_name
    except FileNotFoundError:
        logger.error(f"Error: File not found at {json_path}")
    except json.JSONDecodeError:
        logger.error(f"Error: Invalid JSON format in {json_path}")
    except Exception as e:
        logger.error(f"Error: {e}")


def check_comet_status():
    logger.info(f"Checking Cometbft Health {COMETBFT_RPC_URL}/health.....")
    try:
        response = requests.get(f"{COMETBFT_RPC_URL}/health", timeout=5)
        response.raise_for_status()
        data = response.json()

        if "result" in data and isinstance(data["result"], dict) and not data["result"]:
            logger.info("CometBFT node is healthy")
        elif "error" in data:
            logger.error(f"CometBFT error: {data['error']}")
        else:
            logger.error(f"Unexpected response format: {data}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")

    logger.info(f"Checking Cometbft current status {COMETBFT_RPC_URL}/status....")
    try:
        response = requests.get(f"{COMETBFT_RPC_URL}/status", timeout=5)
        response.raise_for_status()
        data = response.json()

        sync_info = data.get("result", {}).get("sync_info", {})
        catching_up = sync_info.get("catching_up")

        if catching_up is True:
            logger.error("⚠️  CometBFT node is still syncing blocks. Try after sometime. Terminating execution...")
            sys.exit(1)
        elif catching_up is False:
            logger.info("✅  CometBFT node is fully synchronized and ready for transactions.")
        else:
            logger.error("❌  Unable to determine catching_up status. Invalid or missing field.")

    except requests.exceptions.RequestException as e:
        logger.error(f"❌  Request failed: {e}")
        sys.exit(1)
    return None


def dial_peers(peers: list[str], persistent: bool = False):
    """
    Dials a list of peers using /dial_peers.
    Each peer string should be in format: <node_id>@<ip>:<port>
    """
    try:
        peers_json = json.dumps(peers)
        params = {
            "peers": peers_json,
            "persistent": str(persistent).lower()
        }
        url = f"{COMETBFT_RPC_URL}/dial_peers?" + urllib.parse.urlencode(params)
        logger.info(f"[P2P] Dialing peers: {peers}")
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        logger.info(f"[P2P] Dial response: {data}")
    except requests.RequestException as e:
        logger.error(f"[P2P] Failed to dial peers: {e}")
    return None


def get_nodeip_and_bftaddr(buyer: str):
    logger.info(f"Checking Active members from {SERF_URL}/members")
    buyer_ip = None
    response = requests.get(f"{SERF_URL}/members", timeout=5)
    if response.status_code == 200:
        members_data = response.json()
        bft_peers = []
        for member in members_data:
            if member.get("Name") == buyer:
                buyer_ip = member.get("Addr", None)
            tags = member.get("Tags", {})
            bft_addr = tags.get("rpc_addr")
            if bft_addr:
                bft_peers.append(bft_addr)
        return bft_peers, buyer_ip
    else:
        logger.error("Failed to get members from Serf.")
        return [], None


def broadcast_transaction(tx_json):
    """
    Encodes and broadcasts the transaction to the CometBFT node via JSON-RPC.
    """
    try:
        # Step 1: Convert the JSON transaction to bytes, then Base64 encode it
        tx_bytes = json.dumps(tx_json).encode('utf-8')
        tx_base64 = base64.b64encode(tx_bytes).decode('utf-8')
        logger.info(f"Base64 encoded: {tx_base64}")

        # Step 2: Prepare the JSON-RPC payload
        params = {"tx": f'"{tx_base64}"'}

        # Step 3: Send the request to the CometBFT node
        logger.info(f"Broadcasting tx to {COMETBFT_RPC_URL} via JSON-RPC...")
        url = f"{COMETBFT_RPC_URL}/broadcast_tx_sync"
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()  # Raise an exception for bad HTTP status (4xx or 5xx)

        response_json = response.json()
        broadcast_tx_hash = response_json.get("result", {}).get("hash")

        if "result" in response_json:
            result = response_json["result"]
            if result.get("code") == 0:
                logger.info("\nTransaction broadcast successful!")
                logger.info(f"CometBFT Response: {result}")
            else:
                logger.info("\nTransaction was REJECTED by CheckTx.")
                logger.info(f"CometBFT Response: {result}")
        else:
            logger.info(f"\nTransaction broadcast FAILED. Unexpected response:")
            logger.info(response_json)

        return broadcast_tx_hash

    except requests.exceptions.ConnectionError as e:
        logger.error(f"\nTransaction broadcast FAILED. Could not connect to CometBFT RPC.")
        logger.error(f"Error: {e}")
    except Exception as e:
        logger.error(f"\nTransaction broadcast FAILED. An error occurred:")
        logger.error(f"Error: {e}")


def validate_transaction(tx_hash: str):
    logger.info(f"Validation url:  {COMETBFT_RPC_URL}/tx  Transaction hash: {tx_hash}")
    try:
        url = f"{COMETBFT_RPC_URL}/tx"
        params = {"hash": f"0x{tx_hash.lstrip('0x')}", "prove": "true"}
        response = requests.get(url, params=params, timeout=3)
        result = response.json()
        if "error" in result:
            logger.error(f"Error received while validating transaction: {result}")
        else:
            tx_result = result.get("result")
            if tx_result:
                logger.info(f"Transaction Results for {tx_hash}: {tx_result}")
            else:
                logger.error(f"Transaction {tx_hash} not found...")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌  Request failed: {e}")
    except Exception as ex:
        logger.error(f"Exception raised {ex}")
    return None


def main_loop():
    buyer = get_node_name(BUYER_NODE_JSON)
    # Important: Dial Peers to connect Peers
    bft_addr, buyer_ip = get_nodeip_and_bftaddr(buyer)
    dial_peers(peers=bft_addr, persistent=True)
    # Check Comet health and current status
    check_comet_status()
    logger.info("--- Hilbert Core Client ---")
    logger.info(f"Polling {HILBERT_URL} every {POLL_INTERVAL_SECONDS} seconds.")
    logger.info(f"Buyer node is: {buyer}")
    logger.info("---------------------------")
    tx_hash = ""
    seller = None
    seller_ip = None
    cpu = 0
    ram = 0.0
    storage = 0
    gpu = 0
    amount = 0

    while True:
        try:
            logger.info(f"\n[{datetime.datetime.now().isoformat()}] Polling Hilbert for data...")
            # Fetch data from Hilbert
            response = requests.get(HILBERT_URL, timeout=5)
            response.raise_for_status()
            api_data = response.json()

            # Find the best seller
            seller, amount, seller_ip, cpu, ram, storage, gpu = find_best_seller(api_data)

            if seller and amount > 0:
                # Create and broadcast the transaction
                tx_payload = create_transaction(buyer, seller, amount)
                tx_hash = broadcast_transaction(tx_payload)

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Error connecting to Hilbert URL {HILBERT_URL}: {e}")
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP Error from Hilbert URL: {e}")
        except json.JSONDecodeError:
            logger.error("Error: Could not decode JSON response from Hilbert.")
        except Exception as e:
            logger.error(f"An unexpected error occurred in main loop: {e}")

        logger.info("Validating transaction status....")
        time.sleep(20)
        if tx_hash:
            validate_transaction(tx_hash)
            publish_redis(buyer, buyer_ip, seller, seller_ip, cpu, ram, storage, gpu, amount)
        else:
            logger.error("Received Invalid hash after transaction broadcast...")
            continue
        logger.info(f"\nWaiting {POLL_INTERVAL_SECONDS} seconds before next poll...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()
