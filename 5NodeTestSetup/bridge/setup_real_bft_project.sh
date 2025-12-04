#!/bin/bash

if [ -d "main.go" ]; then
    BACKUP_DIR="project_simulation_backup_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    mv *.go *.sh Dockerfile* webapp/ go.mod go.sum "$BACKUP_DIR/" > /dev/null 2>&1
fi

go mod init my-cometbft-app

cat > main.go << 'EOF'
package main

import (
	"context"
	"log"

	"github.com/cometbft/cometbft/abci/server"
	"github.com/cometbft/cometbft/abci/types"
)

type ABCIApplication struct {
	types.BaseApplication
}

func NewABCIApplication() *ABCIApplication {
	return &ABCIApplication{}
}

func (app *ABCIApplication) Info(ctx context.Context, req *types.RequestInfo) (*types.ResponseInfo, error) {
	return &types.ResponseInfo{}, nil
}

func (app *ABCIApplication) DeliverTx(ctx context.Context, req *types.RequestDeliverTx) (*types.ResponseDeliverTx, error) {
	txData := string(req.Tx)
	log.Printf("[APP] Received and processing transaction: %s", txData)
	return &types.ResponseDeliverTx{Code: 0}, nil
}

func (app *ABCIApplication) CheckTx(ctx context.Context, req *types.RequestCheckTx) (*types.ResponseCheckTx, error) {
	return &types.ResponseCheckTx{Code: 0}, nil
}

func (app *ABCIApplication) Commit(ctx context.Context, req *types.RequestCommit) (*types.ResponseCommit, error) {
	log.Println("[APP] Committing new block to state.")
	return &types.ResponseCommit{}, nil
}

func main() {
	app := NewABCIApplication()
	srv, err := server.NewServer("tcp://0.0.0.0:26658", "socket", app)
	if err != nil {
		log.Fatalf("Failed to create ABCI server: %v", err)
	}
	if err := srv.Start(context.Background()); err != nil {
		log.Fatalf("Failed to start ABCI server: %v", err)
	}
	srv.Wait()
}
EOF

cat > Dockerfile << 'EOF'
FROM golang:1.24-alpine
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o /go-app .
EXPOSE 26658
CMD ["/go-app"]
EOF

go get github.com/cometbft/cometbft@v0.38.5
