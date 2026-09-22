FROM node:24-slim AS frontend
WORKDIR /app
COPY package.json package-lock.json tsconfig.json tsconfig.web.json ./
COPY apps/api/package.json ./apps/api/package.json
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci
COPY apps/web ./apps/web
COPY scripts/build-react.ts ./scripts/build-react.ts
RUN npm run build:web

FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/api/backend ./apps/api/backend
COPY alembic.ini ./alembic.ini
COPY apps/web ./apps/web
COPY --from=frontend /app/.web-build ./.web-build
ENV PYTHONPATH=/app/apps/api
ENV SWARMUI_HOST=0.0.0.0 SWARMUI_PORT=7801 U2NET_HOME=/app/data/models/rembg
RUN groupadd --gid 10001 aivideo && useradd --uid 10001 --gid 10001 --no-create-home aivideo && mkdir -p /app/data && chown 10001:10001 /app/data
USER 10001:10001
EXPOSE 7801
CMD ["python", "-m", "backend"]
