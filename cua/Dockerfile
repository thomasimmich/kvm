# CUA service for a Linux host (cloud). On Linux, OCR uses EasyOCR (CUDA when available)
# instead of Apple Vision. For GPU hosts, start from a CUDA PyTorch base image instead.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --extra omniparser --no-install-project --no-dev
COPY src ./src
COPY knowledge ./knowledge
RUN uv sync --extra omniparser --no-dev

ENV CUA_MCP_HOST=0.0.0.0 \
    CUA_MCP_PORT=8765 \
    CUA_WEIGHTS_DIR=/app/weights \
    CUA_RUNS_DIR=/app/runs
VOLUME ["/app/weights", "/app/runs"]
EXPOSE 8765
CMD ["uv", "run", "--no-dev", "cua", "serve"]
