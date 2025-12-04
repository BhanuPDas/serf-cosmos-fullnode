FROM golang:1.24-alpine AS builder
RUN apk add --no-cache git
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o abci-app main.go

# Stage 2: Create a lightweight image
FROM alpine:3.22
WORKDIR /app
RUN apk add --no-cache netcat-openbsd
COPY --from=builder /app/abci-app .
EXPOSE 26658
CMD ["./abci-app", "tcp://0.0.0.0:26658"]
