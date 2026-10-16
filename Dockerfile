FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project

# Copy application files
COPY main.py ./
COPY static ./static

EXPOSE 8000

# Bind to 0.0.0.0 so traffic can reach inside the container
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]