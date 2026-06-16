# Tiny Python image. Slim variant keeps the image small without giving up
# the standard library or pip.
FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first so changes to app code do not bust
# the dependency layer cache on every rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual application code.
COPY . .

EXPOSE 8000

# Uvicorn is our ASGI server. --host 0.0.0.0 makes the service reachable
# from outside the container (other containers in the docker network,
# and the host's 127.0.0.1:8000 port binding in docker-compose.yml).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
