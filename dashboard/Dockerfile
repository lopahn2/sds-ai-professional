FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
# @naria/ui 디자인 시스템은 레지스트리에 게시되지 않아 저장소에 포함한 패키지 파일로 설치한다.
COPY vendor ./vendor
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
