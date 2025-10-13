#!/bin/bash

# The node that will run the 'serf join' command
joining_node="clab-century-serf1"

# List of containers (Ubuntu nodes from 2 to 100)
containers=()
for i in {2..5}; do
  containers+=(clab-century-serf$i)
done

# Gather IP addresses of all nodes
ip_addresses=()
for container in "${containers[@]}"; do
  # Attempt to get the IP address of eth1
  ip_address=$(docker exec "$container" ip -4 addr show eth1 | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
  
  # Check if the IP was found and is not empty
  if [[ -z "$ip_address" ]]; then
    echo "Warning: Could not find IP address for eth1 on container $container"
  else
    ip_addresses+=("$ip_address")
  fi
done

# Check if any IP addresses were collected
if [[ ${#ip_addresses[@]} -eq 0 ]]; then
  echo "Error: No IP addresses were found on eth1 for the specified containers."
  exit 1
fi

# Join all nodes together using the 'serf join' command
join_command="/opt/serfapp/serf join ${ip_addresses[*]}"
echo "Joining the cluster from $joining_node with the following command:"
echo "$join_command"

# Execute the join command on the first node
docker exec "$joining_node" bash -c "$join_command"

echo "Cluster joined successfully."

