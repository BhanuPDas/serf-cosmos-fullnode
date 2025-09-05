#!/bin/bash

# List of containers (Ubuntu nodes for 100 nodes)
containers=()
for i in {1..5}; do
  containers+=(clab-century-serf$i)
done

# Paths and file names
json_file="node.json"
destination_dir="/opt/serfapp"

declare -a ip_list=()

# Function to set up Ubuntu nodes
setup_ubuntu_nodes() {
  for i in "${!containers[@]}"; do
    container="${containers[$i]}"
    
    # Check if container is running
    if ! docker ps --format '{{.Names}}' | grep -q "$container"; then
      echo "Container $container is not running, skipping..."
      continue
    fi
    
    echo "Setting up $container..."

    # Get the IP address of the eth1 interface directly from within the container
    ip_address=$(docker exec "$container" ip -4 addr show eth1 | grep -oP '(?<=inet\s)10\.0\.1\.\d+')
    if [ -z "$ip_address" ]; then
      echo "Failed to retrieve IP address for $container"
      continue
    fi
    
    echo "IP address for $container (eth1): $ip_address"
    docker exec "$container" sysctl -w net.ipv6.conf.all.disable_ipv6=1
    
    # Generate the JSON configuration file dynamically
    json_content=$(cat <<EOF
{
  "node_name": "$container",
  "bind": "0.0.0.0:7946",
  "advertise": "$ip_address:7946",
  "rpc_addr": "0.0.0.0:7373"
}
EOF
)
    # Create a temporary JSON file on the host
    temp_json_file=$(mktemp)
    echo "$json_content" > "$temp_json_file"

    # Create the destination directory inside the container
    docker exec "$container" mkdir -p "$destination_dir"

    # Copy the generated JSON file and serf binary into the /opt/serfapp/ directory
    docker cp "$temp_json_file" "$container":"$destination_dir/node.json" || { echo "Failed to copy node.json to $container"; exit 1; }

    # Remove the temporary JSON file
    rm "$temp_json_file"

    #Create IP lists for all nodes
    if (( i >= 2 )); then
        ip_list+=("$ip_address:7946")
    fi
    
    # Install Go 
    echo "Installing Go..."
    docker cp "$HOME/cometbftconfig/go1.25.0.linux-amd64.tar.gz" "$container":/root/ || { echo "Failed to copy go file to $container"; exit 1; }
    docker exec "$container" bash -c "rm -rf /usr/local/go && tar -C /usr/local -xzf /root/go1.25.0.linux-amd64.tar.gz"
    goVersion=$(docker exec "$container" /usr/local/go/bin/go version)
    echo "$goVersion installation complete."
    
    # Configure Serf API
    echo "Configuring Serf API..."
    docker cp "$HOME/cometbftconfig/go.mod" "$container":/root/ || { echo "Failed to copy go mod file to $container"; exit 1; }
    docker cp "$HOME/cometbftconfig/serfapi.go" "$container":/root/ || { echo "Failed to copy serfapi go file to $container"; exit 1; }
    docker cp "$HOME/cometbftconfig/serf.proto" "$container":/root/ || { echo "Failed to copy serf proto file to $container"; exit 1; }
    docker exec "$container" bash -c "cd /root && mkdir -p pb && chmod 644 pb"
    docker cp "$HOME/cometbftconfig/pb/serf.pb.go" "$container":/root/pb/ || { echo "Failed to copy serf pb go file to $container"; exit 1; }
    docker cp "$HOME/cometbftconfig/pb/serf_grpc.pb.go" "$container":/root/pb/ || { echo "Failed to copy serf grpc pb go file to $container"; exit 1; }
    docker exec "$container" bash -c "cd /root && /usr/local/go/bin/go clean -modcache && /usr/local/go/bin/go mod tidy && /usr/local/go/bin/go build -o /root/serf serfapi.go"
    docker exec -d "$container" /root/serf
    docker exec "$container" /usr/local/go/bin/go install github.com/fullstorydev/grpcurl/cmd/grpcurl@latest
    echo "$container setup complete."
    
  done
  acontainer="${containers[0]}"
  json_list=$(printf '"%s",' "${ip_list[@]}")
  json_list="[${json_list%,}]"
  echo "List of Nodes: json_list"
  
  # Joining Cluster
  docker exec "$acontainer" bash -c "grpcurl -d '{\"peers\":$json_list}' 127.0.0.1:7373 serfapi.SerfService/Join"
  active_members=$(docker exec "$acontainer" grpcurl 127.0.0.1:7373 serfapi.SerfService/Members)
  echo "Active Memebers: $active_members"
  echo "Serf Configured and Running..."
  
}

# Main script execution
setup_ubuntu_nodes