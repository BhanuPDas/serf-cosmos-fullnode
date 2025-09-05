package main

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/hashicorp/memberlist"
	"log"
	"net"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/hashicorp/serf/serf"
	"google.golang.org/grpc"

	pb "serfapp/pb"
)

type Config struct {
	NodeName      string
	BindAddr      string
	BindPort      int
	AdvertiseAddr string
	AdvertisePort int
	RPCAddr       string
	RPCPort       int
	Tags          map[string]string
	JoinOnStart   []string
}

type App struct {
	cfg    Config
	serf   *serf.Serf
	events chan serf.Event
	logger *log.Logger
	pb.UnimplementedSerfServiceServer
}

func loadConfig() Config {
	cfg := Config{}

	// Optional: load from JSON
	file, err := os.Open("/opt/serfapp/node.json")
	if err == nil {
		defer file.Close()
		var raw struct {
			NodeName  string `json:"node_name"`
			Bind      string `json:"bind"`
			Advertise string `json:"advertise"`
			RPCAddr   string `json:"rpc_addr"`
		}
		_ = json.NewDecoder(file).Decode(&raw)

		cfg.NodeName = raw.NodeName

		if raw.Bind != "" {
			host, portStr, err := net.SplitHostPort(raw.Bind)
			if err == nil {
				cfg.BindAddr = host
				if portStr != "" {
					if p, err := strconv.Atoi(portStr); err == nil {
						cfg.BindPort = p
					}
				}
			}
		}

		if raw.Advertise != "" {
			host, portStr, err := net.SplitHostPort(raw.Advertise)
			if err == nil {
				cfg.AdvertiseAddr = host
				if portStr != "" {
					if p, err := strconv.Atoi(portStr); err == nil {
						cfg.AdvertisePort = p
					}
				}
			}
		}

		if raw.RPCAddr != "" {
			host, portStr, err := net.SplitHostPort(raw.RPCAddr)
			if err == nil {
				cfg.RPCAddr = host
				if portStr != "" {
					if p, err := strconv.Atoi(portStr); err == nil {
						cfg.RPCPort = p
					}
				}
			}
		}
	}

	if cfg.Tags == nil {
		cfg.Tags = map[string]string{}
	}
	if cfg.JoinOnStart == nil {
		cfg.JoinOnStart = nil
	}

	return cfg
}

func (a *App) startSerf() error {
	conf := serf.DefaultConfig()
	conf.EventCh = a.events

	mlc := memberlist.DefaultLANConfig()
	mlc.BindAddr = a.cfg.BindAddr
	mlc.BindPort = a.cfg.BindPort
	if a.cfg.AdvertiseAddr != "" {
		mlc.AdvertiseAddr = a.cfg.AdvertiseAddr
	}
	if a.cfg.AdvertisePort != 0 {
		mlc.AdvertisePort = a.cfg.AdvertisePort
	}

	conf.MemberlistConfig = mlc
	conf.NodeName = a.cfg.NodeName
	conf.Tags = a.cfg.Tags

	s, err := serf.Create(conf)
	if err != nil {
		return err
	}
	a.serf = s
	a.logger.Printf("serf started: name=%s addr=%s port=%d", s.LocalMember().Name, s.LocalMember().Addr.String(), s.LocalMember().Port)
	return nil
}

func main() {
	// logging
	f, err := os.OpenFile("/var/log/serfapp.log",
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		log.Fatalf("cannot open log file: %v", err)
	}
	defer f.Close()
	logger := log.New(f, "[serf-grpc] ", log.LstdFlags|log.Lmicroseconds)

	cfg := loadConfig()
	app := &App{cfg: cfg, events: make(chan serf.Event, 512), logger: logger}

	if err := app.startSerf(); err != nil {
		logger.Fatalf("failed to start serf: %v", err)
	}

	go func() {
		for ev := range app.events {
			switch e := ev.(type) {
			case serf.MemberEvent:
				app.logger.Printf("member event: %s %v", e.Type.String(), e.Members)
			case serf.UserEvent:
				app.logger.Printf("user event: %s payload=%s", e.Name, string(e.Payload))
			default:
				app.logger.Printf("event: %#v", e)
			}
		}
	}()

	if len(cfg.JoinOnStart) > 0 {
		n, err := app.serf.Join(cfg.JoinOnStart, true)
		if err != nil {
			logger.Printf("join on start failed: %v", err)
		} else {
			logger.Printf("joined %d peers on start", n)
		}
	}

	// start gRPC
	addr := net.JoinHostPort(cfg.RPCAddr, strconv.Itoa(cfg.RPCPort))
	lis, err := net.Listen("tcp", addr)
	if err != nil {
		logger.Fatalf("failed to listen: %v", err)
	}
	grpcSrv := grpc.NewServer()
	pb.RegisterSerfServiceServer(grpcSrv, app)

	go func() {
		logger.Printf("gRPC listening on %s", lis.Addr())
		if err := grpcSrv.Serve(lis); err != nil {
			logger.Fatalf("gRPC server failed: %v", err)
		}
	}()

	// graceful shutdown
	sigch := make(chan os.Signal, 1)
	signal.Notify(sigch, syscall.SIGINT, syscall.SIGTERM)
	<-sigch
	logger.Println("shutdown signal received")

	app.serf.Leave()
	app.serf.Shutdown()
	grpcSrv.GracefulStop()
	logger.Println("stopped")
}

// --------- gRPC Methods ---------

func (a *App) Join(ctx context.Context, req *pb.JoinRequest) (*pb.JoinResponse, error) {
	if len(req.Peers) == 0 {
		return nil, errors.New("peers required")
	}
	n, err := a.serf.Join(req.Peers, true)
	if err != nil {
		return nil, err
	}
	return &pb.JoinResponse{Joined: int32(n)}, nil
}

func (a *App) Leave(ctx context.Context, req *pb.LeaveRequest) (*pb.LeaveResponse, error) {
	if err := a.serf.Leave(); err != nil {
		return nil, err
	}
	return &pb.LeaveResponse{Result: "left"}, nil
}

func (a *App) SetTags(ctx context.Context, req *pb.SetTagsRequest) (*pb.SetTagsResponse, error) {
	if err := a.serf.SetTags(req.Tags); err != nil {
		return nil, err
	}
	return &pb.SetTagsResponse{Ok: true, Tags: req.Tags}, nil
}

func (a *App) Members(ctx context.Context, req *pb.MembersRequest) (*pb.MembersResponse, error) {
	members := a.serf.Members()
	resp := &pb.MembersResponse{}
	for _, m := range members {
		resp.Members = append(resp.Members, &pb.Member{
			Name:   m.Name,
			Addr:   m.Addr.String(),
			Port:   int32(m.Port),
			Status: int32(m.Status),
			Tags:   m.Tags,
		})
	}
	return resp, nil
}

func (a *App) Query(ctx context.Context, req *pb.QueryRequest) (*pb.QueryResponse, error) {
	if req.Name == "" {
		return nil, errors.New("name required")
	}
	if req.TimeoutMs <= 0 {
		req.TimeoutMs = 2000
	}

	q, err := a.serf.Query(req.Name, []byte(req.Payload), nil)
	if err != nil {
		return nil, err
	}
	defer q.Close()

	timeout := time.NewTimer(time.Duration(req.TimeoutMs) * time.Millisecond)
	defer timeout.Stop()

	resp := &pb.QueryResponse{}
loop:
	for {
		select {
		case rply, ok := <-q.ResponseCh():
			if !ok {
				break loop
			}
			resp.Results = append(resp.Results, &pb.QueryResult{
				From:    rply.From,
				Payload: string(rply.Payload),
			})
		case <-timeout.C:
			break loop
		}
	}
	return resp, nil
}

func (a *App) Broadcast(ctx context.Context, req *pb.BroadcastRequest) (*pb.BroadcastResponse, error) {
	if req.Name == "" {
		return nil, errors.New("name required")
	}
	if err := a.serf.UserEvent(req.Name, []byte(req.Payload), req.Coalesce); err != nil {
		return nil, err
	}
	return &pb.BroadcastResponse{Result: "ok"}, nil
}
