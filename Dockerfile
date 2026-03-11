# emrys sovereign mode — containerized agent memory with key isolation
#
# Keys generated INSIDE the container on first run.
# Private key never leaves the container filesystem.
# .persist volume survives restarts but is isolated from host.
#
# Usage:
#   docker build -t emrys:sovereign .
#   docker run -v emrys-data:/agent/.persist emrys:sovereign
#
# With MCP stdio (for Claude Code):
#   docker run -i -v emrys-data:/agent/.persist emrys:sovereign serve
#
# Initialize sovereign identity:
#   docker run -v emrys-data:/agent/.persist emrys:sovereign init --svrnty --mode tool --dir /agent/.persist

FROM python:3.12-alpine AS base

RUN apk add --no-cache gcc musl-dev libffi-dev

WORKDIR /agent

# Install emrys with sovereign extras
COPY . /opt/emrys/
RUN pip install --no-cache-dir "/opt/emrys[svrnty]"

# .persist lives in a named volume — isolated from host
VOLUME /agent/.persist

# Default: serve MCP over stdio
ENTRYPOINT ["emrys"]
CMD ["serve", "--persist-dir", "/agent/.persist"]
