FROM python:3.12-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       chromium \
       chromium-sandbox \
       fonts-liberation \
       fonts-noto-color-emoji \
       libgbm1 \
       libnss3 \
       libatk-bridge2.0-0 \
       libgtk-3-0 \
       libxcomposite1 \
       libxdamage1 \
       libxrandr2 \
       libasound2t64 \
       xdg-utils \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash mithwire

COPY . /build
RUN pip install --no-cache-dir /build \
    && rm -rf /build

ENV CHROME=/usr/bin/chromium
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /data /downloads \
    && chown mithwire:mithwire /data /downloads

USER mithwire
WORKDIR /home/mithwire

EXPOSE 8000

ENTRYPOINT ["mithwire-mcp"]
CMD ["--transport", "stdio"]
