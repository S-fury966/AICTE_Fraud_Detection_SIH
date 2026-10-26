# Frontend dev server for the Authentiq dashboard.
#
# Vite dev server, port 5175 (pinned by vite.config.ts). The dashboard proxies
# /api to API_TARGET, defaulting to http://127.0.0.1:8765 — in compose it is
# set to http://backend:8765 so same-origin fetches reach the backend service.
FROM node:20-slim

ENV CI=1

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 5175

# --host 0.0.0.0 so the port is reachable from outside the container.
CMD ["npx", "vite", "--host", "0.0.0.0"]
