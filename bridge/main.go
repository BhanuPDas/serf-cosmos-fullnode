package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	abcitypes "github.com/cometbft/cometbft/abci/types"
	cmn "github.com/cometbft/cometbft/libs/bytes"
	cometbftmempool "github.com/cometbft/cometbft/mempool" // For the Mempool interface and TxInfo
	cometbfttypes "github.com/cometbft/cometbft/types"
	"github.com/youruser/serf-comet-bridge/p2p" 
)

type MockMempool struct{}

func (mm *MockMempool) CheckTx(
	tx cometbfttypes.Tx,
	cb func(*abcitypes.ResponseCheckTx), // Callback for the actual CheckTx response
	txInfo cometbftmempool.TxInfo,      // Information about the transaction
) error {
	// Log the received transaction. TxInfo does not have a public 'Sender' field.
	log.Printf("Mock Mempool received transaction: %s (TxInfo: %+v)", cmn.HexBytes(tx).String(), txInfo)

	// Simulate a successful check for demonstration
	response := &abcitypes.ResponseCheckTx{
		Code: 0, // 0 indicates success
		Log:  fmt.Sprintf("Tx %s received by mock mempool at %s", cmn.HexBytes(tx).String(), time.Now().Format(time.RFC3339)),
	}

	// Call the provided callback with the simulated response
	cb(response)

	// CheckTx itself returns an error if the submission failed, not the ABCI response
	return nil
}

// ReapMaxBytesMaxGas is a stub implementation for the Mempool interface.
func (mm *MockMempool) ReapMaxBytesMaxGas(maxBytes, maxGas int64) cometbfttypes.Txs {
	log.Println("Mock Mempool: ReapMaxBytesMaxGas called")
	return nil // Return empty transactions
}

// ReapMaxTxs is a stub implementation for the Mempool interface.
func (mm *MockMempool) ReapMaxTxs(max int) cometbfttypes.Txs {
	log.Println("Mock Mempool: ReapMaxTxs called")
	return nil // Return empty transactions
}

// Update is a stub implementation for the Mempool interface.
func (mm *MockMempool) Update(
	height int64,
	txs cometbfttypes.Txs,
	txResults []*abcitypes.ExecTxResult,
	preCheck cometbftmempool.PreCheckFunc,
	postCheck cometbftmempool.PostCheckFunc,
) error {
	log.Printf("Mock Mempool: Update called for height %d with %d txs", height, len(txs))
	return nil
}

// Flush is a stub implementation for the Mempool interface.
func (mm *MockMempool) Flush() {
	log.Println("Mock Mempool: Flush called")
}

// FlushAppConn is a stub implementation for the Mempool interface.
func (mm *MockMempool) FlushAppConn() error {
	log.Println("Mock Mempool: FlushAppConn called")
	return nil
}

// TxsAvailable is a stub implementation for the Mempool interface.
// It returns a channel that can be used to signal transaction availability.
func (mm *MockMempool) TxsAvailable() <-chan struct{} {
	log.Println("Mock Mempool: TxsAvailable called")
	// For a mock, we can return a closed channel to immediately signal availability,
	// or a new channel that's never signaled if not needed.
	ch := make(chan struct{})
	close(ch) // Immediately signal that txs are available (for testing purposes)
	return ch
}

// EnableTxsAvailable is a stub implementation for the Mempool interface.
func (mm *MockMempool) EnableTxsAvailable() {
	log.Println("Mock Mempool: EnableTxsAvailable called")
}

// Size is a stub implementation for the Mempool interface.
func (mm *MockMempool) Size() int {
	log.Println("Mock Mempool: Size called")
	return 0 // Return 0 transactions
}

// SizeBytes is a stub implementation for the Mempool interface.
func (mm *MockMempool) SizeBytes() int64 {
	log.Println("Mock Mempool: SizeBytes called")
	return 0 // Return 0 bytes
}

// Lock is a stub implementation for the Mempool interface.
func (mm *MockMempool) Lock() {
	log.Println("Mock Mempool: Lock called")
}

// Unlock is a stub implementation for the Mempool interface.
func (mm *MockMempool) Unlock() {
	log.Println("Mock Mempool: Unlock called")
}

// RemoveTxByKey is a stub implementation for the Mempool interface.
func (mm *MockMempool) RemoveTxByKey(txKey cometbfttypes.TxKey) error {
	log.Printf("Mock Mempool: RemoveTxByKey called for key: %+v", txKey) // Print the struct directly
	return nil
}


// Ensure MockMempool satisfies the cometbftmempool.Mempool interface
var _ cometbftmempool.Mempool = (*MockMempool)(nil)

// MockNode simulates a CometBFT node for testing purposes.
type MockNode struct{}

// Mempool returns a mock mempool instance.
func (mn *MockNode) Mempool() cometbftmempool.Mempool {
	return &MockMempool{}
}

// initializeCometNode is a placeholder for your actual CometBFT node initialization logic.
func initializeCometNode() *MockNode {
	log.Println("Initializing mock CometBFT node...")
	return &MockNode{}
}

// --- End Mocks ---

func main() {
	serfRPCAddr := flag.String("serf-addr", "10.0.1.11:7373", "Serf RPC address")
	flag.Parse()

	node := initializeCometNode()
	mp := node.Mempool()

	log.Printf("Initializing SerfAdapter with Serf RPC: %s and CometBFT Mempool", *serfRPCAddr)

	adapter, err := p2p.NewSerfAdapter(*serfRPCAddr, mp)
	if err != nil {
		log.Fatalf("SerfAdapter init failed: %v", err)
	}

	log.Printf("Listening for Serf events on %s ...", *serfRPCAddr)
	go adapter.Start()

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	s := <-sigChan
	log.Printf("Received signal %s. Shutting down...", s)

	log.Println("Application gracefully stopped.")
}

