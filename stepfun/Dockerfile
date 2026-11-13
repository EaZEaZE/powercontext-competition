FROM node:20-bookworm-slim

WORKDIR /app

# Install dependencies for seekdb
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget libaio1t64 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install seekdb from GitHub release
RUN wget -q https://github.com/oceanbase/seekdb/releases/latest/download/seekdb-linux-amd64.deb -O /tmp/seekdb.deb && \
    dpkg -i /tmp/seekdb.deb && \
    rm /tmp/seekdb.deb

# Copy backend
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

WORKDIR /app/backend

# Fix seekdb npm packages (workspace:* bug)
RUN npm pack seekdb@1.4.0 2>/dev/null && \
    tar xzf seekdb-1.4.0.tgz && \
    sed -i 's/"optionalDependencies"/"_optionalDependencies"/g' package/package.json && \
    sed -i 's/"peerDependencies"/"_peerDependencies"/g' package/package.json && \
    sed -i 's/"peerDependenciesMeta"/"_peerDependenciesMeta"/g' package/package.json && \
    tar czf /tmp/seekdb-fixed.tgz -C package . && \
    npm pack @seekdb/bm25@1.4.0 2>/dev/null && \
    tar xzf seekdb-bm25-1.4.0.tgz && \
    sed -i 's/"workspace:\*"/"*"/g' package/package.json && \
    tar czf /tmp/bm25-fixed.tgz -C package . && \
    rm -rf package seekdb-1.4.0.tgz seekdb-bm25-1.4.0.tgz && \
    npm install /tmp/seekdb-fixed.tgz /tmp/bm25-fixed.tgz express cors @seekdb/default-embed@1.2.0 && \
    rm -f /tmp/seekdb-fixed.tgz /tmp/bm25-fixed.tgz

# Startup script
COPY docker-entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 3000

CMD ["/app/entrypoint.sh"]
