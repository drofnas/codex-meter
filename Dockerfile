FROM golang:1.27.1-alpine@sha256:cf6fca6641884b8433441b2b0652976f975e1d0fdd26d177eaaf8596087f3125 AS build
WORKDIR /src
COPY go.mod ./
COPY internal/api/ ./internal/api/
COPY internal/calendar/ ./internal/calendar/
COPY cmd/meter-api/ ./cmd/meter-api/
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /meter-api ./cmd/meter-api

FROM scratch
COPY --from=build /meter-api /meter-api
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/meter-api"]
