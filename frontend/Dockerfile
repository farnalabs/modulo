# syntax=docker/dockerfile:1.7
FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --ignore-scripts --no-audit --no-fund && \
    if [ ! -f node_modules/reka-ui/dist/index.d.ts ] && [ -f node_modules/reka-ui/dist/index.d.cts ]; then \
        cp node_modules/reka-ui/dist/index.d.cts node_modules/reka-ui/dist/index.d.ts; \
    fi && \
    if [ ! -f node_modules/@internationalized/date/dist/index.mjs ] && [ -f node_modules/@internationalized/date/dist/index.js ]; then \
        cp node_modules/@internationalized/date/dist/index.js node_modules/@internationalized/date/dist/index.mjs; \
    fi
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
