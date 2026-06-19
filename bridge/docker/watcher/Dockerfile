FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/telegram-watcher/ /app/
COPY freqtrade/signal_strategy/ /app/freqtrade/signal_strategy/
COPY freqtrade/requirements*.txt /tmp/freqtrade-requirements/

RUN for req in /tmp/freqtrade-requirements/requirements*.txt; do \
      pip3 install --break-system-packages --no-cache-dir -r "$req"; \
    done

RUN npm ci --omit=dev

CMD ["node", "server.js"]
