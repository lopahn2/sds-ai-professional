FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
WORKDIR /app
COPY --chown=node:node package.json *.js ./
COPY --chown=node:node static ./static
COPY --chown=node:node test ./test
USER node
RUN node --test test
ARG RELEASE_FAULTS='{"faults":{}}'
ENV PORT=8080 RELEASE_FAULTS=${RELEASE_FAULTS}
EXPOSE 8080
CMD ["node", "server.js"]
