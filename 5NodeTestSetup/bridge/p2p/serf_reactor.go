package p2p

import (
	"fmt"
	"net"
	"sync"
	"time"

	"github.com/cometbft/cometbft/config"
	"github.com/cometbft/cometbft/libs/log"
	"github.com/cometbft/cometbft/libs/service"
	"github.com/hashicorp/serf/serf"
)

const (
	SerfReactorName = "SerfReactor"
	SerfDiscoveryInterval = 5 * time.Second // How often to check Serf members
)

// SerfReactor implements Reactor for Serf-based peer discovery.
type SerfReactor struct {
	service.BaseService
	config     *config.P2PConfig
	logger     log.Logger
	serfAgent  *serf.Serf
	nodeKey    *NodeKey // CometBFT's node key, needed to identify self
	peerAddrs  chan PeerAddress // Channel to send discovered peers to the Switch
	peersMutex sync.Mutex
	knownPeers map[ID]PeerAddress // Keep track of peers known by CometBFT
}

// NewSerfReactor creates a new SerfReactor.
func NewSerfReactor(p2pConfig *config.P2PConfig, nodeKey *NodeKey, logger log.Logger) *SerfReactor {
	sr := &SerfReactor{
		config:     p2pConfig,
		logger:     logger.
			With("module", "p2p").
			With("reactor", SerfReactorName),
		nodeKey:    nodeKey,
		peerAddrs:  make(chan PeerAddress, 100), // Buffered channel
		knownPeers: make(map[ID]PeerAddress),
	}
	sr.BaseService = *service.NewBaseService(logger, SerfReactorName, sr)
	return sr
}

// OnStart implements service.Service.
func (sr *SerfReactor) OnStart() error {
	// 1. Initialize Serf Agent
	serfConfig := serf.DefaultConfig()
	serfConfig.NodeName = string(sr.nodeKey.ID()) // Use CometBFT NodeID as Serf NodeName
	serfConfig.BindAddr = sr.config.ListenAddress // Serf will bind to the same address as CometBFT P2P
	serfConfig.Logger = sr.logger.With("component", "serf-agent") // Use CometBFT logger

	// Parse the listen address for the Serf bind port
	_, serfPortStr, err := net.SplitHostPort(sr.config.ListenAddress)
	if err != nil {
		return fmt.Errorf("failed to parse CometBFT listen address for Serf: %w", err)
	}
	serfPort, err := net.ParsePort(serfPortStr)
	if err != nil {
		return fmt.Errorf("failed to parse Serf port from CometBFT listen address: %w", err)
	}
	serfConfig.BindPort = serfPort

	// Optional: Use a separate port for Serf if you don't want it on the same as CometBFT P2P
	// For simplicity, we're using the same.
	// You might also want to set Serf's RPC address if you need to control it externally.

	agent, err := serf.Create(serfConfig)
	if err != nil {
		return fmt.Errorf("failed to create Serf agent: %w", err)
	}
	sr.serfAgent = agent

	// 2. Join Serf Cluster (if join addresses are provided)
	if len(sr.config.PersistentPeers) > 0 { // Re-purpose PersistentPeers for Serf initial join
		serfJoinAddrs := make([]string, 0, len(sr.config.PersistentPeers))
		for _, peer := range sr.config.PersistentPeers {
			// Assuming PersistentPeers are in format "NodeID@IP:Port"
			// Extract IP:Port for Serf join
			_, host, port, err := SplitIDPortAddr(string(peer))
			if err != nil {
				sr.logger.Error("Failed to parse persistent peer address for Serf join", "peer", peer, "err", err)
				continue
			}
			serfJoinAddrs = append(serfJoinAddrs, net.JoinHostPort(host.String(), port))
		}

		if len(serfJoinAddrs) > 0 {
			_, err = sr.serfAgent.Join(serfJoinAddrs, true)
			if err != nil {
				sr.logger.Error("Failed to join Serf cluster, starting own", "err", err)
			} else {
				sr.logger.Info("Successfully joined Serf cluster", "addrs", serfJoinAddrs)
			}
		}
	} else {
		sr.logger.Info("No Serf join addresses provided, starting a new Serf cluster")
	}

	// 3. Start goroutines to monitor Serf events and update CometBFT peers
	go sr.monitorSerfMembers()

	return nil
}

// OnStop implements service.Service.
func (sr *SerfReactor) OnStop() {
	if sr.serfAgent != nil {
		if err := sr.serfAgent.Leave(); err != nil {
			sr.logger.Error("Failed to leave Serf cluster", "err", err)
		}
		if err := sr.serfAgent.Shutdown(); err != nil {
			sr.logger.Error("Failed to shut down Serf agent", "err", err)
		}
	}
	close(sr.peerAddrs)
	sr.logger.Info("SerfReactor stopped")
}

// Get , Set, InitPeer, AddPeer, RemovePeer, Receive methods of Reactor interface
// These will mostly be no-ops or delegate to the Switch, as SerfReactor's
// primary role is discovery, not direct peer management.

// The Switch will call this, but the SerfReactor doesn't actively send/receive
// messages for consensus directly. Its purpose is to provide the peer list.
func (sr *SerfReactor) Receive(e Envelope) {
	// No-op for now, as SerfReactor is for discovery.
	// If you wanted to use Serf for actual gossip of CometBFT messages,
	// this would be the place to handle incoming Serf user events.
}

// PeerUpdates returns a channel for peer addresses. The Switch will read from this.
func (sr *SerfReactor) PeerUpdates() <-chan PeerAddress {
	return sr.peerAddrs
}

// monitorSerfMembers periodically queries Serf for members and updates CometBFT's peer list.
func (sr *SerfReactor) monitorSerfMembers() {
	ticker := time.NewTicker(SerfDiscoveryInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			sr.updateCometBFTPeers()
		case <-sr.Quit():
			return
		}
	}
}

// updateCometBFTPeers fetches Serf members and sends updates to the CometBFT P2P Switch.
func (sr *SerfReactor) updateCometBFTPeers() {
	members := sr.serfAgent.Members()
	if len(members) == 0 {
		sr.logger.Debug("Serf has no members yet")
		return
	}

	sr.peersMutex.Lock()
	defer sr.peersMutex.Unlock()

	currentSerfPeers := make(map[ID]struct{})

	for _, member := range members {
		// Ignore self
		if member.Name == string(sr.nodeKey.ID()) {
			continue
		}
		// Only consider alive members
		if member.Status != serf.StatusAlive {
			if _, ok := sr.knownPeers[ID(member.Name)]; ok {
				// If a peer was previously known and is now not alive, signal removal
				sr.logger.Info("Serf member not alive, removing from CometBFT peers", "node", member.Name, "status", member.Status.String())
				delete(sr.knownPeers, ID(member.Name))
				// Send a removal event (conceptual, CometBFT doesn't have a direct "remove peer" channel for reactors)
				// The Switch's internal logic will eventually prune disconnected peers.
				// For now, we only focus on adding new/updated peers.
			}
			continue
		}

		// Use Serf's NodeName as CometBFT's NodeID
		peerID := ID(member.Name)
		peerAddr := PeerAddress{
			ID:   peerID,
			IP:   member.Addr,
			Port: int(member.Port), // Assuming Serf gossip port is the CometBFT P2P port
		}

		currentSerfPeers[peerID] = struct{}{}

		// Check if CometBFT already knows about this peer
		if existingPeer, ok := sr.knownPeers[peerID]; !ok || !existingPeer.Equal(peerAddr) {
			sr.logger.Info("New or updated Serf member discovered, adding to CometBFT peers",
				"node", member.Name,
				"addr", peerAddr.DialString(),
			)
			sr.knownPeers[peerID] = peerAddr
			select {
			case sr.peerAddrs <- peerAddr:
				// Successfully sent peer address
			default:
				sr.logger.Warn("Peer discovery channel full, dropping new peer address", "peer", peerAddr.DialString())
			}
		}
	}

	// Optional: Handle peers that were previously known by CometBFT but are no longer in Serf's alive list
	// This would require a more sophisticated diffing mechanism. For simplicity, CometBFT's own P2P layer
	// will eventually drop connections to non-responding peers.
}

// SplitIDPortAddr is a helper to parse "id@ip:port" strings (simplified)
func SplitIDPortAddr(addr string) (ID, net.IP, string, error) {
	parts := splitIDHostPort(addr)
	if len(parts) != 2 {
		return "", nil, "", fmt.Errorf("invalid address format: %s", addr)
	}

	nodeID := ID(parts[0])
	host, port, err := net.SplitHostPort(parts[1])
	if err != nil {
		return "", nil, "", fmt.Errorf("invalid host:port in address: %s", parts[1])
	}
	ip := net.ParseIP(host)
	if ip == nil {
		// Attempt DNS lookup if not an IP (Serf might return DNS names)
		addrs, dnsErr := net.LookupHost(host)
		if dnsErr != nil || len(addrs) == 0 {
			return "", nil, "", fmt.Errorf("could not resolve host %s: %w", host, dnsErr)
		}
		ip = net.ParseIP(addrs[0]) // Take the first resolved IP
		if ip == nil {
			return "", nil, "", fmt.Errorf("failed to parse IP after DNS lookup for %s", host)
		}
	}
	return nodeID, ip, port, nil
}

// splitIDHostPort splits "id@ip:port" into "id" and "ip:port"
func splitIDHostPort(s string) []string {
	for i := 0; i < len(s); i++ {
		if s[i] == '@' {
			return []string{s[:i], s[i+1:]}
		}
	}
	return []string{s}
}
