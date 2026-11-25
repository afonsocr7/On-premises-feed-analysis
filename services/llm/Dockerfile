FROM ollama/ollama:latest
ARG MODEL=gemma3:1b
RUN set -e; ollama serve & PID=$!; \
    until ollama list >/dev/null 2>&1; do sleep 1; done; \
    ollama pull "$MODEL"; kill $PID; wait $PID || true