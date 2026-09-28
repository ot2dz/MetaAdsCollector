# Meta Ads Collector Production Container for Coolify / Docker
FROM python:3.11-slim

# Install system dependencies needed for compiling and network utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files first to leverage Docker cache
COPY requirements.txt pyproject.toml ./

# Install python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code and application
COPY . .

# Install the library in editable mode
RUN pip install --no-cache-dir -e .

# Expose web application port
EXPOSE 5001

# Run the Flask web application
CMD ["python", "web_app.py"]