# syntax=docker/dockerfile:1

FROM nginx:1.29-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/nginx.conf

USER nginx

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD ["wget", "--quiet", "--output-document=-", "http://127.0.0.1:8080/health"]

CMD ["nginx", "-g", "daemon off;"]
