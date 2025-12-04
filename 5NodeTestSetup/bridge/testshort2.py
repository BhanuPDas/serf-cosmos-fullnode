import json
import base64
import requests
import logging
import time
import os

# --- Configuration ---
# Set logging level (INFO for general operations, DEBUG for detailed troubleshooting)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Serf Agent RPC address (replace with your actual Serf RPC address)
SERF_RPC_ADDR = os.getenv("SERF_RPC_ADDR", "http://172.20.20.7:7373")
# CometBFT RPC URL (replace with your actual CometBFT RPC URL)
COMETBFT_RPC_URL = os.getenv("COMETBFT_RPC_URL", "http://localhost:26657")

# --- Serf Client ---
class SerfClient:
    def __init__(self, rpc_addr):
        self.rpc_addr = rpc_addr
        self.session = requests.Session()
        logger.info(f"SerfClient initialized for RPC: {self.rpc_addr}")

    def dispatch_event(self, event_name, payload, coalesce=True):
        """
        Dispatches a user event via Serf RPC.
        The payload should be a Base64 encoded string containing the full transaction data.
        """
        url = f"{self.rpc_addr}/v1/event/{event_name}"
        headers = {"Content-Type": "application/json"}
        data = {
            "Payload": payload,
            "Coalesce": coalesce
        }
        try:
            response = self.session.post(url, headers=headers, json=data, timeout=5)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Successfully dispatched Serf event '{event_name}' via RPC. Output: {result}")
            return result
        except requests.exceptions.Timeout:
            logger.error(f"Timeout connecting to Serf RPC at {self.rpc_addr}.")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Failed to connect to Serf RPC at {self.rpc_addr}: {e}")
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP Error from Serf RPC: {e.response.status_code} - {e.response.text}")
            raise
        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON from Serf RPC response: {response.text}")
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred during Serf event dispatch: {e}")
            raise

# --- CometBFT Interaction ---
def broadcast_to_cometbft(payload_to_cometbft_b64):
    """
    Broadcasts a Base64-encoded transaction to CometBFT's RPC using a POST request with JSON-RPC 2.0 body.
    This triggers validation (via ABCI CheckTx), consensus (if valid), and final broadcasting.
    """
    broadcast_url = f"{COMETBFT_RPC_URL}/" 
    
    json_rpc_data = {
        "jsonrpc": "2.0",
        "id": "jsonrpc-client",
        "method": "broadcast_tx_sync",
        "params": [payload_to_cometbft_b64]
    }
    
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(broadcast_url, headers=headers, json=json_rpc_data, timeout=10)
        response.raise_for_status()
        cometbft_response = response.json()

        logger.info(f"CometBFT RPC broadcast response received: {cometbft_response}")

        if 'error' in cometbft_response:
            error_code = cometbft_response['error'].get('code')
            error_message = cometbft_response['error'].get('message')
            error_data = cometbft_response['error'].get('data', '')
            logger.error(f"CometBFT RPC Broadcast Failed: Error Code={error_code}, Message='{error_message}', Data='{error_data}'")
            if error_code == -32602:
                 logger.error("CometBFT 'Invalid params' error. Ensure the transaction payload is a valid Base64-encoded string and the JSON-RPC format is correct.")
            elif error_code == 2:
                logger.error("CometBFT returned Code=2, indicating the ABCI application rejected the transaction. Check your ABCI application's logs for details!")
        else:
            tx_hash = cometbft_response.get('result', {}).get('hash')
            if tx_hash:
                logger.info(f"Transaction successfully broadcast to CometBFT. Hash: {tx_hash}")
                # You might want to poll CometBFT for the transaction status or block height here
                # to confirm it was included in a block.
            else:
                logger.warning("CometBFT broadcast response did not contain a transaction hash in 'result'.")
        
        return cometbft_response

    except requests.exceptions.Timeout:
        logger.error(f"Timeout connecting to CometBFT RPC at {COMETBFT_RPC_URL}.")
        raise
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Failed to connect to CometBFT RPC at {COMETBFT_RPC_URL}: {e}")
        raise
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP Error from CometBFT RPC: {e.response.status_code} - {e.response.text}")
        logger.error(f"CometBFT Error Body: {e.response.text}")
        raise
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from CometBFT response: {response.text}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred during CometBFT broadcast: {e}")
        raise

# --- Main Workflow ---
def main():
    logger.info("Starting blockchain interaction workflow.")

    serf_client = SerfClient(SERF_RPC_ADDR)

    # Example Transaction Data
    transaction_data = {
        "type": "transfer",
        "from_node": "clab-century-serf4",
        "to_node": "clab-century-serf1",
        "amount": "78 tokens",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    }
    full_tx_json_string = json.dumps(transaction_data)
    
    logger.info(f"Generated transaction: {full_tx_json_string}")

    # Encode the full transaction JSON string to Base64 for transmission via Serf
    serf_event_payload_b64 = base64.b64encode(full_tx_json_string.encode('utf-8')).decode('utf-8')
    event_name = f"transfer-{transaction_data['from_node']}-to-{transaction_data['to_node']}"

    try:
        logger.info(f"Dispatching Serf event '{event_name}' with transaction data.")
        # This sends the transaction data to ALL nodes in the Serf cluster.
        serf_client.dispatch_event(event_name, serf_event_payload_b64)
        logger.info("Serf event dispatched. All Serf agents configured to monitor this event will receive it.")
        
        # --- Critical Workflow Part (Conceptual for this script) ---
        # The key aspect of your system is that a *separate component* (your existing SerfMonitorThread)
        # on the CometBFT node is responsible for:
        # 1. Receiving the Serf event.
        # 2. Decoding the Base64 payload to get the full transaction JSON.
        # 3. Broadcasting this full transaction to its local CometBFT node's RPC.
        
        logger.info("The actual broadcast to CometBFT is handled by the Serf event listener (e.g., your SerfMonitorThread) on the CometBFT node.")
        logger.info("This script has successfully sent the transaction via Serf.")

        # If this script were also the SerfMonitorThread, it would perform the following:
        # received_full_tx_json_string_by_monitor = base64.b64decode(serf_event_payload_b64).decode('utf-8')
        # cometbft_tx_payload_b64_by_monitor = base64.b64encode(received_full_tx_json_string_by_monitor.encode('utf-8')).decode('utf-8')
        # logger.info("SerfMonitorThread (Illustrative): Broadcasting received transaction to CometBFT.")
        # broadcast_to_cometbft(cometbft_tx_payload_b64_by_monitor)

    except Exception as e:
        logger.error(f"Workflow execution failed: {e}")

if __name__ == "__main__":
    main()
