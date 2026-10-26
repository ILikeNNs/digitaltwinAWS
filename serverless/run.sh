#!/bin/bash
# Lambda handler under the Lambda Web Adapter (AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap): starts the web app,
# the adapter forwards Function URL requests to it and streams responses back.
PATH=$PATH:$LAMBDA_TASK_ROOT/bin \
    PYTHONPATH=$PYTHONPATH:/opt/python:$LAMBDA_RUNTIME_DIR \
    exec python -m uvicorn --host 127.0.0.1 --port "${AWS_LWA_PORT:-8080}" --no-access-log server:app
