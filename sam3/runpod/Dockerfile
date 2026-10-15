# SAM 3 Runpod serverless worker.
#
# Runpod GitHub integration: Dockerfile Path = sam3/runpod/Dockerfile. Local or CI build:
#   docker build --platform linux/amd64 -f sam3/runpod/Dockerfile -t <registry>/p2p-sam3:<sha> .
#
# Weights are not baked in. In order of preference the handler loads them from:
#   1. SAM3_MODEL_PATH, e.g. a network volume folder /runpod-volume/models/sam3
#   2. Runpod cached models: endpoint "Model" field = facebook/sam3 plus an HF token from the
#      approved account, mounted at
#      /runpod-volume/huggingface-cache/hub/models--facebook--sam3/snapshots/<hash>/
#   3. A Hub download of SAM3_MODEL_ID (default facebook/sam3, needs HF_TOKEN) at cold start,
#      billed as worker time
# Every source is checked against the facebook/sam3 model.safetensors sha256 before loading.
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# Works whether the build context is the repo root or sam3/runpod/ (Runpod's builder does not
# document which one it uses).
COPY . /tmp/ctx
RUN set -eu; \
    if [ -f /tmp/ctx/sam3/runpod/handler.py ]; then src=/tmp/ctx/sam3/runpod; else src=/tmp/ctx; fi; \
    cp "$src/handler.py" "$src/smoke.py" "$src/requirements.txt" /app/; \
    rm -rf /tmp/ctx

RUN pip install -r /app/requirements.txt \
 && python -c "import torch, torchvision, transformers; from transformers import Sam3Model, Sam3Processor; print(torch.__version__, torchvision.__version__, transformers.__version__)"

CMD ["python", "-u", "/app/handler.py"]
