import requests
import json
import time
import base64
import datetime
import urllib.parse
import logging
import random

# --- Configuration ---
# URL for your colleague's Hilbert service (running in container 5)
HILBERT_URL = "http://127.0.0.1:4041/hilbert-output"

# URL for your CometBFT node's RPC (running on the host)
COMETBFT_RPC_URL = "http://127.0.0.1:26657"

# The node that is buying resources
BUYER_NODE = [
    "clab-century-serf1", "clab-century-serf2",
    "clab-century-serf3", "clab-century-serf4",
    "clab-century-serf5", "clab-century-serf6",
    "clab-century-serf7", "clab-century-serf8",
    "clab-century-serf9", "clab-century-serf10"
]

# How often to poll for new data
POLL_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


# --- End Configuration ---


def find_best_seller(api_data, buyer):
    """
    Parses the Hilbert API data and finds the seller with the lowest price_per_ram.
    """
    try:
        results = api_data.get("results", [])
        best_seller = None
        lowest_price = float('inf')

        logger.info("--- Scanning for Sellers ---")
        for node in results:
            node_name = node.get("name")
            price = node.get("price_per_ram")

            # Skip if the node is the buyer
            if node_name == buyer:
                continue

            if node_name and price is not None:
                logger.info(f"  - Considering node '{node_name}' (Price: {price})")
                if price < lowest_price:
                    lowest_price = price
                    best_seller = node_name

        if best_seller:
            logger.info(f"--- Found best seller: '{best_seller}' at price {lowest_price} ---")
            # Convert float price (e.g., 1.79) to integer tokens (e.g., 179)
            amount_in_tokens = int(lowest_price * 100)
            return best_seller, amount_in_tokens
        else:
            logger.info("--- No valid sellers found. ---")
            return None, 0



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


def dial_peers(peers: list[str], persistent: bool = False):
    """
    Dials a list of peers using /v1/dial_peers.
    Each peer string should be in format: <node_id>@<ip>:<port>
    """
    try:
        peers_json = json.dumps(peers)
        params = {
            "peers": peers_json,
            "persistent": str(persistent).lower()
        }
        url = f"{COMETBFT_RPC_URL}/v1/dial_peers?" + urllib.parse.urlencode(params)
        logger.info(f"[P2P] Dialing peers: {peers}, persistent={persistent}, URL: {url}")
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        logger.info(f"[P2P] Dial response: {data}")
    except requests.RequestException as e:
        logger.error(f"[P2P] Failed to dial peers: {e}")
    return None


def check_active_buyers():
    response = requests.get("http://localhost:5555/members", timeout=5)
    if response.status_code == 200:
        members_data = response.json()
        rpc_peers = []
        for member in members_data:
            tags = member.get("Tags", {})
            rpc_addr = tags.get("rpc_addr")
            if rpc_addr:  # only add if exists
                rpc_peers.append(rpc_addr)
        return rpc_peers


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

        # Step 3: Fetch Active Buyers
        rpc_addr = check_active_buyers()

        # Step 4: Dial Peers nodes
        dial_peers(peers=rpc_addr, persistent=True)

        # Step 5: Send the request to the CometBFT node
        logger.info(f"Broadcasting tx to {COMETBFT_RPC_URL} via JSON-RPC...")
        url = f"{COMETBFT_RPC_URL}/v1/broadcast_tx_sync"
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()  # Raise an exception for bad HTTP status (4xx or 5xx)

        response_json = response.json()

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



    except requests.exceptions.ConnectionError as e:
        logger.error(f"\nTransaction broadcast FAILED. Could not connect to CometBFT RPC.")
        logger.error(f"Error: {e}")
    except Exception as e:
        logger.error(f"\nTransaction broadcast FAILED. An error occurred:")
        logger.error(f"Error: {e}")


def main_loop():
    """
    Main client loop.
    """

    buyer = random.choice(BUYER_NODE)
    logger.info("--- Hilbert Core Client ---")
    logger.info(f"Polling {HILBERT_URL} every {POLL_INTERVAL_SECONDS} seconds.")
    logger.info(f"Buyer node is: {buyer}")
    logger.info("---------------------------")

    while True:
        try:
            logger.info(f"\n[{datetime.datetime.now().isoformat()}] Polling Hilbert for data...")
            # Fetch data from Hilbert
            response = requests.get(HILBERT_URL, timeout=5)
            response.raise_for_status()
            api_data = response.json()
            logger.info(f"Seller List from Hilbert: {api_data}")
            # Find the best seller
            seller, amount = find_best_seller(api_data, buyer)

            if seller and amount > 0:
                # Create and broadcast the transaction
                tx_payload = create_transaction(buyer, seller, amount)
                broadcast_transaction(tx_payload)



        except requests.exceptions.ConnectionError as e:
            logger.error(f"Error connecting to Hilbert URL {HILBERT_URL}: {e}")
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP Error from Hilbert URL: {e}")
        except json.JSONDecodeError:
            logger.error("Error: Could not decode JSON response from Hilbert.")
        except Exception as e:
            logger.error(f"An unexpected error occurred in main loop: {e}")

        logger.info(f"\nWaiting {POLL_INTERVAL_SECONDS} seconds before next poll...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()
