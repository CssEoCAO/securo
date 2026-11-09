FROM node:22-slim AS build

WORKDIR /app

ARG VITE_APP_VERSION
ENV VITE_APP_VERSION=$VITE_APP_VERSION
ARG VITE_BASE_PATH=/
ENV VITE_BASE_PATH=$VITE_BASE_PATH

COPY package.json package-lock.json .npmrc ./
RUN npm ci

COPY . .
RUN npm run build

# Serve with nginx
FROM nginxinc/nginx-unprivileged:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY default.conf.template /etc/nginx/templates/default.conf.template

# Have the image derive the DNS resolver from /etc/resolv.conf, so the same
# image proxies to the backend on Docker, Podman, Kubernetes and Railway with
# no per-platform configuration. 19-resolver.envsh turns that into
# NGINX_RESOLVER (with a Docker fallback) for the config template; setting
# NGINX_RESOLVER explicitly still overrides both.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
COPY --chmod=755 docker-entrypoint.d/19-resolver.envsh /docker-entrypoint.d/

# The classic builder ignores --chmod, and a checkout made under a restrictive
# umask leaves these unreadable to nginx's unprivileged user. Normalize here.
USER root
RUN chmod 644 /etc/nginx/templates/default.conf.template \
    && chmod 755 /docker-entrypoint.d/19-resolver.envsh \
    && chmod -R a+rX /usr/share/nginx/html
USER 101

EXPOSE 8080
