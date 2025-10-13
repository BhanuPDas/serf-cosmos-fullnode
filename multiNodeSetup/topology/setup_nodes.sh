#!/bin/bash

# List of containers (Ubuntu nodes for 100 nodes)
containers=()
for i in {1..5}; do
  containers+=(clab-century-serf$i)
done

# Paths and file names
json_file="node.json"
binary_file="serf"      # Use the full path to the serf binary
pytogo_binary="pytogoapi"  # This is the binary file for pytogo
destination_dir="/opt/serfapp"

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
    ip_address=$(docker exec "$container" ip -4 addr show eth1 | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
    if [ -z "$ip_address" ]; then
      echo "Failed to retrieve IP address for $container"
      continue
    fi
    
    echo "IP address for $container (eth1): $ip_address"
    
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
    docker cp "$binary_file" "$container":"$destination_dir/" || { echo "Failed to copy serf binary to $container"; exit 1; }

    # Create the pytogo directory inside the container
    docker exec "$container" mkdir -p "$destination_dir/pytogo"

    # Copy the pytogo binary file into the pytogo directory
    docker cp "$pytogo_binary" "$container":"$destination_dir/pytogo/" || { echo "Failed to copy pytogo binary to $container"; exit 1; }

    # Make the serf binary executable
    docker exec "$container" chmod +x "$destination_dir/$binary_file" || { echo "Failed to make serf executable on $container"; exit 1; }
    
    # Make the pytogo binary executable
    docker exec "$container" chmod +x "$destination_dir/pytogo/$pytogo_binary" || { echo "Failed to make pytogo binary executable on $container"; exit 1; }

    # Remove the temporary JSON file
    rm "$temp_json_file"

    echo "$container setup complete."
  done
}

# Main script execution
setup_ubuntu_nodes

