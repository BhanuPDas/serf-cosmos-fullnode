import requests
import json
import base64

RPC_URL = "http://localhost:26657"

def broadcast_tx_sync(tx_bytes):
    tx_base64 = base64.b64encode(tx_bytes).decode('utf-8')
    
    headers = {'Content-Type': 'application/json'}
    payload = {
        "jsonrpc": "2.0",
        "method": "broadcast_tx_sync",
        "params": [tx_base64],
        "id": 1
    }
    
    try:
        response = requests.post(f"{RPC_URL}", headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        
        result = response.json()
        print(f"CometBFT RPC Response: {json.dumps(result, indent=2)}")
        
        if result.get('result', {}).get('code') == 0:
            print("Transaction broadcast successful!")
        else:
            error_code = result.get('result', {}).get('code')
            error_log = result.get('result', {}).get('log', 'No log message provided by ABCI app.')
            print(f"Transaction broadcast failed! Code: {error_code}, Log: '{error_log}'")
            print("This usually means your ABCI application rejected the transaction.")
            
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error: Could not connect to CometBFT RPC at {RPC_URL}. Is CometBFT running?")
        print(e)
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: Received {e.response.status_code} from CometBFT RPC.")
        print(e)
    except json.JSONDecodeError as e:
        print(f"JSON Decode Error: Could not parse response from CometBFT RPC.")
        print(e)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    transfer_tx_data = {
        "type": "transfer",
        "from": "sender_account",
        "to": "receiver_account",
        "amount": 100
    }
    transaction_data = base64.b64encode(json.dumps(transfer_tx_data).encode('utf-8'))
    
    print(f"Attempting to broadcast transaction: {transaction_data.decode('utf-8')}")
    
    broadcast_tx_sync(transaction_data)
