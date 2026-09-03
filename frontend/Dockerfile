# syntax=docker/dockerfile:1.27
FROM node:22-alpine
WORKDIR /app
COPY package.json pnpm-lock.yaml .npmrc pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    npm install -g pnpm@11.21.0 --ignore-scripts && pnpm install --frozen-lockfile --ignore-scripts && \
    if [ ! -f node_modules/@internationalized/date/dist/index.mjs ] && [ -f node_modules/@internationalized/date/dist/index.js ]; then \
        cp node_modules/@internationalized/date/dist/index.js node_modules/@internationalized/date/dist/index.mjs; \
    fi
# S6470: copy only the sources needed to run the dev server instead of `COPY . .`
# (a recursive copy can inadvertently pull in secrets like .env). A
# .dockerignore additionally excludes .env, node_modules, .git, etc.
COPY index.html vite.config.ts tsconfig.json tsconfig.node.json tailwind.config.cjs components.json eslint.config.mjs .npmrc .nsprc ./
COPY src ./src
COPY public ./public
RUN chown -R node:node /app
USER node
EXPOSE 5173
CMD ["pnpm", "run", "dev", "--", "--host", "0.0.0.0"]
