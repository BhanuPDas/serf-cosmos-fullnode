#!/bin/bash

# List of containers
containers=()
for i in {1..25}; do
  containers+=(clab-century-serf$i)
done

reset_cometbft() {
  for container in "${containers[@]}"; do
    echo "=============================================="
    echo "Resetting ABCI + CometBFT on $container..."
    echo "=============================================="

    echo "[1] Killing CometBFT..."
    comet_pid=$(docker exec "$container" pgrep -f "/root/go/bin/cometbft node")
    if [[ -n "$comet_pid" ]]; then
      docker exec "$container" kill -9 $comet_pid
      sleep 1
    else
      echo "CometBFT not running"
    fi

    echo "[2] Killing ABCI..."
    abci_pid=$(docker exec "$container" pgrep -f "/root/abci-app")
    if [[ -n "$abci_pid" ]]; then
      docker exec "$container" kill -9 $abci_pid
      sleep 1
    else
      echo "ABCI not running"
    fi

    echo "[3] Removing state.db..."
    docker exec "$container" rm -rf /root/abci/state.db
    sleep 1

    echo "[4] Resetting CometBFT state..."
    docker exec "$container" /root/go/bin/cometbft unsafe-reset-all
    sleep 1

    echo "[5] Restarting ABCI..."
    docker exec -d "$container" bash -c "nohup /root/abci-app > /root/logs/abci.log 2>&1"
    sleep 2

    echo "[6] Restarting CometBFT..."
    docker exec -d "$container" bash -c "nohup /root/go/bin/cometbft node > /root/logs/cometbft.log 2>&1"
    sleep 3

    echo "[7] Verifying logs..."
    docker exec "$container" tail -n 20 /root/logs/abci.log
    docker exec "$container" tail -n 20 /root/logs/cometbft.log

    echo "✔ Done with $container"
  done
}

reset_cometbft
