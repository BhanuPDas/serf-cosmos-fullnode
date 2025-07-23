import json
import base64
import requests
import logging
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SERF_RPC_ADDR = "http://172.20.20.7:7373"
COMETBFT_RPC_URL = "http://localhost:26657"

class SerfClient:
    def __init__(self, rpc_addr):
        self.rpc_addr = rpc_addr
        self.session = requests.Session()
        logger.info(f"SerfClient initialized for RPC: {self.rpc_addr}")

    def dispatch_event(self, event_name, payload, coalesce=True):
        url = f"{self.rpc_addr}/v1/event/{event_name}"
        headers = {"Content-Type": "application/json"}
        data = {
            "Payload": payload,
            "Coalesce": coalesce
        }
        try:
            response = self.session.post(url, headers=headers, json=data)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Successfully dispatched Serf event '{event_name}' via RPC. Output: {result}")
            return result
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

def broadcast_to_cometbft(payload_to_cometbft_b64):
    broadcast_url = f"{COMETBFT_RPC_URL}/" # CometBFT JSON-RPC usually listens at the root endpoint
    
    json_rpc_data = {
        "jsonrpc": "2.0",
        "id": "jsonrpc-client",
        "method": "broadcast_tx_sync",
        "params": [payload_to_cometbft_b64]
    }
    
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(broadcast_url, headers=headers, json=json_rpc_data)
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
            logger.info(f"Transaction successfully broadcast to CometBFT. Hash: {cometbft_response.get('result', {}).get('hash')}")
        
        return cometbft_response

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

def main():
    logger.info("Starting real-world transaction flow with actual Serf and CometBFT connections.")

    serf_client = SerfClient(SERF_RPC_ADDR)

    transaction_data = {
        "type": "transfer",
        "from_node": "clab-century-serf4",
        "to_node": "clab-century-serf1",
        "amount": "78 tokens",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    }
    full_tx_json_string = json.dumps(transaction_data)
    
    logger.info(f"Generated transaction: {full_tx_json_string}")

    serf_event_payload_b64 = base64.b64encode(full_tx_json_string.encode('utf-8')).decode('utf-8')
    event_name = f"transfer-{transaction_data['from_node']}-to-{transaction_data['to_node']}"

    try:
        logger.info(f"Attempting to dispatch Serf event '{event_name}' with full transaction data.")
        serf_client.dispatch_event(event_name, serf_event_payload_b64)
        logger.info("Serf event dispatched. Your SerfMonitorThread on the CometBFT node should now receive and process this event.")
        
        logger.info("The next step in the workflow is handled by the actual SerfMonitorThread on the CometBFT node.")
        logger.info("The SerfMonitorThread will decode the Serf event payload and then broadcast it to CometBFT.")
        

    except Exception as e:
        logger.error(f"Workflow failed: {e}")

if __name__ == "__main__":
    main()
