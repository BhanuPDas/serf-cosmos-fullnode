#!/bin/bash

# List of containers (Ubuntu nodes for 100 nodes)
containers=()
for i in {1..5}; do
  containers+=(clab-century-serf$i)
done

# Start Serf agent on each node
start_serf_agents() {
  for container in "${containers[@]}"; do
    echo "Starting Serf agent on $container..."
    
    # Ensure the serf binary is executable
    docker exec "$container" chmod +x /opt/serfapp/serf
    
    # Start the serf agent with the specified config file
    docker exec -d "$container" /opt/serfapp/serf agent -profile lan -config-file=/opt/serfapp/node.json
    #docker exec -d
    
    echo "Serf agent started on $container."
    echo ""
  done
}

# Main script execution
start_serf_agents

