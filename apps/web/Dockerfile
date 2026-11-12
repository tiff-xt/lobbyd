# Build context is the repository root (npm workspaces live there):
#   docker build -f apps/web/Dockerfile .

FROM node:22-alpine AS build
WORKDIR /repo
# Manifests first: this layer is reused until a dependency changes.
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
COPY libs/api-client/package.json libs/api-client/package.json
RUN npm ci
COPY libs/api-client libs/api-client
COPY apps/web apps/web
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build --workspace web


FROM node:22-alpine
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
# The standalone build is a minimal server with only the files it needs.
COPY --from=build /repo/apps/web/.next/standalone ./
COPY --from=build /repo/apps/web/.next/static ./apps/web/.next/static
USER node
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
