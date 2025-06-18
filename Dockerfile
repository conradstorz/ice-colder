FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install system dependencies and uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential pipx \
    && pipx ensurepath \
    && pipx install uv \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Add uv to PATH for this and subsequent steps
ENV PATH="/root/.local/bin:${PATH}"

# Copy project files (including pyproject.toml and uv.lock)
COPY . .

# Install project dependencies using uv
RUN uv sync --frozen

# Verify uvicorn installation
RUN find / -name uvicorn
RUN /root/.local/bin/uvicorn --version || true

# Expose the port FastAPI will run on
EXPOSE 8000

# Start the FastAPI server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
