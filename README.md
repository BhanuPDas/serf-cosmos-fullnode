# Source Code And Configurations for Transaction Module
This repository has code changes and CometBFT configurations for a 25 Node Topology. This includes scripts to automate the process.

## Steps to set up CometBFT (For 25 Nodes) (These steps only configure CometBFT. Validators' configurations will be added later.)

1. Pull the repository to the local system (VM).
2. Navigate to the folder 25NodeCometSetup ***cd 25NodeCometSetup***
3. Execute the script setup_cometbft ***./setup_cometbft.sh***
4. The script installs all required software and starts all applications required for the transaction module. The script uses the config file and deploys the application code provided in the folder.
5. To test transactions, go to the root folder on the specific containers and run the python file - main.py ***python3 main.py***
6. To trigger transactions from UI on a VM, run tx_api.py. This will expose an API for UI to send a request to initiate a transaction. Make sure to terminate the main.py script if running before running tx_api.py. ***python3 tx_api.py***
7. If the ABCI client or CometBFT is down or not responding, in either case, run the reset_comet script. It will automatically restart the application. ***./reset_comet.sh***


```Note: The configuration/reset scripts are applicable for 25 25-node topology only. For other topology setups, these scripts need to be updated accordingly.```