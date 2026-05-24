#!/bin/bash
set -e

JUPYTER_PORT="${JUPYTER_PORT:-8888}"

# Start JupyterLab with MCP server extension in background
jupyter lab \
    --JupyterMCPServerExtensionApp.document_url=local \
    --JupyterMCPServerExtensionApp.runtime_url=local \
    --JupyterMCPServerExtensionApp.document_id=notebook.ipynb \
    --JupyterMCPServerExtensionApp.start_new_runtime=True \
    --ServerApp.token='' \
    --ServerApp.password='' \
    --ServerApp.disable_check_xsrf=True \
    --ServerApp.root_dir=/workspace \
    --port="${JUPYTER_PORT}" \
    --ip=0.0.0.0 \
    --no-browser \
    --allow-root \
    > /tmp/jupyter.log 2>&1 &

# Wait for Jupyter to start
sleep 2

echo "Logs: /tmp/jupyter.log"

# Keep container alive
tail -f /tmp/jupyter.log

