# Source Code And Configurations for Transaction Module
This repository has code changes and CometBFT configurations for 25 Node Topology. This includes scripts to automate the process.

## Steps to setup CometBFT (For 25 Nodes) (This steps only configures CometBFT. Validators configurations will be added later.)

1. Pull the repository to the local system (VM).
2. Navigate to the folder 25NodeCometSetup ***cd 25NodeCometSetup***
3. Execute the script setup_cometbft ***./setup_cometbft.sh***
4. The script installs all required software and starts all applications required for transaction module. The script uses the config file and deploys the application code provided in the folder.
5. In order to test transactions, go to the root folder on the specific containers and run the python file - main.py ***python3 main.py***
6. Inorder to trigger transactions from UI on a VM, run tx_api.py. This will expose an API for UI to send request to initiate a transaction. Make sure to terminate main.py script if running before running tx_api.py. ***python3 tx_api.py***
7. If ABCI client or CometBFT is down or not responding, in either case, run reset_comet script. It will automatically restart the application. ***./reset_comet.sh***


'''Note: The configuration/reset scripts are applicable for 25 Nodes topology. For other topology set, these scripts needs to be updated accordingly.'''