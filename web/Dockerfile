# syntax=docker/dockerfile:1

FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# Next serialises `rewrites()` into routes-manifest.json at build time, so the
# proxy target is read HERE and not from the runner's environment. The default
# is the compose service name, which is what production actually uses; the ARG
# exists so a different topology can be baked without editing next.config.mjs.
ARG API_ORIGIN=http://netedge-api:8000
ENV API_ORIGIN=$API_ORIGIN
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app

RUN apk add --no-cache curl \
  && addgroup -g 1001 -S nodejs \
  && adduser -u 1001 -S nextjs -G nodejs

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
# Declared for parity with the build stage and so `docker inspect` shows what
# this image proxies to. The value that is actually in force was baked into the
# route manifest above — changing it here alone does nothing.
ENV API_ORIGIN=http://netedge-api:8000

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

# The login page is the only route that renders without a session, so it is
# the only honest liveness probe for the web container itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/netedge/login" || exit 1

CMD ["node", "server.js"]
