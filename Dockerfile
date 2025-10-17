# ================================
# Hugging Face LLM Code Deployer
# ================================

# Use lightweight Python base
FROM python:3.10-slim

# Disable Python bytecode & buffering
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies (git required for pushes)
RUN apt-get update && \
    apt-get install -y git curl && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency list first (for Docker caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app source code
COPY . .

# Set environment variables (for Hugging Face Spaces)
# You can override these in the Hugging Face Space UI under "Settings > Variables"
ENV HF_SPACE=1
ENV PORT=7860
ENV HOST=0.0.0.0

# Expose the default port
EXPOSE 7860

# Run the app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]

