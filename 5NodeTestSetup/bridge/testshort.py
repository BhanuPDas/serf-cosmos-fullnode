import json
import base64
import requests # For making HTTP requests to CometBFT
import logging

# Configure logging for better visibility
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Part 1: Simulating the Transaction Dispatch (Thread-4 in your logs) ---

def trigger_random_transaction_simplified():
    """
    Simulates generating a transaction and dispatching it via Serf.
    Crucially, it includes the FULL transaction in the Serf payload.
    """
    transaction_data = {
        "type": "transfer",
        "from_node": "clab-century-serf4",
        "to_node": "clab-century-serf1",
        "amount": "78 tokens",
        "timestamp": "2025-07-21 12:25:44" # Using the timestamp from your log for consistency
    }

    # In a real scenario, you'd calculate a proper tx_hash for the *full* transaction
    # For this example, we'll just use a placeholder or re-use the one from the log.
    # The actual tx_hash isn't what CometBFT needs for broadcast, it needs the data.
    # Let's say a real hash generation function would produce this:
    tx_hash_for_logging = "0719914f96d2cd273bd8335882565fba3d08dc66fbec466af2b7876eb76a4a16"

    # Convert the FULL transaction_data to a JSON string, then to bytes, then Base64 encode
    # This is what gets sent as the Serf event payload
    full_tx_json_string = json.dumps(transaction_data)
    serf_payload_b64 = base64.b64encode(full_tx_json_string.encode('utf-8')).decode('utf-8')

    # Simulate dispatching via Serf RPC (e.g., using requests to a Serf agent)
    serf_event_name = f"transfer-{transaction_data['from_node']}-to-{transaction_data['to_node']}"

    # In a real application, you'd make an actual RPC call to Serf.
    # For this example, we'll just log what Serf *would* receive.
    logger.info(f"Simulating Serf dispatch of event '{serf_event_name}' with full transaction payload.")
    logger.debug(f"Transaction JSON (full): {full_tx_json_string}")
    logger.debug(f"Base64-encoded Serf payload (full transaction): {serf_payload_b64}")

    # Now, let's pass this serf_payload_b64 to the monitor to simulate receipt
    return serf_event_name, serf_payload_b64

# --- Part 2: Simulating the Serf Monitor Thread (SerfMonitorThread in your logs) ---

def serf_monitor_thread_simplified(event_name, serf_payload_b64):
    """
    Simulates the Serf monitor receiving an event and broadcasting to CometBFT.
    It now extracts the FULL transaction from the payload.
    """
    cometbft_rpc_url = "http://localhost:26657" # Your CometBFT RPC URL

    logger.info(f"SerfMonitorThread - Received Serf event: Name='{event_name}', Payload(base64)='{serf_payload_b64[:30]}...'")

    try:
        # Decode the Serf payload to get the original FULL transaction JSON string
        full_tx_json_string = base64.b64decode(serf_payload_b64).decode('utf-8')
        logger.debug(f"SerfMonitorThread - Decoded full transaction JSON from Serf payload: {full_tx_json_string}")

        # The data to send to CometBFT's broadcast_tx_sync endpoint should be
        # the BASE64-encoded bytes of the transaction JSON.
        # So, re-encode the *transaction JSON string* to bytes, then Base64.
        tx_bytes_for_cometbft = full_tx_json_string.encode('utf-8')
        payload_to_cometbft_b64 = base64.b64encode(tx_bytes_for_cometbft).decode('utf-8')

        logger.debug(f"SerfMonitorThread - Attempting to broadcast transaction (payload_b64_to_cometbft: {payload_to_cometbft_b64[:30]}...) to CometBFT RPC: {cometbft_rpc_url}/broadcast_tx_sync")

        # Make the POST request to CometBFT's broadcast_tx_sync endpoint
        broadcast_url = f"{cometbft_rpc_url}/broadcast_tx_sync"
        params = {"tx": payload_to_cometbft_b64}
        headers = {"Content-Type": "application/json"} # CometBFT often expects JSON content type

        # Important: CometBFT's broadcast_tx_sync expects the tx parameter
        # to be passed as a query parameter or within the JSON body,
        # depending on how you structure your request.
        # We'll use params for simplicity here.
        response = requests.get(broadcast_url, params=params) # Using GET with params, or POST with json=params

        # Alternatively, if POST with JSON body is preferred:
        # response = requests.post(broadcast_url, json=params, headers=headers)

        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
        cometbft_response = response.json()

        logger.info(f"SerfMonitorThread - CometBFT RPC broadcast response received: {cometbft_response}")

        if cometbft_response.get('code') == 0: # Assuming 'code': 0 means success
            logger.info(f"SerfMonitorThread - Transaction '{event_name}' successfully broadcast to CometBFT. Hash: {cometbft_response.get('hash')}")
        else:
            logger.error(f"SerfMonitorThread - CometBFT RPC Broadcast Failed for event '{event_name}': Code={cometbft_response.get('code')}, Log='{cometbft_response.get('log')}'")
            # This is where you would handle the CometBFT-specific error codes
            # (e.g., Code=2, which indicates an application-level error)

    except requests.exceptions.ConnectionError as e:
        logger.error(f"SerfMonitorThread - Failed to connect to CometBFT RPC at {cometbft_rpc_url}: {e}")
    except requests.exceptions.HTTPError as e:
        logger.error(f"SerfMonitorThread - HTTP Error from CometBFT RPC: {e.response.status_code} - {e.response.text}")
    except json.JSONDecodeError:
        logger.error("SerfMonitorThread - Failed to decode JSON from CometBFT response.")
    except Exception as e:
        logger.error(f"SerfMonitorThread - An unexpected error occurred during broadcast: {e}")

# --- Execute the simplified flow ---
if __name__ == "__main__":
    logger.info("Starting simplified transaction flow...")

    # 1. Simulate dispatching the event
    event_name_dispatched, serf_payload_dispatched = trigger_random_transaction_simplified()

    # 2. Simulate the monitor receiving and processing it
    serf_monitor_thread_simplified(event_name_dispatched, serf_payload_dispatched)

    logger.info("Simplified transaction flow finished.")
