# Container specification for smart-job-apply-mcp
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml requirements.txt README.md /app/
COPY . /app/

# Install dependencies and the package
RUN pip install --no-cache-dir -e .

# Run the MCP server over stdio
ENTRYPOINT ["python", "server.py"]
