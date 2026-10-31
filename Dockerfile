FROM node:22-alpine AS frontend
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY index.html vite.config.ts tsconfig*.json ./
COPY src ./src
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./backend/app
COPY --from=frontend /app/dist ./frontend/dist
RUN mkdir -p /app/data/traces
ENV TRACESCOPE_DATA=/app/data
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
