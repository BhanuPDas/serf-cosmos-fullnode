package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"

	abci_server "github.com/cometbft/cometbft/abci/server"
	"github.com/cometbft/cometbft/abci/types"
)
    type TransferTransaction struct {
    	Type      string `json:"type"`
    	FromNode  string `json:"from_node"`
    	ToNode    string `json:"to_node"`
    	Amount    string `json:"amount"` // e.g., "50 tokens"
    	Timestamp string `json:"timestamp"`
    }

    // MyApp represents your custom ABCI application state.
    // It embeds abci_types.BaseApplication to get default (no-op) implementations
    // for methods we don't explicitly override.
     type MyApp struct {
	types.BaseApplication
	Balances map[string]int
}

// All method signatures should use `v0.RequestInfo`, `v0.ResponseInfo`, etc.
    func (app *MyApp) Info(req types.RequestInfo) types.ResponseInfo { ... }
    func (app *MyApp) InitChain(req types.RequestInitChain) types.ResponseInitChain { ... }
    func (app *MyApp) CheckTx(req types.RequestCheckTx) types.ResponseCheckTx { ... }
    func (app *MyApp) DeliverTx(req types.RequestDeliverTx) types.ResponseDeliverTx { ... }
    func (app *MyApp) BeginBlock(req types.RequestBeginBlock) types.ResponseBeginBlock { ... }
    func (app *MyApp) EndBlock(req types.RequestEndBlock) types.ResponseEndBlock { ... }
    func (app *MyApp) Commit() types.ResponseCommit { ... }
    func (app *MyApp) Query(req types.RequestQuery) types.ResponseQuery { ... }

    func NewMyApp() *MyApp {
    	return &MyApp{
    		Balances: make(map[string]int), // Initialize the balances map
    	}
    }

    // Info is called to get information about the application.
    // CometBFT uses this during handshake to check application version and last committed block.
    func (app *MyApp) Info(req types.RequestInfo) types.ResponseInfo {
    	log.Printf("ABCI Info: Version=%s, BlockVersion=%d, P2PVersion=%d, ABCIVersion=%s",
    		req.Version, req.BlockVersion, req.P2PVersion, req.AbciVersion)
    	return types.ResponseInfo{
    		Version:         "1.0.0", // Your application's version
    		AppVersion:      1,
    		LastBlockHeight: 0, // For a simple in-memory app, this might be 0 or tracked
    	}
    }

    // InitChain is called once upon genesis, when the blockchain is initialized.
    // This is where you set initial state, like genesis validators or initial balances.
    func (app *MyApp) InitChain(req types.RequestInitChain) types.ResponseInitChain {
    	log.Println("ABCI InitChain: Initializing application state with genesis data.")

    	// Initialize some default balances for your Serf nodes
    	app.Balances["clab-century-serf1"] = 1000
    	app.Balances["clab-century-serf2"] = 1000
    	app.Balances["clab-century-serf3"] = 1000
    	app.Balances["clab-century-serf4"] = 1000
    	app.Balances["clab-century-serf5"] = 1000

    	log.Printf("Initial Balances: %v", app.Balances)

    	return types.ResponseInitChain{}
    }

    // CheckTx is called to validate a transaction before it enters the mempool.
    // This method should be fast and stateless (or use a cached state).
    func (app *MyApp) CheckTx(req types.RequestCheckTx) types.ResponseCheckTx {
    	log.Printf("ABCI CheckTx: Received raw transaction bytes (Base64 string): %s", string(req.Tx))

    	// --- Step 1: Base64 Decode the incoming transaction bytes ---
    	decodedJSONBytes, err := base64.StdEncoding.DecodeString(string(req.Tx))
    	if err != nil {
    		logMsg := fmt.Sprintf("ABCI CheckTx ERROR: Failed to Base64 decode transaction: %v", err)
    		log.Println(logMsg)
    		return types.ResponseCheckTx{Code: 2, Log: logMsg} // Code 2 for encoding/parsing error
    	}
    	log.Printf("ABCI CheckTx: Successfully Base64 decoded to raw JSON bytes: %s", string(decodedJSONBytes))

    	// --- Step 2: JSON Unmarshal the decoded bytes into our Go struct ---
    	var tx TransferTransaction
    	err = json.Unmarshal(decodedJSONBytes, &tx)
    	if err != nil {
    		logMsg := fmt.Sprintf("ABCI CheckTx ERROR: Failed to unmarshal JSON: %v. Decoded payload was: %s", err, string(decodedJSONBytes))
    		log.Println(logMsg)
    		return types.ResponseCheckTx{Code: 2, Log: logMsg} // Code 2 for parsing error
    	}
    	log.Printf("ABCI CheckTx: Successfully unmarshaled transaction: %+v", tx)

    	// --- Step 3: Application-Specific Validation ---
    	// Basic validation: check transaction type and required fields
    	if tx.Type != "transfer" {
    		logMsg := fmt.Sprintf("ABCI CheckTx ERROR: Invalid transaction type '%s'. Expected 'transfer'.", tx.Type)
    		log.Println(logMsg)
    		return types.ResponseCheckTx{Code: 3, Log: logMsg} // Use a different code for app-specific validation failure
    	}
    	if tx.FromNode == "" || tx.ToNode == "" || tx.Amount == "" {
    		logMsg := "ABCI CheckTx ERROR: Missing required fields (from_node, to_node, amount)."
    		log.Println(logMsg)
    		return types.ResponseCheckTx{Code: 4, Log: logMsg} // Another custom code
    	}

    	// Validate amount format and value
    	amountStr := strings.TrimSuffix(tx.Amount, " tokens")
    	amount, amountErr := strconv.Atoi(amountStr)
    	if amountErr != nil {
    		logMsg := fmt.Sprintf("ABCI CheckTx ERROR: Invalid amount format: %s", tx.Amount)
    		log.Println(logMsg)
    		return types.ResponseCheckTx{Code: 5, Log: logMsg}
    	}
    	if amount <= 0 {
    		logMsg := fmt.Sprintf("ABCI CheckTx ERROR: Transfer amount must be positive: %d", amount)
    		log.Println(logMsg)
    		return types.ResponseCheckTx{Code: 6, Log: logMsg}
    	}

    	// Stateful validation: Check sender's balance
    	// Note: In CheckTx, stateful checks should be fast and ideally against a cached state.
    	// For simplicity, we're directly accessing `app.Balances`.
    	senderBalance, exists := app.Balances[tx.FromNode]
    	if !exists || senderBalance < amount {
    		logMsg := fmt.Sprintf("ABCI CheckTx ERROR: Insufficient balance or sender '%s' does not exist. Balance: %d, Required: %d", tx.FromNode, senderBalance, amount)
    		log.Println(logMsg)
    		return types.ResponseCheckTx{Code: 7, Log: logMsg} // Code 7 for insufficient funds
    	}

    	log.Println("ABCI CheckTx: Transaction passed all validations. Returning Code 0.")
    	return types.ResponseCheckTx{Code: 0, Log: "Transaction successfully validated by CheckTx."}
    }

    // DeliverTx is called when a transaction is included in a block and committed.
    // This is where the application's state is actually updated.
    func (app *MyApp) DeliverTx(req types.RequestDeliverTx) types.ResponseDeliverTx {
    	log.Printf("ABCI DeliverTx: Processing transaction for block: %s", string(req.Tx))

    	// The parsing and basic validation logic will mirror CheckTx.
    	// A transaction that passed CheckTx should ideally pass these steps here too.
    	decodedJSONBytes, err := base64.StdEncoding.DecodeString(string(req.Tx))
    	if err != nil {
    		logMsg := fmt.Sprintf("ABCI DeliverTx ERROR: Failed to Base64 decode: %v", err)
    		log.Println(logMsg)
    		return types.ResponseDeliverTx{Code: 2, Log: logMsg}
    	}
    	var tx TransferTransaction
    	err = json.Unmarshal(decodedJSONBytes, &tx)
    	if err != nil {
    		logMsg := fmt.Sprintf("ABCI DeliverTx ERROR: Failed to unmarshal JSON: %v. Payload: %s", err, string(decodedJSONBytes))
    		log.Println(logMsg)
    		return types.ResponseDeliverTx{Code: 2, Log: logMsg}
    	}

    	// Apply state changes (update balances)
    	amountStr := strings.TrimSuffix(tx.Amount, " tokens")
    	amount, _ := strconv.Atoi(amountStr) // Error already handled in CheckTx

    	// Deduct from sender
    	app.Balances[tx.FromNode] -= amount
    	// Add to receiver (initialize receiver balance if they don't exist)
    	if _, exists := app.Balances[tx.ToNode]; !exists {
    		app.Balances[tx.ToNode] = 0
    	}
    	app.Balances[tx.ToNode] += amount

    	log.Printf("ABCI DeliverTx: Successfully processed transfer: %+v. Current Balances: %v", tx, app.Balances)
    	return types.ResponseDeliverTx{Code: 0, Log: "Transaction successfully applied to state."}
    }

    // BeginBlock is called before any transactions are delivered in a block.
    // Useful for block-level initialization or processing.
    func (app *MyApp) BeginBlock(req types.RequestBeginBlock) types.ResponseBeginBlock {
    	log.Printf("ABCI BeginBlock: Height %d, Hash %x", req.Header.Height, req.Hash)
    	return types.ResponseBeginBlock{}
    }

    // EndBlock is called after all transactions are delivered in a block.
    // Useful for block-level finalization or validator set updates.
    func (app *MyApp) EndBlock(req types.RequestEndBlock) types.ResponseEndBlock {
    	log.Printf("ABCI EndBlock: Height %d", req.Height)
    	return types.ResponseEndBlock{}
    }

    // Commit is called to persist the current application state.
    // This is critical for crash recovery and state synchronization.
    func (app *MyApp) Commit() types.ResponseCommit {
    	// In a real application, you would save `app.Balances` to a persistent database (e.g., LevelDB, BadgerDB).
    	// For this in-memory example, we just log the action.
    	log.Println("ABCI Commit: State committed (in-memory only for this example).")
    	return types.ResponseCommit{Data: []byte("State committed")} // Data can be the AppHash
    }

    // Query is used by clients to query the application state.
    // This is typically used for reading data from the blockchain.
    func (app *MyApp) Query(req types.RequestQuery) types.ResponseQuery {
    	log.Printf("ABCI Query: Path=%s, Data=%s", req.Path, string(req.Data))

    	if req.Path == "/balances" {
    		// Return all balances as JSON
    		balancesJSON, err := json.Marshal(app.Balances)
    		if err != nil {
    			return types.ResponseQuery{Code: 4, Log: fmt.Sprintf("Error marshaling balances: %v", err)}
    		}
    		return types.ResponseQuery{Code: 0, Value: balancesJSON, Log: "All balances queried."}
    	} else if strings.HasPrefix(req.Path, "/balance/") {
    		// Return specific account balance
    		account := strings.TrimPrefix(req.Path, "/balance/")
    		balance, exists := app.Balances[account]
    		if !exists {
    			return types.ResponseQuery{Code: 3, Log: fmt.Sprintf("Account not found: %s", account)}
    		}
    		return types.ResponseQuery{Code: 0, Value: []byte(fmt.Sprintf("%d", balance)), Log: fmt.Sprintf("Balance for %s queried.", account)}
    	}

    	return types.ResponseQuery{Code: 1, Log: "Unsupported query path."}
    }

    // The main function to start the ABCI server.
    func main() {
    	app := NewMyApp() // Create an instance of your ABCI application

    	// Determine the address to listen on (e.g., from command line arguments or config)
    	// Default to a Unix socket path.
    	addr := "unix:///home/shuddhank/gopyserf/my_abci_app/abci_app_executable.sock" // Use .sock suffix for clarity
    	if len(os.Args) > 1 {
    		addr = os.Args[1] // Allows specifying address as a command-line argument
    	}

    	// If using a Unix socket, ensure the old socket file is removed before starting the server.
    	if strings.HasPrefix(addr, "unix://") {
    		socketPath := strings.TrimPrefix(addr, "unix://")
    		if _, err := os.Stat(socketPath); err == nil {
    			if err := os.Remove(socketPath); err != nil {
    				log.Fatalf("Failed to remove old unix socket file %s: %v", socketPath, err)
    			}
    			log.Printf("Removed old unix socket file: %s", socketPath)
    		}
    	}

    	// Create the ABCI server.
    	// For TCP, use abci_server.NewServer("tcp://127.0.0.1:26658", "socket", app)
    	server := abci_server.NewSocketServer(addr, app)

    	// Start the server.
    	log.Printf("ABCI server listening on %s", addr)
    	if err := server.Start(); err != nil {
    		log.Fatalf("Error starting ABCI server: %v", err)
    	}

    	// Wait for server to stop (e.g., via interrupt signal).
    	defer server.Stop()
    	log.Println("ABCI server stopped.")
    }
