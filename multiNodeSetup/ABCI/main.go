package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"

	"github.com/cockroachdb/pebble/v2"
	abci_server "github.com/cometbft/cometbft/abci/server"
	abci_types "github.com/cometbft/cometbft/abci/types"
	"github.com/redis/go-redis/v9"
)

// --- CONFIGURATION ---
const REDIS_ADDR = "127.0.0.1:6379"
const REDIS_CHANNEL = "liqo:initiate"
const STATE_DB_PATH = "/root/abci/state.db"

// --- END CONFIGURATION ---

type TransferTransaction struct {
	Type      string `json:"type"`
	FromNode  string `json:"from_node"`
	ToNode    string `json:"to_node"`
	Amount    string `json:"amount"`
	Timestamp string `json:"timestamp"`
}

type MyApp struct {
	abci_types.BaseApplication
	ledger          map[string]int64
	lastBlockHeight int64
	appHash         []byte
	redisClient     *redis.Client
	pendingJobs     []TransferTransaction
}

// ---------- PERSISTENCE HELPERS ----------

// LoadBalancesFromDB loads existing balances from PebbleDB into memory.
func (app *MyApp) LoadBalancesFromDB() error {
	db, err := pebble.Open(STATE_DB_PATH, &pebble.Options{})
	if err != nil {
		log.Printf("[INIT] No existing state DB found or cannot open: %v", err)
		return err
	}
	defer func(db *pebble.DB) {
		err := db.Close()
		if err != nil {
			log.Printf("[INIT] Error closing state DB: %v", err)
		}
	}(db)

	iter, err := db.NewIter(&pebble.IterOptions{LowerBound: []byte("balance:"), UpperBound: []byte("balance;")})
	if err != nil {
		log.Printf("[INIT] Error opening state DB: %v", err)
	}
	defer func() {
		if cerr := iter.Close(); cerr != nil {
			log.Printf("[INIT] Error closing iterator: %v", cerr)
		}
	}()
	count := 0
	for iter.First(); iter.Valid(); iter.Next() {
		key := string(iter.Key())
		if strings.HasPrefix(key, "balance:") {
			node := strings.TrimPrefix(key, "balance:")
			valStr1, err := iter.ValueAndErr()
			if err != nil {
				log.Printf("[INIT] Error getting balance: %v", err)
				continue
			}
			valStr := string(valStr1)
			val, err := strconv.ParseInt(valStr, 10, 64)
			if err != nil {
				log.Printf("[INIT] Skipping invalid value for %s: %s", node, valStr)
				continue
			}
			app.ledger[node] = val
			count++
		}
	}
	if err := iter.Error(); err != nil {
		log.Printf("[INIT] Iterator encountered an error: %v", err)
		return err
	}
	log.Printf("[INIT] Loaded %d balances from Pebble DB: %+v", count, app.ledger)
	return nil
}

// SaveBalancesToDB persists the current ledger state to Pebble DB.
func (app *MyApp) SaveBalancesToDB() error {
	db, err := pebble.Open(STATE_DB_PATH, &pebble.Options{})
	if err != nil {
		return fmt.Errorf("failed to open state DB for write: %v", err)
	}
	defer func(db *pebble.DB) {
		err := db.Close()
		if err != nil {
			log.Printf("[INIT] Error closing state DB: %v", err)
		}
	}(db)

	for node, balance := range app.ledger {
		key := "balance:" + node
		val := []byte(fmt.Sprintf("%d", balance))
		if err := db.Set([]byte(key), val, pebble.Sync); err != nil {
			log.Printf("[SAVE] Failed to persist %s: %v", node, err)
		}
	}
	log.Println("[SAVE] Balances successfully persisted to Blockchain PebbleDB.")
	return nil
}

// ---------- END PERSISTENCE HELPERS ----------

// Constructor
func NewMyApp(rdb *redis.Client) *MyApp {
	app := &MyApp{
		ledger:          make(map[string]int64),
		lastBlockHeight: 0,
		appHash:         []byte("initial_app_hash"),
		redisClient:     rdb,
		pendingJobs:     make([]TransferTransaction, 0),
	}
	err := app.LoadBalancesFromDB()
	if err != nil {
		log.Printf("[INIT] Error loading balances from DB: %v", err)
	}
	return app
}

func (app *MyApp) Info(ctx context.Context, req *abci_types.InfoRequest) (*abci_types.InfoResponse, error) {
	log.Printf("[INFO] CometBFT Node connected. Version: %s, ABCIVersion: %s", req.Version, req.AbciVersion)
	return &abci_types.InfoResponse{
		Version:          "1.0.0",
		AppVersion:       1,
		LastBlockHeight:  app.lastBlockHeight,
		LastBlockAppHash: app.appHash,
	}, nil
}

func (app *MyApp) InitChain(ctx context.Context, req *abci_types.InitChainRequest) (*abci_types.InitChainResponse, error) {
	log.Println("--- [INIT CHAIN] ---")
	if len(app.ledger) == 0 {
		log.Println("[INIT CHAIN] No existing balances found, initializing defaults...")
		app.ledger["clab-century-serf1"] = 100000
		app.ledger["clab-century-serf2"] = 100000
		app.ledger["clab-century-serf3"] = 100000
		app.ledger["clab-century-serf4"] = 100000
		app.ledger["clab-century-serf5"] = 100000
		app.ledger["clab-century-serf6"] = 100000
		app.ledger["clab-century-serf7"] = 100000
		app.ledger["clab-century-serf8"] = 100000
		app.ledger["clab-century-serf9"] = 100000
		app.ledger["clab-century-serf10"] = 100000
		app.ledger["serf11"] = 100000
		app.ledger["serf12"] = 100000
		app.ledger["serf13"] = 100000
		app.ledger["serf14"] = 100000
		app.ledger["serf15"] = 100000
		app.ledger["serf16"] = 100000
		app.ledger["serf17"] = 100000
		app.ledger["serf18"] = 100000
		app.ledger["serf19"] = 100000
		app.ledger["serf20"] = 100000
		app.ledger["serf21"] = 100000
		app.ledger["serf22"] = 100000
		app.ledger["serf23"] = 100000
		app.ledger["serf24"] = 100000
		app.ledger["serf25"] = 100000

	} else {
		log.Println("[INIT CHAIN] Successfully restored balances from Pebble DB.")
	}

	app.lastBlockHeight = req.InitialHeight
	app.appHash = []byte("initial_app_hash_after_init")
	err := app.SaveBalancesToDB()
	if err != nil {
		log.Printf("[INIT] Error saving balances to DB: %v", err)
	}
	log.Printf("[INIT CHAIN] Ledger initialized with %d accounts.", len(app.ledger))
	log.Println("--- [INIT CHAIN END] ---")
	return &abci_types.InitChainResponse{}, nil
}

func (app *MyApp) CheckTx(ctx context.Context, req *abci_types.CheckTxRequest) (*abci_types.CheckTxResponse, error) {
	log.Println("--- [DRY RUN / CHECK TX START] ---")
	log.Printf("[DRY RUN] Received raw transaction: %s", string(req.Tx))
	rawTx := string(req.Tx)
	if strings.HasPrefix(rawTx, "\"") && strings.HasSuffix(rawTx, "\"") {
		rawTx = rawTx[1 : len(rawTx)-1]
	}
	decodedBytes, err := base64.StdEncoding.DecodeString(rawTx)
	if err != nil {
		log.Printf("ABCI CheckTx ERROR: Base64 decode failed: %v", err)
		return &abci_types.CheckTxResponse{Code: 1, Log: fmt.Sprintf("Base64 decode failed: %v", err)}, nil
	}
	log.Printf("ABCI CheckTx: Successfully Base64 decoded to JSON: %s", string(decodedBytes))

	var tx TransferTransaction
	if err := json.Unmarshal(decodedBytes, &tx); err != nil {
		msg := fmt.Sprintf("[DRY RUN] ERROR: Failed to parse JSON: %v", err)
		return &abci_types.CheckTxResponse{Code: 2, Log: msg}, nil
	}
	if tx.Type == "" || tx.FromNode == "" || tx.ToNode == "" || tx.Amount == "" || tx.Timestamp == "" {
		logMsg := "ABCI CheckTx ERROR: Missing one or more required fields (type, from_node, to_node, amount, timestamp)."
		log.Println(logMsg)
		return &abci_types.CheckTxResponse{Code: 4, Log: logMsg}, nil
	}

	amountStr := strings.TrimSuffix(tx.Amount, " tokens")
	amountInt, err := strconv.ParseInt(amountStr, 10, 64)
	if err != nil {
		msg := fmt.Sprintf("[DRY RUN] ERROR: Invalid amount: %s", tx.Amount)
		return &abci_types.CheckTxResponse{Code: 5, Log: msg}, nil
	}

	fromBalance, ok := app.ledger[tx.FromNode]
	if !ok {
		return &abci_types.CheckTxResponse{Code: 6, Log: fmt.Sprintf("[DRY RUN] 'from' node '%s' missing", tx.FromNode)}, nil
	}
	if fromBalance < amountInt {
		msg := fmt.Sprintf("[DRY RUN] ERROR: Insufficient funds for '%s'. Has %d, needs %d",
			tx.FromNode, fromBalance, amountInt)
		return &abci_types.CheckTxResponse{Code: 7, Log: msg}, nil
	}

	log.Printf("[DRY RUN] Transaction OK. From=%s, To=%s, Amount=%d", tx.FromNode, tx.ToNode, amountInt)
	return &abci_types.CheckTxResponse{Code: 0, Log: "Transaction format and logic OK."}, nil
}

func (app *MyApp) FinalizeBlock(ctx context.Context, req *abci_types.FinalizeBlockRequest) (*abci_types.FinalizeBlockResponse, error) {
	log.Printf("=== [EXECUTION / FINALIZE BLOCK START] (Block: %d) ===", req.Height)
	var txStrings []string
	for _, txBytes := range req.Txs {
		txStrings = append(txStrings, fmt.Sprintf("%x", txBytes))
	}
	log.Printf("ABCI : Processing transactions for block. Tx count: %d, Txs: %v", len(req.Txs), txStrings)

	app.lastBlockHeight = req.Height
	app.appHash = []byte(fmt.Sprintf("app_hash_at_height_%d", req.Height))
	txResults := make([]*abci_types.ExecTxResult, 0, len(req.Txs))
	var processedTxs []TransferTransaction

	for _, txBytes := range req.Txs {
		decodedTxBytes, err2 := base64.StdEncoding.DecodeString(string(txBytes))
		if err2 != nil {
			log.Printf("ABCI ERROR: Failed to base64 decode tx: %v, Payload: %s", err2, string(txBytes))
			txResults = append(txResults, &abci_types.ExecTxResult{
				Code: 1,
				Log:  "Failed to base64 decode tx",
			})
			continue // continue processing other txs
		}
		var tx TransferTransaction
		if err := json.Unmarshal(decodedTxBytes, &tx); err != nil {
			txResults = append(txResults, &abci_types.ExecTxResult{Code: 2, Log: "Bad JSON"})
			continue
		}
		amountStr := strings.TrimSuffix(tx.Amount, " tokens")
		amountInt, _ := strconv.ParseInt(amountStr, 10, 64)

		fromBalance := app.ledger[tx.FromNode]
		if fromBalance < amountInt {
			txResults = append(txResults, &abci_types.ExecTxResult{Code: 7, Log: "Insufficient funds"})
			continue
		}

		app.ledger[tx.FromNode] -= amountInt
		app.ledger[tx.ToNode] += amountInt
		processedTxs = append(processedTxs, tx)
		txResults = append(txResults, &abci_types.ExecTxResult{Code: 0, Log: "Executed"})
	}

	app.pendingJobs = processedTxs
	err := app.SaveBalancesToDB()
	if err != nil {
		log.Printf("[EXECUTION] ERROR: Failed to save balances to DB: %v", err)
	} // ✅ Persist ledger after block execution

	log.Printf("=== [EXECUTION / FINALIZE BLOCK END] (Block: %d) ===", req.Height)
	return &abci_types.FinalizeBlockResponse{TxResults: txResults, AppHash: app.appHash}, nil
}

func (app *MyApp) Commit(ctx context.Context, req *abci_types.CommitRequest) (*abci_types.CommitResponse, error) {
	log.Printf("+++ [COMMIT & TRIGGER START] (Block: %d) +++", app.lastBlockHeight)

	if app.redisClient != nil && len(app.pendingJobs) > 0 {
		for _, job := range app.pendingJobs {
			go app.publishToRedis(job)
		}
	} else {
		log.Printf("[TRIGGER] No pending jobs to publish.")
	}

	app.pendingJobs = nil
	log.Printf("+++ [COMMIT & TRIGGER END] (Block: %d) +++", app.lastBlockHeight)
	return &abci_types.CommitResponse{RetainHeight: 0}, nil
}

func (app *MyApp) publishToRedis(tx TransferTransaction) {
	ctx := context.Background()
	payload, _ := json.Marshal(tx)
	if err := app.redisClient.Publish(ctx, REDIS_CHANNEL, string(payload)).Err(); err != nil {
		log.Printf("[REDIS-BRIDGE ERROR] Publish failed: %v", err)
	} else {
		log.Printf("[REDIS-BRIDGE] Published off-chain trigger for %s -> %s", tx.FromNode, tx.ToNode)
	}
}

func main() {
	log.Println("--- [VERSION 7.0 - Persistent State Enabled] ---")

	rdb := redis.NewClient(&redis.Options{Addr: REDIS_ADDR})
	if err := rdb.Ping(context.Background()).Err(); err != nil {
		log.Fatalf("Fatal: Failed to connect to Redis at %s: %v", REDIS_ADDR, err)
	}
	log.Printf("Connected to Redis at %s", REDIS_ADDR)

	app := NewMyApp(rdb)
	addr := "tcp://127.0.0.1:26658"
	if len(os.Args) > 1 {
		addr = os.Args[1]
	}

	server := abci_server.NewSocketServer(addr, app)
	log.Printf("ABCI server listening on %s", addr)

	go func() {
		if err := server.Start(); err != nil {
			log.Fatalf("Error starting ABCI server: %v", err)
		}
	}()

	c := make(chan os.Signal, 1)
	signal.Notify(c, os.Interrupt, syscall.SIGTERM)
	<-c

	log.Println("Shutting down ABCI server gracefully...")
	if err := server.Stop(); err != nil {
		log.Fatalf("Error stopping ABCI server: %v", err)
	}
	log.Println("ABCI server stopped.")
}
