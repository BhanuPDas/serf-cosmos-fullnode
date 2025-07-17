package p2p

import (
	"context"
	"fmt"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/cometbft/cometbft/config"
	"github.com/cometbft/cometbft/libs/log"
	"github.com/cometbft/cometbft/libs/service"
	
	// IMPORTANT: Replace "github.com/yourusername/cometbft" with the actual module path of your CometBFT fork.
	// For example, if your go.mod says "module github.com/shuddhank/cometbft", use that.
	pb "github.com/shuddhank/cometbft/p2p/serf_ipc" 
	
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure" // For simplicity during local testing. Use TLS in production!
)

const (
	SerfReactorName       = "SerfReactor"
	SerfDiscoveryInterval = 5 * time.Second // How often to poll (if not streaming)
	SerfRPCTimeout        = 3 * time.Second // Timeout for gRPC calls
)

// SerfReactor implements Reactor for Serf-based peer discovery via IPC.
type SerfReactor struct {
	service.BaseService
	config          *config.P2PConfig
	logger          log.Logger
	nodeKey         *NodeKey // CometBFT's node key, needed to identify self
	peerAddrs       chan PeerAddress // Channel to send discovered peers to the Switch
	peersMutex      sync.Mutex
	knownCometPeers map[ID]PeerAddress // Peers CometBFT knows about (from Serf's perspective)
	serfRPCAddr     string             // Address of the Rust Serf RPC server (e.g., "127.0.0.1:8080")

	grpcClient pb.SerfDiscoveryServiceClient
	grpcConn   *grpc.ClientConn
}

// NewSerfReactor creates a new SerfReactor.
func NewSerfReactor(p2pConfig *config.P2PConfig, nodeKey *NodeKey, logger log.Logger, serfRPCAddr string) *SerfReactor {
	sr := &SerfReactor{
		config:          p2pConfig,
		logger:          logger.With("module", "p2p").With("reactor", SerfReactorName),
		nodeKey:         nodeKey,
		peerAddrs:       make(chan PeerAddress, 100), // Buffered channel to avoid blocking Serf events
		knownCometPeers: make(map[ID]PeerAddress),
		serfRPCAddr:     serfRPCAddr,
	}
	sr.BaseService = *service.NewBaseService(logger, SerfReactorName, sr)
	return sr
}

// OnStart implements service.Service.
func (sr *SerfReactor) OnStart() error {
	sr.logger.Info("Starting SerfReactor (IPC Client)", "serf_rpc_addr", sr.serfRPCAddr)

	// 1. Establish gRPC connection to the Rust Serf daemon
	var err error
	// In a real application, consider TLS for production!
	sr.grpcConn, err = grpc.NewClient(sr.serfRPCAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return fmt.Errorf("failed to dial Serf RPC server at %s: %w", sr.serfRPCAddr, err)
	}
	sr.grpcClient = pb.NewSerfDiscoveryServiceClient(sr.grpcConn)

	// 2. Start goroutine to monitor Serf members via RPC streaming
	// This is generally more efficient than polling.
	go sr.monitorSerfMembersViaStreaming()

	return nil
}

// OnStop implements service.Service.
func (sr *SerfReactor) OnStop() {
	sr.logger.Info("Stopping SerfReactor")
	if sr.grpcConn != nil {
		if err := sr.grpcConn.Close(); err != nil {
			sr.logger.Error("Failed to close gRPC connection to Serf RPC", "err", err)
		}
	}
	// Close the channel to signal to the Switch that no more peers will be sent.
	close(sr.peerAddrs) 
}

// Get , Set, InitPeer, AddPeer, RemovePeer, Receive methods of Reactor interface
// These will mostly be no-ops or delegate to the Switch, as SerfReactor's
// primary role is discovery, not direct peer management or message passing.

// Receive is part of the Reactor interface, but for a discovery reactor, it's typically a no-op.
func (sr *SerfReactor) Receive(e Envelope) {
	// No-op for a discovery reactor.
	// If you wanted to use Serf for actual gossip of CometBFT messages,
	// this would be the place to handle incoming Serf user events (if exposed via RPC).
}

// PeerUpdates returns a channel for peer addresses. The Switch will read from this to connect.
func (sr *SerfReactor) PeerUpdates() <-chan PeerAddress {
	return sr.peerAddrs
}

// monitorSerfMembersViaStreaming connects to the Serf daemon and streams member events.
// This is the preferred method for reactive updates.
func (sr *SerfReactor) monitorSerfMembersViaStreaming() {
	for {
		// Ensure a new context for each stream attempt
		ctx, cancel := context.WithCancel(context.Background())
		
		// Initial sync: Get all current members before starting the stream
		// This handles cases where members join before the stream is established
		resp, err := sr.grpcClient.GetMembers(ctx, &pb.GetMembersRequest{})
		if err != nil {
			sr.logger.Error("Failed initial sync of Serf members via RPC, retrying...", "err", err)
			cancel() // Cancel the context for this attempt
			select {
			case <-time.After(SerfDiscoveryInterval):
			case <-sr.Quit():
				return
			}
			continue
		}
		sr.logger.Info("Initial sync of Serf members complete", "count", len(resp.Members))
		sr.processMembers(resp.Members) // Process existing members

		// Now, open the streaming connection
		stream, err := sr.grpcClient.StreamMemberEvents(ctx, &pb.StreamMemberEventsRequest{})
		if err != nil {
			sr.logger.Error("Failed to open Serf member event stream, retrying...", "err", err)
			cancel() // Cancel the context for this attempt
			select {
			case <-time.After(SerfDiscoveryInterval):
			case <-sr.Quit():
				return
			}
			continue
		}

		sr.logger.Info("Serf member event stream opened")
		
		for {
			event, err := stream.Recv()
			if err != nil {
				sr.logger.Error("Error receiving Serf member event, restarting stream...", "err", err)
				break // Break inner loop, outer loop will try to re-establish stream
			}
			sr.processMemberEvent(event)
		}

		// Cleanup context and check if reactor is quitting before retrying stream
		cancel()
		select {
		case <-sr.Quit():
			return // Reactor is stopping, exit goroutine
		default:
			sr.logger.Info("Stream closed, attempting to re-establish Serf member stream after delay...")
			time.Sleep(SerfDiscoveryInterval) // Wait before retrying to avoid tight loop
		}
	}
}

// processMemberEvent handles a single member event received from the Serf daemon.
func (sr *SerfReactor) processMemberEvent(event *pb.StreamMemberEventsResponse) {
	if event.GetMember() == nil {
		sr.logger.Warn("Received Serf member event with no member data", "event_type", event.EventType.String())
		return
	}

	member := event.GetMember()
	peerID := ID(member.Id)

	// Ignore self, unless it's a specific self-update you want to log
	if peerID == sr.nodeKey.ID() {
		sr.logger.Debug("Received Serf event for self", "event", event.EventType.String())
		return
	}

	sr.peersMutex.Lock()
	defer sr.peersMutex.Unlock()

	switch event.EventType {
	case pb.StreamMemberEventsResponse_JOIN, pb.StreamMemberEventsResponse_UPDATE:
		// Only consider adding/updating if the member is reported as "alive"
		if member.Status == "alive" {
			ip := net.ParseIP(member.Address)
			if ip == nil {
				sr.logger.Error("Failed to parse IP address from Serf member", "address", member.Address, "node", member.Id)
				return
			}
			peerAddr := PeerAddress{
				ID:   peerID,
				IP:   ip,
				Port: int(member.Port),
			}

			// Add or update the peer if it's new or its address has changed
			if existingPeer, ok := sr.knownCometPeers[peerID]; !ok || !existingPeer.Equal(peerAddr) {
				sr.logger.Info("New or updated Serf member discovered",
					"node", member.Id,
					"addr", peerAddr.DialString(),
					"event", event.EventType.String(),
				)
				sr.knownCometPeers[peerID] = peerAddr
				// Send to the Switch's PeerUpdates channel
				select {
				case sr.peerAddrs <- peerAddr:
					// Successfully sent peer address
				default:
					sr.logger.Warn("Peer discovery channel full, dropping new peer address", "peer", peerAddr.DialString())
				}
			}
		} else {
			// If a member is not alive but known, it might have transitioned to failed/left,
			// or was previously alive and now isn't. Log and ensure it's not in knownCometPeers.
			if _, ok := sr.knownCometPeers[peerID]; ok {
				sr.logger.Info("Serf member not alive, removing from known peers", "node", member.Id, "status", member.Status)
				delete(sr.knownCometPeers, peerID)
			}
		}

	case pb.StreamMemberEventsResponse_LEAVE, pb.StreamMemberEventsResponse_FAIL:
		// If a peer leaves or fails, remove it from our internal tracking map.
		// CometBFT's P2P Switch will handle the actual disconnection/reconnection
		// based on its own connection management.
		if _, ok := sr.knownCometPeers[peerID]; ok {
			sr.logger.Info("Serf member left or failed, removing from known list",
				"node", member.Id,
				"event", event.EventType.String(),
			)
			delete(sr.knownCometPeers, peerID)
		}

	default:
		sr.logger.Debug("Unhandled Serf member event type", "event_type", event.EventType.String(), "member", member.Id)
	}
}

// processMembers is used for initial sync or polling, iterating over a list of members.
func (sr *SerfReactor) processMembers(members []*pb.Member) {
	sr.peersMutex.Lock()
	defer sr.peersMutex.Unlock()

	currentSerfMembersMap := make(map[ID]struct{}) // Track members currently reported by Serf

	for _, member := range members {
		peerID := ID(member.Id)

		// Ignore self
		if peerID == sr.nodeKey.ID() {
			continue
		}

		// Only consider alive members for adding
		if member.Status == "alive" {
			ip := net.ParseIP(member.Address)
			if ip == nil {
				sr.logger.Error("Failed to parse IP address from Serf member (initial sync)", "address", member.Address, "node", member.Id)
				continue
			}
			peerAddr := PeerAddress{
				ID:   peerID,
				IP:   ip,
				Port: int(member.Port),
			}

			currentSerfMembersMap[peerID] = struct{}{}

			// Add or update the peer if it's new or its address has changed
			if existingPeer, ok := sr.knownCometPeers[peerID]; !ok || !existingPeer.Equal(peerAddr) {
				sr.logger.Info("New or updated Serf member discovered (initial sync)",
					"node", member.Id,
					"addr", peerAddr.DialString(),
				)
				sr.knownCometPeers[peerID] = peerAddr
				select {
				case sr.peerAddrs <- peerAddr:
					// Successfully sent peer address
				default:
					sr.logger.Warn("Peer discovery channel full (initial sync), dropping new peer address", "peer", peerAddr.DialString())
				}
			}
		} else {
			// If a member is not alive, ensure it's removed from our known list if it was there
			if _, ok := sr.knownCometPeers[peerID]; ok {
				sr.logger.Info("Serf member no longer alive during sync, removing from known peers", "node", member.Id, "status", member.Status)
				delete(sr.knownCometPeers, peerID)
			}
		}
	}

	// Remove any peers from knownCometPeers that are no longer reported by Serf as alive.
	// This helps prune stale entries in case streaming events were missed or for polling mode.
	for knownID := range sr.knownCometPeers {
		if _, found := currentSerfMembersMap[knownID]; !found {
			sr.logger.Info("Known CometBFT peer not in current Serf alive list, removing", "node", knownID)
			delete(sr.knownCometPeers, knownID)
		}
	}
}

// monitorSerfMembersViaPolling is an alternative to streaming, less efficient but simpler to reason about.
// You can uncomment this and use go sr.monitorSerfMembersViaPolling() in OnStart if you prefer.
/*
func (sr *SerfReactor) monitorSerfMembersViaPolling() {
	ticker := time.NewTicker(SerfDiscoveryInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(context.Background(), SerfRPCTimeout)
			resp, err := sr.grpcClient.GetMembers(ctx, &pb.GetMembersRequest{})
			cancel() // Ensure context is cancelled
			if err != nil {
				sr.logger.Error("Failed to get Serf members via RPC (polling)", "err", err)
				continue
			}
			sr.processMembers(resp.Members)
		case <-sr.Quit():
			return
		}
	}
}
*/

```
