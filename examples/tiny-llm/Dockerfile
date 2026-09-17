FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY server.py /opt/tiny-llm/server.py
EXPOSE 8080
CMD ["python3", "/opt/tiny-llm/server.py"]
