FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    lsof \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml .
COPY src/ src/
COPY config.example.yaml .

# Install the package
RUN uv pip install --system .

# Create state directories
RUN mkdir -p /var/log/mediasorter /root/.local/state/mediasorter /root/.config/mediasorter

# Default config location
ENV MEDIASORTER_CONFIG=/app/config.yaml

EXPOSE 9876

ENTRYPOINT ["mediasorter"]
CMD ["daemon"]
