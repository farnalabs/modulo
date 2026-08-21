# syntax=docker/dockerfile:1.7
FROM node:22-alpine
WORKDIR /app
COPY package.json pnpm-lock.yaml .npmrc pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    npm install -g pnpm@11.21.0 --ignore-scripts && pnpm install --frozen-lockfile --ignore-scripts && \
    if [ ! -f node_modules/@internationalized/date/dist/index.mjs ] && [ -f node_modules/@internationalized/date/dist/index.js ]; then \
        cp node_modules/@internationalized/date/dist/index.js node_modules/@internationalized/date/dist/index.mjs; \
    fi
COPY . .
RUN chown -R node:node /app
USER node
EXPOSE 5173
CMD ["pnpm", "run", "dev", "--", "--host", "0.0.0.0"]
