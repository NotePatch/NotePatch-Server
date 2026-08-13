FROM node:20-alpine AS build
WORKDIR /opt/notepatch/web/admin
COPY web/admin/package*.json ./
RUN npm ci
COPY web/admin ./
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/docker/admin-nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /opt/notepatch/web/admin/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
