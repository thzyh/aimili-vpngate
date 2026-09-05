FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        iproute2 \
        iptables \
        openvpn \
        procps \
        psmisc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/aimilivpn

COPY control_api.py main_assignment.py node_pool.py proxy_server.py vpn_utils.py vpngate_manager.py ./
COPY deploy/docker-single-exit/entrypoint.py ./docker-entrypoint.py

ENTRYPOINT ["python3", "/opt/aimilivpn/docker-entrypoint.py"]
