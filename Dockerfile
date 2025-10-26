FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies and geth
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg2 lsb-release build-essential wget jq python3-dev git \
    && rm -rf /var/lib/apt/lists/*

# Install geth (ethereum client)
RUN wget -qO - https://packages.ethereum.org/keys.gpg | apt-key add - \
 && echo "deb [arch=amd64] https://packages.ethereum.org/debian $(lsb_release -cs) main" > /etc/apt/sources.list.d/ethereum.list \
 && apt-get update && apt-get install -y ethereum \
 && rm -rf /var/lib/apt/lists/*

# Copy files
COPY . /app

# Install python deps
RUN pip install --upgrade pip wheel setuptools
RUN pip install --no-cache-dir -r requirements.txt

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
EXPOSE 8545 30303 30303/udp

CMD ["/app/entrypoint.sh"]
