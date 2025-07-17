# 5 Nodes Topology with Serf & Containerlab

This project sets up a 5-node network topology using [Containerlab](https://containerlab.dev), along with Serf agents for decentralized cluster membership and node communication. It's useful for experimenting with service discovery, failure detection, and distributed coordination in lab environments.

---

## 🗂️ Project Structure

- `ceso5node.yml` — Main Containerlab topology definition file.
- `clab-century/` — Contains node-specific configs, TLS materials, inventory, and logs.
- `setup_nodes.sh` — Orchestrates the setup of nodes.
- `ipaddressing.sh` — Assigns IP addresses to nodes.
- `serf_agents_start.sh` — Starts Serf agents.
- `serf_agents_joining.sh` — Manages the joining of nodes to the Serf cluster.
- `serf/` —  Contains Serf binary .
- `pytogoapi/` —  Web Server API for Python (custom implementation).

---

## 🚀 Getting Started

### Prerequisites

- Docker & Containerlab installed
- Bash or Linux shell
- Git (for cloning and version control)

### Setup Steps

```bash
# Step 1: Clone the repo
git clone https://github.com/anjumm/gopyserf.git

# Step 2: Deploy the topology
sudo containerlab deploy -t ceso5node.yml

# Step 3: Assign IPs
./ipaddressing.sh

# Step 3: Setting Nodes 
./setup_nodes.sh

# Step 4: Start Serf agents
./serf_agents_start.sh

# Step 5: Join nodes into the cluster
./serf_agents_joining.sh
