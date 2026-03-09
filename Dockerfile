# cairn-ai sovereign mode — containerized agent memory with key isolation
#
# Keys generated INSIDE the container on first run.
# Private key never leaves the container filesystem.
# .persist volume survives restarts but is isolated from host.
#
# Usage:
#   docker build -t cairn-ai:sovereign .
#   docker run -v cairn-data:/agent/.persist cairn-ai:sovereign
#
# With MCP stdio (for Claude Code):
#   docker run -i -v cairn-data:/agent/.persist cairn-ai:sovereign serve
#
# Initialize sovereign identity:
#   docker run -v cairn-data:/agent/.persist cairn-ai:sovereign init --sovereign --mode tool --dir /agent/.persist

FROM python:3.12-alpine AS base

RUN apk add --no-cache gcc musl-dev libffi-dev

WORKDIR /agent

# Install cairn with sovereign extras
COPY . /opt/cairn-ai/
RUN pip install --no-cache-dir "/opt/cairn-ai[sovereign]"

# .persist lives in a named volume — isolated from host
VOLUME /agent/.persist

# Default: serve MCP over stdio
ENTRYPOINT ["cairn"]
CMD ["serve", "--persist-dir", "/agent/.persist"]
