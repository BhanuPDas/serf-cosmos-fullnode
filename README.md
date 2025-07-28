# serf-cosmos-fullnode
This repository has code changes for serf-cosmetbft integration.

## Steps to run the application

1. Follow the steps mentioned in [5 Nodes Readme](https://github.com/BhanuPDas/serf-cosmos-fullnode/blob/main/topology.md) to create 5 nodes topology using containerlab.
2. Run the docker compose file to start cometBFT, custom ABCI server and Redis Pub/Sub MQ. ***command: docker compose up***
3. Go to [codeBlock Folder](https://github.com/BhanuPDas/serf-cosmos-fullnode/tree/main/bridge/codeBlock)
4. Run the python file: ***python3 app_ui.py***
5. Click to initiate transactions.
6. Verify logs to validate the transaction.

## Application Architecture

This diagram shows the architecture of the application.
