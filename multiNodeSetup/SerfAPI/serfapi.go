package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/hashicorp/serf/client"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func StartHTTPServer(ctx context.Context, listenAddr string, rpcAddr string) error {
	// Create RPC client
	c, err := client.ClientFromConfig(&client.Config{Addr: rpcAddr})
	if err != nil {
		return fmt.Errorf("failed to create RPC client: %w", err)
	}
	// if we exit, close client
	go func() {
		<-ctx.Done()
		_ = c.Close()
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("/members", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		members, err := c.Members()
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		writeJSON(w, members)
	})

	mux.HandleFunc("/updatetags", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			Tags       map[string]string `json:"tags"`
			DeleteTags []string          `json:"delete_tags"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "invalid json: "+err.Error(), http.StatusBadRequest)
			return
		}
		if req.Tags == nil && len(req.DeleteTags) == 0 {
			http.Error(w, "nothing to update", http.StatusBadRequest)
			return
		}
		if err := c.UpdateTags(req.Tags, req.DeleteTags); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})

	mux.HandleFunc("/query", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}

		// Decode request payload
		var req struct {
			Name       string            `json:"name"`
			Payload    string            `json:"payload"`
			FilterTags map[string]string `json:"filter_tags,omitempty"`
			Timeout    int               `json:"timeout_sec,omitempty"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "invalid json: "+err.Error(), http.StatusBadRequest)
			return
		}

		respCh := make(chan client.NodeResponse, 10)
		ackCh := make(chan string, 10)

		qp := &client.QueryParam{
			Name:       req.Name,
			Payload:    []byte(req.Payload),
			FilterTags: req.FilterTags,
			RequestAck: true,
			Timeout:    time.Duration(maxInt(time.Duration(req.Timeout)*time.Second, 5)) * time.Second,
			AckCh:      ackCh,
			RespCh:     respCh,
		}

		if err := c.Query(qp); err != nil {
			http.Error(w, "query failed: "+err.Error(), http.StatusInternalServerError)
			return
		}

		collectCtx, cancel := context.WithTimeout(r.Context(), qp.Timeout)
		defer cancel()

		acks := []string{}
		responses := []struct {
			From    string `json:"from"`
			Payload string `json:"payload_b64"`
		}{}

		for {
			select {
			case a, ok := <-ackCh:
				if !ok {
					ackCh = nil
				} else {
					acks = append(acks, a)
				}
			case nr, ok := <-respCh:
				if !ok {
					respCh = nil
				} else {
					responses = append(responses, struct {
						From    string `json:"from"`
						Payload string `json:"payload_b64"`
					}{
						From:    nr.From,
						Payload: base64.StdEncoding.EncodeToString(nr.Payload),
					})
				}
			case <-collectCtx.Done():
				if collectCtx.Err() == context.DeadlineExceeded {
					http.Error(w, "query timed out", http.StatusGatewayTimeout)
					return
				}
				http.Error(w, "request canceled", http.StatusRequestTimeout)
				return
			}

			if ackCh == nil && respCh == nil {
				break
			}
		}

		writeJSON(w, map[string]interface{}{"acks": acks, "responses": responses})
	})

	httpSrv := &http.Server{
		Addr:    listenAddr,
		Handler: mux,
	}

	// run server in a goroutine and wait for context cancellation
	errCh := make(chan error, 1)
	go func() {
		log.Printf("HTTP server listening on %s", listenAddr)
		errCh <- httpSrv.ListenAndServe()
	}()

	select {
	case <-ctx.Done():
		// shutdown server gracefully
		sctx, scancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer scancel()
		_ = httpSrv.Shutdown(sctx)
		return nil
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func maxInt(d time.Duration, fallback int) int {
	if d > 0 {
		return int(d / time.Second)
	}
	return fallback
}

func main() {
	rpcAddr := "127.0.0.1:7373"
	listen := "0.0.0.0:5555"

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// Start server (blocks until ctx is done or server fails)
	if err := StartHTTPServer(ctx, listen, rpcAddr); err != nil {
		log.Fatalf("server failed: %v", err)
	}

	// give a moment to shutdown
	time.Sleep(100 * time.Millisecond)
	log.Println("server stopped")
}
