#!/bin/bash

# List of containers (Ubuntu nodes)
containers=()
for i in {1..5}; do
  containers+=(clab-century-serf$i)
done

setup_multinodes_cometbft() {
  for container in "${containers[@]}"; do

    # Get IP address of eth1
    ip_address=$(docker exec "$container" ip -4 addr show eth1 | grep -oP '(?<=inet\s)10\.0\.1\.\d+')
    if [ -z "$ip_address" ]; then
      echo "Failed to retrieve IP address for $container"
      continue
    fi
    echo "IP address for $container (eth1): $ip_address"
    
    # Install Redis
    docker exec "$container" bash -c "apt-get update && apt-get install -y lsb-release curl gpg && \
    curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg && \
    chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg && \
    echo 'deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main' | tee /etc/apt/sources.list.d/redis.list && \
    apt-get update && apt-get install -y redis"
    rVersion=$(docker exec "$container" redis-server --version)
    echo "Redis $rVersion installation complete."
    
    # Configure ABCI server
    echo "Configuring ABCI server..."
    docker cp "$HOME/cometbftconfig/abci/go.mod" "$container":/root/abci/ || { echo "Failed to copy go mod file to $container"; exit 1; }
    docker cp "$HOME/cometbftconfig/abci/main.go" "$container":/root/abci/ || { echo "Failed to copy abci main go file to $container"; exit 1; }
    docker exec "$container" bash -c "cd /root && /usr/local/go/bin/go clean -modcache && /usr/local/go/bin/go mod tidy && /usr/local/go/bin/go build -o /root/abci-app /abci/main.go"
    docker exec -d "$container" /root/abci-app
    
    # Install CometBFT
    echo "Installing Cometbft..."
    docker exec "$container" /usr/local/go/bin/go install github.com/cometbft/cometbft/cmd/cometbft@v1.0.0
    cVersion=$(docker exec "$container" /root/go/bin/cometbft version)
    echo "CometBFT $cVersion installation complete."

    # Init CometBFT
    echo "Configuring Cometbft..."
    docker exec "$container" /root/go/bin/cometbft init
    nodeId=$(docker exec "$container" /root/go/bin/cometbft show-node-id)
    echo "CometBFT Node: $nodeId configured."
    docker exec "$container" rm -f /root/.cometbft/config/config.toml
    docker cp "$HOME/cometbftconfig/config.toml" "$container":/root/.cometbft/config/ || { echo "Failed to copy config.toml file to $container"; exit 1; }
    echo "Starting Cometbft..."
    docker exec -d "$container" /root/go/bin/cometbft node

    # Add tags to Serf
    echo "Setting Serf Tags for $container..."
    docker exec "$container" /root/go/bin/grpcurl -d "{\"tags\":{\"role\":\"buyer\",\"rpc_addr\":\"$nodeId@$ip_address:7373\"}}" 127.0.0.1:7373 serfapi.SerfService/SetTags
    
    # Install Python
    echo "Installing Python..."
    docker exec "$container" bash -c "DEBIAN_FRONTEND=noninteractive apt update && apt upgrade -y && apt install -y python3 python3-pip && pip3 install --no-cache-dir flask requests redis"
    pVersion=$(docker exec "$container" python3 --version)
    echo "$pVersion installation complete."
    #echo "Copying Serf Client and Cometbft client..."
    #docker cp "$HOME/cometbftconfig/app.py" "$container":/root/ || { echo "Failed to copy app.py file to $container"; exit 1; }
    #docker cp "$HOME/cometbftconfig/serf_client.py" "$container":/root/ || { echo "Failed to copy serf_client.py file to $container"; exit 1; }
    #docker cp "$HOME/cometbftconfig/cometbft_client.py" "$container":/root/ || { echo "Failed to copy cometbft_client.py file to $container"; exit 1; }
    #docker exec -d "$container" python3 /root/app.py 

    echo "Cometbft setup in $container is complete."
    
  done
}

setup_multinodes_cometbft
