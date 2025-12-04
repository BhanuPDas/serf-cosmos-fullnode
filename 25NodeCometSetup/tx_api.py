from flask import Flask, request, jsonify
import logging
import requests
import time
import base64
import datetime
import urllib.parse
import redis
import json

COMETBFT_RPC_URL = "http://127.0.0.1:26657"
SERF_URL = "http://127.0.0.1:5555"

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
rd = redis.Redis(host='localhost', port=6379, decode_responses=True)
channel = "liqo:initiate"
BUYER_NODE_JSON = "/opt/serfapp/node.json"
buyer_ip = None
app = Flask(__name__)


class CometNotReadyError(Exception):
    pass


def create_transaction(buyer, seller_name, amount):
    tx = {
        "type": "transfer",
        "from_node": buyer,
        "to_node": seller_name,
        "amount": f"{amount} tokens",
        "timestamp": datetime.datetime.now().isoformat()
    }
    logger.info(f"Prepared transaction: {json.dumps(tx)}")
    return tx


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


def publish_redis(buyer, b_ip, seller, seller_ip, cpu, ram, storage, gpu, amount):
    logger.info("Preparing records to publish to redis..")
    tx = {
        "type": "transfer",
        "from_node": buyer,
        "buyer_ip": b_ip,
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
            logger.error("⚠️  CometBFT node is still syncing blocks. Try after sometime....")
            raise CometNotReadyError("CometBFT node is still syncing blocks. Try after sometime")
        elif catching_up is False:
            logger.info("✅  CometBFT node is fully synchronized and ready for transactions.")

    except requests.exceptions.RequestException as e:
        logger.error(f"❌  Request failed: {e}")
        raise CometNotReadyError("Request failed")
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
    buyerip = None
    response = requests.get(f"{SERF_URL}/members", timeout=5)
    if response.status_code == 200:
        members_data = response.json()
        bft_peers = []
        for member in members_data:
            if member.get("Name") == buyer:
                buyerip = member.get("Addr", None)
            tags = member.get("Tags", {})
            bft_addr = tags.get("rpc_addr")
            if bft_addr:
                bft_peers.append(bft_addr)
        return bft_peers, buyerip
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
        return None
    except Exception as e:
        logger.error(f"\nTransaction broadcast FAILED. An error occurred:")
        logger.error(f"Error: {e}")
        return None


def validate_transaction(tx_hash: str):
    logger.info(f"Validation url:  {COMETBFT_RPC_URL}/tx  Transaction hash: {tx_hash}")
    try:
        url = f"{COMETBFT_RPC_URL}/tx"
        params = {"hash": f"0x{tx_hash.lstrip('0x')}", "prove": "true"}
        response = requests.get(url, params=params, timeout=3)
        result = response.json()
        error = result.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, int) and code < 0:
                logger.error(f"Error while validating tx: {error}")
                return f"Error while validating transaction: {error}"

        tx_result = result.get("result")
        if tx_result:
            logger.info(f"Transaction Results for {tx_hash}: {tx_result}")
            return tx_result
        else:
            logger.error(f"Transaction {tx_hash} not found...")
            return f"Transaction Error: {tx_hash} not found..."
    except requests.exceptions.RequestException as e:
        logger.error(f"❌  Request failed: {e}")
        return None
    except Exception as ex:
        logger.error(f"Exception raised {ex}")
        return None


@app.route('/initiate_tx', methods=['POST'])
def get_transaction():
    try:
        data = request.get_json(silent=True)
        if not data or not data.get("buyer") or not data.get("seller") or not data.get("seller_ip"):
            logger.info(f"Invalid request received: {data}")
            return jsonify({"error": "Invalid request received"}), 400
        buyer = data.get("buyer")
        seller = data.get("seller")
        seller_ip = data.get("seller_ip")
        cpu = data.get("cpu")
        ram = data.get("ram")
        storage = data.get("storage")
        gpu = data.get("gpu")
        amount = data.get("amount")
        logger.info(f"Received transaction request between BUYER: {buyer} and SELLER: {seller}")
        check_comet_status()
        logger.info(f"Preparing payload for transaction..")
        tx_payload = create_transaction(buyer, seller, amount)
        tx_hash = broadcast_transaction(tx_payload)
        logger.info(f"Broadcast Hash received from cometbft: {tx_hash}")
        if tx_hash:
            logger.info("Validating broadcast status from cometbft in 2 sec...")
            time.sleep(2)
            tx_result = validate_transaction(tx_hash)
            if tx_result and tx_result.get("result"):
                publish_redis(buyer, buyer_ip, seller, seller_ip, cpu, ram, storage, gpu, amount)
                return jsonify({"status": "success", "message": tx_result}), 200
            else:
                return jsonify({"status": "error", "message": tx_result}), 400
        else:
            return jsonify({"status": "error", "message": "Error occurred during transaction broadcast"}), 400
    except CometNotReadyError as e:
        logger.error(str(e))
        return jsonify({"status": "error", "message": str(e)}), 503
    except Exception as ex:
        logger.error(f"Unexpected error: {ex}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


if __name__ == '__main__':
    try:
        node = get_node_name(BUYER_NODE_JSON)
        # Important: Dial Peers to connect Peers
        bftaddr, bip = get_nodeip_and_bftaddr(node)
        buyer_ip = bip
        dial_peers(peers=bftaddr, persistent=True)
        app.run(debug=True, host='0.0.0.0', port=5005)
    except Exception as ex:
        logger.error(f"Unexpected error: {ex}")