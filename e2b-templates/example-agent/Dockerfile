FROM docker.io/cloudflare/sandbox:0.7.0

# Install git and jq - fast
RUN apt-get update && apt-get install -y --no-install-recommends git jq curl && rm -rf /var/lib/apt/lists/*

# Python packages - from cache
RUN pip install --no-cache-dir requests pyyaml

# Copy wrapper
COPY modulo-wrap.sh /usr/local/bin/modulo-wrap
RUN chmod +x /usr/local/bin/modulo-wrap

WORKDIR /home/user
ENTRYPOINT ["modulo-wrap"]
